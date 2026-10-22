namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public interface IMediaAccess
{
    bool TryGetUser(Guid userId, out MediaPrincipal? principal);
}

public sealed class MediaPrincipal
{
    public MediaPrincipal(bool isAdministrator, bool isDisabled, Func<Guid, bool> canSeeLibrary, Func<Guid, string, bool> canAccess)
    {
        IsAdministrator = isAdministrator;
        IsDisabled = isDisabled;
        CanSeeLibrary = canSeeLibrary;
        CanAccess = canAccess;
    }

    public bool IsAdministrator { get; }
    public bool IsDisabled { get; }
    public Func<Guid, bool> CanSeeLibrary { get; }
    public Func<Guid, string, bool> CanAccess { get; }
}

public sealed class LibraryListing
{
    public Guid Id { get; init; }
    public string Name { get; init; } = "";
}

public interface ILibraryCatalog
{
    IReadOnlyList<LibraryListing> ListLibraries();

    ParentBinding ResolveParent(Guid parentId);

    bool ItemBelongsToParent(Guid itemId, Guid parentId);
}

public readonly record struct ParentBinding(bool Known, bool IsLibrary);

public sealed class SegmentDecision
{
    public int StatusCode { get; init; }
    public string? Code { get; init; }
    public string? Message { get; init; }
    public SegmentDetailResponse? Body { get; init; }

    public static SegmentDecision NotFound()
        => new() { StatusCode = 404, Code = "notFound", Message = "Segment was not found." };

    public static SegmentDecision Stale(string code)
        => new() { StatusCode = 409, Code = code, Message = "Segment is no longer current." };

    public static SegmentDecision Ok(SegmentDetailResponse body)
        => new() { StatusCode = 200, Body = body };
}

public sealed partial class SearchRuntime : IDisposable
{
    private readonly IndexStore _store;
    private readonly IEmbeddingClient _embeddings;
    private readonly IMediaAccess _access;
    private readonly ILibraryCatalog _libraries;
    private readonly IMediaFacts _facts;
    private readonly Func<bool> _videoPlayback;
    private readonly Version _hostVersion;
    private readonly string _hostVersionText;

    public SearchRuntime(IndexStore store, IEmbeddingClient embeddings, IMediaAccess access, ILibraryCatalog libraries, Version hostVersion, IMediaFacts facts, Func<bool>? videoPlaybackActive = null)
    {
        _store = store;
        _embeddings = embeddings;
        _access = access;
        _libraries = libraries;
        _facts = facts;
        _videoPlayback = videoPlaybackActive ?? (() => false);
        _hostVersion = hostVersion;
        _hostVersionText = hostVersion.ToString(3);
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        _store.Dispose();
    }

    private int _disposed;

    public IndexStore Store => _store;

    public async Task<CapabilitiesResponse> CapabilitiesAsync(Guid userId, CancellationToken cancellationToken)
    {
        var generationBeforeObserve = _store.ActiveSignature()?.Generation;
        var observation = await ObserveIfConfigured(cancellationToken).ConfigureAwait(false);
        var principal = RequirePrincipal(userId);
        ReconcileCompatibility(observation, generationBeforeObserve);
        return BuildCapabilities(principal, observation);
    }

    public async Task<SearchResponse> SearchAsync(Guid userId, SearchRequest request, CancellationToken cancellationToken)
    {
        RequirePrincipal(userId);
        if (request.Diagnostics)
        {
            RequireAdmin(userId);
        }
        var query = request.Query?.Trim() ?? "";
        if (query.Length == 0 || query.Length > 1000 || query.Contains('\0'))
        {
            throw new SearchInputException("invalidRequest", "Query text is required.");
        }

        int limit;
        if (request.GroupByVideo)
        {
            limit = request.Limit ?? ApiV1.MaxGroupedVideos;
            if (limit < 1 || limit > ApiV1.MaxGroupedVideos)
            {
                throw new SearchInputException("invalidRequest", "Grouped search limit must be between 1 and 3 films.");
            }
        }
        else
        {
            limit = request.Limit ?? ApiV1.DefaultLimit;
            if (limit < 1 || limit > ApiV1.MaxLimit)
            {
                throw new SearchInputException("invalidRequest", "Limit must be between 1 and 50.");
            }
        }

        var response = new SearchResponse
        {
            QueryId = Guid.NewGuid(),
            ClientQueryId = CleanClientQueryId(request.ClientQueryId),
            QualityPolicy = Quality()
        };

        var generationBeforeObserve = _store.ActiveSignature()?.Generation;
        var observation = await ObserveIfConfigured(cancellationToken).ConfigureAwait(false);
        var currentUser = RequirePrincipal(userId);
        ReconcileCompatibility(observation, generationBeforeObserve);
        var selection = SelectLibraries(currentUser, request);
        var libraries = selection.Libraries;
        var containerParent = selection.Container;
        if (!HostCompatibility.IsSupported(_hostVersion))
        {
            response.Availability = Availability("hostIncompatible", "hostIncompatible");
            return response;
        }

        if (_store.SchemaReadOnly)
        {
            response.Availability = Availability("schemaNewer", "schemaNewer");
            return response;
        }

        if (!_store.HasWriterLock)
        {
            response.Availability = Availability("writerLocked", "writerLocked");
            return response;
        }

        if (_store.Incompatible)
        {
            response.Availability = Availability("incompatible", "incompatible");
            return response;
        }

        var snapshot = _store.ActiveSignature();
        if (snapshot is null || libraries.Count == 0)
        {
            response.Availability = Availability("empty", libraries.Count == 0 ? "noLibraries" : "none");
            response.IndexComplete = false;
            return response;
        }

        CandidateSnapshot candidates;
        try
        {
            candidates = _store.ReadCandidateSnapshot();
            if (candidates.Representations.Any(r => !r.Matches(snapshot)))
            {
                response.Availability = Availability("incompatible", "incompatible");
                return response;
            }
        }
        catch (Exception e) when (e is InvalidDataException or System.Text.Json.JsonException)
        {
            response.Availability = Availability("incompatible", "incompatible");
            return response;
        }

        if (request.GroupByVideo && candidates.Representations.Length == 0)
        {
            throw new SearchInputException("invalidRequest", "groupByVideo requires imported candidate representations.");
        }

        if (candidates.Representations.Length > 0 && (!observation.Reachable || !observation.Ready))
        {
            response.Availability = AvailabilityForTransport(observation.ErrorCode);
            return response;
        }

        var embedded = candidates.Representations.Length > 0
            ? await CandidateQueryAsync(userId, request, query, snapshot, cancellationToken).ConfigureAwait(false)
            : await _embeddings.EmbedTextAsync(_store.EmbeddingBaseUrl, query, cancellationToken,
                () => ValidateBeforeQuery(userId, request, snapshot)).ConfigureAwait(false);
        if (embedded.ErrorCode is "generationChanged" or "noLibraries")
        {
            response.Availability = Availability("empty", embedded.ErrorCode == "noLibraries" ? "noLibraries" : "none");
            response.IndexComplete = false;
            return response;
        }

        if (embedded.ErrorCode == "incompatible")
        {
            response.Availability = Availability("incompatible", "incompatible");
            return response;
        }

        if (embedded.ErrorCode == "superseded")
        {
            response.Availability = Availability("timeout", "timeout");
            return response;
        }

        currentUser = RequirePrincipal(userId);
        selection = SelectLibraries(currentUser, request);
        libraries = selection.Libraries;
        containerParent = selection.Container;
        var activeNow = _store.ActiveSignature();
        if (activeNow is null || activeNow.Generation != snapshot.Generation)
        {
            response.Availability = Availability("empty", "none");
            response.IndexComplete = false;
            return response;
        }

        if (!embedded.Success || embedded.Vector is null)
        {
            if (embedded.ErrorCode == "unexpectedModel" && _store.ActiveSignature()?.Generation == snapshot.Generation)
            {
                _store.SetIncompatible(true);
                response.Availability = Availability("incompatible", "incompatible");
                return response;
            }

            response.Availability = AvailabilityForTransport(embedded.ErrorCode);
            return response;
        }

        if (!IndexStore.SignaturesMatch(snapshot, embedded.Observation))
        {
            if (_store.ActiveSignature()?.Generation == snapshot.Generation)
            {
                _store.SetIncompatible(true);
            }

            response.Availability = Availability("incompatible", "incompatible");
            return response;
        }

        if (_store.ActiveGeneration != snapshot.Generation)
        {
            response.Availability = Availability("empty", "none");
            return response;
        }

        if (candidates.Representations.Length > 0)
        {
            return SearchCandidates(userId, request, embedded.Vector, candidates, snapshot, limit, response);
        }

        var rows = _store.LoadSearchable(libraries, snapshot.Generation);
        currentUser = RequirePrincipal(userId);
        var related = new List<SearchHit>();
        var weak = new List<SearchHit>();
        foreach (var row in rows)
        {
            if (containerParent is Guid container && !_libraries.ItemBelongsToParent(row.ItemId, container))
            {
                continue;
            }

            if (!currentUser.CanAccess(row.ItemId, row.MediaSourceId))
            {
                continue;
            }

            if (!FactsMatch(row.ItemId, row.MediaSourceId, row.ContentVersion, libraries))
            {
                continue;
            }

            var score = VectorMath.Dot(embedded.Vector, row.Vector);
            var hit = new SearchHit
            {
                ItemId = row.ItemId,
                MediaSourceId = row.MediaSourceId,
                SegmentId = row.SegmentId,
                StartTicks = row.StartTicks,
                EndTicks = row.EndTicks,
                Score = score,
                Quality = score >= _store.RelatedMinimum ? "related" : "weak",
                IndexComplete = row.MediaState == "complete",
                ContentVersion = row.ContentVersion,
                BindingVersion = SegmentBinding.Version(row.SegmentId, row.ItemId, row.MediaSourceId, row.ContentVersion, row.StartTicks, row.EndTicks, "jointAudioVideo")
            };
            if (hit.Quality == "related")
            {
                related.Add(hit);
            }
            else
            {
                weak.Add(hit);
            }
        }

        related.Sort(static (left, right) => right.Score.CompareTo(left.Score));
        weak.Sort(static (left, right) => right.Score.CompareTo(left.Score));
        response.Results.AddRange(related.Take(limit));
        response.Results.AddRange(weak.Take(limit));
        if (request.CollapseRuns)
        {
            response.Results = RunCollapse.Apply(response.Results, null);
        }

        if (_store.ActiveGeneration != snapshot.Generation)
        {
            response.Results.Clear();
            response.Availability = Availability("empty", "none");
            response.IndexComplete = false;
            return response;
        }

        var coverage = _store.Coverage(libraries);
        response.IndexComplete = coverage.IndexComplete;
        response.Availability = Availability(response.Results.Count == 0 && coverage.Searchable == 0 ? "empty" : "available", "none");
        return response;
    }

    public SegmentDecision GetSegment(Guid userId, Guid segmentId)
    {
        var principal = RequirePrincipal(userId);
        var imported = GetCandidateSegment(userId, segmentId);
        if (imported is not null)
        {
            return imported;
        }
        var row = _store.ReadSegment(segmentId);
        if (!row.Found || !principal.CanSeeLibrary(row.LibraryId) || !principal.CanAccess(row.ItemId, row.MediaSourceId))
        {
            return SegmentDecision.NotFound();
        }

        if (!_store.EnabledLibraryIds().Contains(row.LibraryId))
        {
            return SegmentDecision.NotFound();
        }

        if (_store.Incompatible || _store.SchemaReadOnly || !row.IsCurrent)
        {
            return SegmentDecision.Stale(_store.Incompatible || _store.SchemaReadOnly ? "incompatible" : "stale");
        }

        var facts = ReadFacts(row.ItemId, row.MediaSourceId, _store.EnabledLibraryIds());
        if (facts.Presence == ItemPresence.Unreadable)
        {
            return SegmentDecision.Stale("unknown");
        }

        if (facts.Presence != ItemPresence.Readable || facts.Video is null || facts.Video.MediaSourceId != row.MediaSourceId || facts.Video.ContentVersion != row.ContentVersion)
        {
            return SegmentDecision.Stale("stale");
        }

        return SegmentDecision.Ok(new SegmentDetailResponse
        {
            ItemId = row.ItemId,
            MediaSourceId = row.MediaSourceId,
            SegmentId = row.SegmentId,
            StartTicks = row.StartTicks,
            EndTicks = row.EndTicks,
            Score = 0,
            Quality = "related",
            IndexComplete = row.MediaState == "complete",
            Representation = "jointAudioVideo",
            BindingVersion = SegmentBinding.Version(row.SegmentId, row.ItemId, row.MediaSourceId, row.ContentVersion, row.StartTicks, row.EndTicks, "jointAudioVideo")
        });
    }

    public async Task<AdminStatusResponse> AdminStatusAsync(Guid userId, CancellationToken cancellationToken)
    {
        RequireAdmin(userId);
        var generationBeforeObserve = _store.ActiveSignature()?.Generation;
        var observation = await ObserveIfConfigured(cancellationToken).ConfigureAwait(false);
        RequireAdmin(userId);
        ReconcileCompatibility(observation, generationBeforeObserve);
        return BuildAdmin(observation);
    }

    public void SetLibraries(Guid userId, IReadOnlyList<Guid> libraryIds)
    {
        RequireAdmin(userId);
        var known = _libraries.ListLibraries().Select(library => library.Id).ToHashSet();
        if (libraryIds.Any(id => !known.Contains(id)))
        {
            throw new SearchInputException("invalidRequest", "One or more libraries are not available on this server.");
        }

        _store.ReplaceLibraries(libraryIds);
        _store.RequestReconcile();
    }

    public void UpdateSettings(Guid userId, SettingsRequest request)
    {
        RequireAdmin(userId);
        if (request.RelatedMinimum is null && request.Calibrated is null && request.EmbeddingServiceBaseUrl is null)
        {
            throw new SearchInputException("invalidRequest", "No settings were provided.");
        }

        if (request.EmbeddingServiceBaseUrl is not null)
        {
            if (request.EmbeddingServiceBaseUrl.Length == 0)
            {
                _store.SetEmbeddingBaseUrl("");
            }
            else if (!HttpEmbeddingClient.TryUri(request.EmbeddingServiceBaseUrl, "/", out _))
            {
                throw new SearchInputException("invalidRequest", "Embedding service URL must be an absolute http(s) URL without credentials.");
            }
            else
            {
                _store.SetEmbeddingBaseUrl(request.EmbeddingServiceBaseUrl.TrimEnd('/'));
            }
        }

        if (request.RelatedMinimum is not null || request.Calibrated is not null)
        {
            var minimum = request.RelatedMinimum ?? _store.RelatedMinimum;
            var calibrated = request.Calibrated ?? _store.ThresholdCalibrated;
            try
            {
                _store.SetQuality(minimum, calibrated);
            }
            catch (ArgumentOutOfRangeException)
            {
                throw new SearchInputException("invalidRequest", "relatedMinimum must be between -1 and 1.");
            }
        }
    }

    public void Pause(Guid userId)
    {
        RequireAdmin(userId);
        _store.SetManualPause(true);
    }

    public void Resume(Guid userId)
    {
        RequireAdmin(userId);
        _store.SetManualPause(false);
    }

    public async Task<RetryFailedResponse> RetryFailedAsync(Guid userId, CancellationToken cancellationToken)
    {
        RequireAdmin(userId);
        if (_store.ImportedCandidatesOnly)
        {
            throw new SearchInputException("importedCandidatesOnly", "Imported candidates are not expanded into full-video windows.");
        }

        if (!HostCompatibility.IsSupported(_hostVersion) || _store.SchemaReadOnly || !_store.HasWriterLock)
        {
            var code = !HostCompatibility.IsSupported(_hostVersion) ? "hostIncompatible" : _store.SchemaReadOnly ? "schemaNewer" : "writerLocked";
            throw new SearchInputException(code, "Retry is refused while this host cannot write the index.");
        }

        var generation = _store.ActiveGeneration;
        var generations = _store.GenerationCount();
        var paused = _store.ManualPause;
        var reset = 0;
        foreach (var failed in _store.ListFailedForRetry())
        {
            CatalogItem facts;
            try
            {
                facts = _facts.ReadSource(failed.ItemId, failed.MediaSourceId, _store.EnabledLibraryIds());
            }
            catch (Exception)
            {
                _store.MarkUnknown(failed.ItemId);
                continue;
            }

            if (facts.Presence == ItemPresence.Unreadable)
            {
                _store.MarkUnknown(failed.ItemId);
                continue;
            }

            if (facts.Presence is ItemPresence.Missing or ItemPresence.OutOfScope || facts.Video is null || facts.Video.MediaSourceId != failed.MediaSourceId)
            {
                _store.HideItem(failed.ItemId, failed.MediaSourceId);
                continue;
            }

            if (facts.Video.ContentVersion != failed.ContentVersion)
            {
                _store.UpsertVideo(facts.Video.ItemId, facts.Video.MediaSourceId, facts.Video.LibraryId, facts.Video.RuntimeTicks, facts.Video.ContentVersion, facts.Video.SourcePath, facts.Video.HasAudio, facts.Video.AudioStreamIndex);
                continue;
            }

            reset += _store.ResetFailed(failed.ItemId, failed.MediaSourceId, failed.ContentVersion);
        }

        return new RetryFailedResponse
        {
            Accepted = true,
            ResetWindows = reset,
            ActiveGeneration = _store.ActiveGeneration,
            GenerationUnchanged = _store.ActiveGeneration == generation && _store.GenerationCount() == generations,
            ManualPause = paused && _store.ManualPause,
            ReindexStarted = false,
            Status = await AdminStatusAsync(userId, cancellationToken).ConfigureAwait(false)
        };
    }

    public async Task<RebuildResponse> RebuildAsync(Guid userId, CancellationToken cancellationToken)
    {
        RequireAdmin(userId);
        if (_store.ImportedCandidatesOnly)
        {
            throw new SearchInputException("importedCandidatesOnly", "Imported candidates are not expanded into full-video windows.");
        }

        if (!HostCompatibility.IsSupported(_hostVersion) || _store.SchemaReadOnly)
        {
            throw new SearchInputException(_store.SchemaReadOnly ? "schemaNewer" : "hostIncompatible", "Rebuild is refused while the index cannot be written for this host.");
        }

        var before = _store.WindowCount();
        var generations = _store.GenerationCount();
        var observation = await _embeddings.ObserveAsync(_store.EmbeddingBaseUrl, cancellationToken).ConfigureAwait(false);
        if (!observation.Reachable || !observation.Ready)
        {
            throw new SearchInputException("modelOffline", "The embedding service is not ready. Stored windows were kept.");
        }

        if (!observation.IsExpectedModel)
        {
            _store.SetIncompatible(true);
            throw new SearchInputException("incompatible", "The embedding service signature is not the expected model. Stored windows were kept.");
        }

        var active = _store.ActiveSignature();
        if (active is not null && IndexStore.SignaturesMatch(active, observation) && !_store.Incompatible)
        {
            throw new SearchInputException("alreadyCompatible", "The stored signature already matches the service. Nothing was deleted.");
        }

        var generation = active is null
            ? CreateFirst(observation)
            : _store.BeginRebuild(observation);
        _store.RequestReconcile();
        _embeddings.NoteSignatureRenewed();
        if (_store.WindowCount() < before || _store.GenerationCount() < generations)
        {
            throw new InvalidOperationException("Rebuild lost stored index data.");
        }

        return new RebuildResponse
        {
            Accepted = true,
            OldGenerationRetained = true,
            ReindexStarted = true,
            ActiveGeneration = generation,
            Status = BuildAdmin(observation)
        };
    }

    private int CreateFirst(ModelObservation observation)
    {
        _store.EnsureActiveSignature(observation);
        return _store.ActiveGeneration;
    }

    private async Task<ModelObservation> ObserveIfConfigured(CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(_store.EmbeddingBaseUrl))
        {
            return new ModelObservation { ErrorCode = "notConfigured" };
        }

        try
        {
            return await _embeddings.ObserveAsync(_store.EmbeddingBaseUrl, cancellationToken).ConfigureAwait(false);
        }
        catch (HttpRequestException)
        {
            return new ModelObservation { ErrorCode = "modelOffline" };
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return new ModelObservation { ErrorCode = "cancelled" };
        }
        catch (OperationCanceledException)
        {
            return new ModelObservation { ErrorCode = "timeout" };
        }
    }

    private void ReconcileCompatibility(ModelObservation observation, int? generationAtStart)
    {
        var active = _store.ActiveSignature();
        if (active is null || generationAtStart is null || active.Generation != generationAtStart || _store.SchemaReadOnly || !_store.HasWriterLock)
        {
            return;
        }

        if (!observation.Reachable)
        {
            return;
        }

        if (IndexStore.SignaturesMatch(active, observation))
        {
            if (_store.Incompatible)
            {
                _store.SetIncompatible(false);
            }

            return;
        }

        if (!_store.Incompatible)
        {
            _store.SetIncompatible(true);
        }
    }

    private CapabilitiesResponse BuildCapabilities(MediaPrincipal principal, ModelObservation observation)
    {
        var libraries = VisibleEnabledLibraries(principal, null);
        var coverage = _store.Coverage(libraries);
        var active = _store.ActiveSignature();
        var availability = CurrentAvailability(libraries.Count, active is not null);
        var candidateCount = 0;
        try
        {
            var candidates = _store.ReadCandidateSnapshot();
            if (candidates.Representations.Length > 0 && availability.Search == "available")
            {
                if (!observation.Reachable || !observation.Ready)
                {
                    availability = AvailabilityForTransport(observation.ErrorCode);
                }
                else
                {
                    candidateCount = candidates.Representations.SelectMany(r => r.Candidates).Count(row =>
                        libraries.Contains(row.LibraryId) && principal.CanAccess(row.ItemId, row.MediaSourceId)
                        && _store.ReadStoredContentVersion(row.ItemId, row.MediaSourceId) == row.ContentVersion
                        && FactsMatch(row.ItemId, row.MediaSourceId, row.ContentVersion, libraries));
                }
            }
        }
        catch (Exception e) when (e is InvalidDataException or System.Text.Json.JsonException)
        {
            availability = Availability("incompatible", "incompatible");
        }

        var response = new CapabilitiesResponse
        {
            Availability = availability,
            Compatibility = new CompatibilityBody
            {
                HostVersion = _hostVersionText,
                HostCompatible = HostCompatibility.IsSupported(_hostVersion),
                ProcessingId = active?.ProcessingId,
                IndexSignatureMatches = active is null || !observation.Reachable || IndexStore.SignaturesMatch(active, observation)
            },
            Coverage = new CoverageBody
            {
                CandidatesSearchable = candidateCount,
                Libraries = libraries.Select(id => new LibraryCoverageBody
                {
                    LibraryId = id,
                    Name = _libraries.ListLibraries().FirstOrDefault(library => library.Id == id)?.Name ?? ""
                }).ToList(),
                VideosTotal = coverage.Videos,
                VideosComplete = coverage.Complete,
                VideosPartial = coverage.Partial,
                VideosFailed = coverage.Failed,
                VideosWaiting = coverage.Waiting,
                WindowsSearchable = _store.Incompatible ? 0 : coverage.Searchable,
                IndexComplete = !_store.Incompatible && coverage.IndexComplete
            },
            QualityPolicy = Quality()
        };
        WindowContract.Apply(response.Compatibility, response.Window, active?.WindowVersion);
        response.Features = [ApiV1.GroupedVideoFeature, ApiV1.CollapsedRunsFeature, ApiV1.SimilarSegmentsFeature];
        return response;
    }

    private AdminStatusResponse BuildAdmin(ModelObservation observation)
    {
        var active = _store.ActiveSignature();
        var enabled = _store.EnabledLibraryIds().ToHashSet();
        var coverage = _store.Coverage(enabled);
        return new AdminStatusResponse
        {
            Pause = Pause(),
            Model = new ModelStatusBody
            {
                Configured = !string.IsNullOrWhiteSpace(_store.EmbeddingBaseUrl),
                Reachable = observation.Reachable,
                Ready = observation.Ready,
                ProcessingId = observation.ProcessingId,
                Dimension = observation.Dimension == 0 ? ApiV1.Dimension : observation.Dimension,
                Normalized = observation.Dimension == 0 || observation.Normalized,
                ErrorCode = observation.ErrorCode
            },
            Signature = new SignatureStatusBody
            {
                ActiveGeneration = active?.Generation,
                ProcessingId = active?.ProcessingId,
                ServiceMatchesActive = active is not null && IndexStore.SignaturesMatch(active, observation)
            },
            Libraries = _libraries.ListLibraries().Select(library => new AdminLibraryBody
            {
                LibraryId = library.Id,
                Name = library.Name,
                Enabled = enabled.Contains(library.Id)
            }).ToList(),
            Progress = new ProgressBody
            {
                VideosComplete = coverage.Complete,
                VideosWaiting = coverage.Waiting,
                VideosPartial = coverage.Partial,
                VideosFailed = coverage.Failed,
                WindowsRetained = _store.WindowCount(),
                GenerationsRetained = _store.GenerationCount()
            },
            Thresholds = Quality(),
            ActiveGeneration = _store.ActiveGeneration,
            HostCompatible = HostCompatibility.IsSupported(_hostVersion)
        };
    }

    private AvailabilityBody CurrentAvailability(int visibleLibraries, bool hasSignature)
    {
        try
        {
            var candidates = _store.ReadCandidateSnapshot();
            var signature = _store.ActiveSignature();
            if (candidates.Representations.Any(r => signature is null || !r.Matches(signature)))
            {
                return Availability("incompatible", "incompatible");
            }
        }
        catch (Exception e) when (e is InvalidDataException or System.Text.Json.JsonException)
        {
            return Availability("incompatible", "incompatible");
        }

        if (!HostCompatibility.IsSupported(_hostVersion))
        {
            return Availability("hostIncompatible", "hostIncompatible");
        }

        if (_store.SchemaReadOnly)
        {
            return Availability("schemaNewer", "schemaNewer");
        }

        if (!_store.HasWriterLock)
        {
            return Availability("writerLocked", "writerLocked");
        }

        if (_store.Incompatible)
        {
            return Availability("incompatible", "incompatible");
        }

        if (visibleLibraries == 0)
        {
            return Availability("empty", "noLibraries");
        }

        if (!hasSignature)
        {
            return Availability("empty", "none");
        }

        return Availability("available", "none");
    }

    private AvailabilityBody AvailabilityForTransport(string? code)
    {
        var (search, reason) = code switch
        {
            "cancelled" => ("cancelled", "cancelled"),
            "timeout" => ("timeout", "timeout"),
            "queueFull" or "queue_full" => ("queueFull", "queueFull"),
            _ => ("modelOffline", "modelOffline")
        };
        return Availability(search, reason);
    }

    private AvailabilityBody Availability(string search, string reason)
    {
        var indexingReasons = new List<string>();
        if (!HostCompatibility.IsSupported(_hostVersion))
        {
            indexingReasons.Add("hostIncompatible");
        }

        if (_store.SchemaReadOnly)
        {
            indexingReasons.Add("schemaNewer");
        }

        if (!_store.HasWriterLock)
        {
            indexingReasons.Add("writerLocked");
        }

        if (_store.Incompatible)
        {
            indexingReasons.Add("incompatible");
        }

        if (_store.ManualPause)
        {
            indexingReasons.Add("manual");
        }

        if (_store.ImportedCandidatesOnly)
        {
            indexingReasons.Add("importedCandidatesOnly");
        }

        if (_videoPlayback())
        {
            indexingReasons.Add("playback");
        }

        if (_store.EnabledLibraryIds().Count == 0)
        {
            indexingReasons.Add("noLibraries");
        }

        var indexing = indexingReasons.Contains("hostIncompatible") || indexingReasons.Contains("schemaNewer") || indexingReasons.Contains("writerLocked") || indexingReasons.Contains("incompatible")
            ? "blocked"
            : indexingReasons.Contains("manual") || indexingReasons.Contains("playback") || indexingReasons.Contains("importedCandidatesOnly") ? "paused" : "idle";
        return new AvailabilityBody
        {
            Search = search,
            SearchReason = reason,
            Indexing = indexing,
            IndexingReasons = indexingReasons
        };
    }

    private PauseBody Pause()
    {
        var reasons = new List<string>();
        if (_store.ManualPause)
        {
            reasons.Add("manual");
        }

        if (_store.ImportedCandidatesOnly)
        {
            reasons.Add("importedCandidatesOnly");
        }

        var playback = _videoPlayback();
        if (playback)
        {
            reasons.Add("playback");
        }

        if (_store.Incompatible)
        {
            reasons.Add("incompatible");
        }

        if (_store.SchemaReadOnly)
        {
            reasons.Add("schemaNewer");
        }

        if (!_store.HasWriterLock)
        {
            reasons.Add("writerLocked");
        }

        return new PauseBody
        {
            Manual = _store.ManualPause,
            Playback = playback,
            Incompatible = _store.Incompatible,
            SchemaReadOnly = _store.SchemaReadOnly,
            WriterLocked = !_store.HasWriterLock,
            Reasons = reasons
        };
    }

    private QualityPolicyBody Quality()
    {
        try
        {
            var candidates = _store.ReadCandidateSnapshot();
            if (candidates.Representations.Length > 0)
            {
                return CandidateQuality(candidates);
            }
        }
        catch (Exception e) when (e is InvalidDataException or System.Text.Json.JsonException)
        {
            return CandidateQuality(new CandidateSnapshot());
        }

        return new QualityPolicyBody { RelatedMinimum = _store.RelatedMinimum, Calibrated = _store.ThresholdCalibrated };
    }

    private bool FactsMatch(Guid itemId, string mediaSourceId, string contentVersion, IReadOnlyCollection<Guid> libraries)
    {
        var facts = ReadFacts(itemId, mediaSourceId, libraries);
        return facts.Presence == ItemPresence.Readable
            && facts.Video is not null
            && facts.Video.MediaSourceId == mediaSourceId
            && facts.Video.ContentVersion == contentVersion;
    }

    private CatalogItem ReadFacts(Guid itemId, string mediaSourceId, IReadOnlyCollection<Guid> libraries)
    {
        try
        {
            return _facts.ReadSource(itemId, mediaSourceId, libraries);
        }
        catch (Exception)
        {
            return new CatalogItem { Presence = ItemPresence.Unreadable };
        }
    }

    private Task<EmbeddingCall?> ValidateBeforeQuery(Guid userId, SearchRequest request, StoredSignature snapshot)
    {
        var principal = RequirePrincipal(userId);
        if (_store.Incompatible)
        {
            return Task.FromResult<EmbeddingCall?>(new EmbeddingCall { ErrorCode = "incompatible" });
        }

        var active = _store.ActiveSignature();
        if (active is null || active.Generation != snapshot.Generation)
        {
            return Task.FromResult<EmbeddingCall?>(new EmbeddingCall { ErrorCode = "generationChanged" });
        }

        if (SelectLibraries(principal, request).Libraries.Count == 0)
        {
            return Task.FromResult<EmbeddingCall?>(new EmbeddingCall { ErrorCode = "noLibraries" });
        }

        return Task.FromResult<EmbeddingCall?>(null);
    }

    private (List<Guid> Libraries, Guid? Container) SelectLibraries(MediaPrincipal principal, SearchRequest request)
    {
        var libraries = VisibleEnabledLibraries(principal, request.LibraryIds);
        if (request.ParentId is not Guid parent || parent == Guid.Empty)
        {
            return (libraries, null);
        }

        var binding = _libraries.ResolveParent(parent);
        if (!binding.Known)
        {
            return ([], null);
        }

        if (binding.IsLibrary)
        {
            return (libraries.Contains(parent) ? [parent] : [], null);
        }

        return (libraries, parent);
    }

    private List<Guid> VisibleEnabledLibraries(MediaPrincipal principal, Guid[]? requested)
    {
        var enabled = _store.EnabledLibraryIds().Where(principal.CanSeeLibrary).ToHashSet();
        if (requested is null || requested.Length == 0)
        {
            return enabled.ToList();
        }

        return requested.Where(enabled.Contains).Distinct().ToList();
    }

    private MediaPrincipal RequirePrincipal(Guid userId)
    {
        if (userId == Guid.Empty || !_access.TryGetUser(userId, out var principal) || principal is null)
        {
            throw new SearchAuthException(401, "unauthorized", "A signed-in user is required.");
        }

        if (principal.IsDisabled)
        {
            throw new SearchAuthException(403, "forbidden", "This user is not allowed to search.");
        }

        return principal;
    }

    private void RequireAdmin(Guid userId)
    {
        var principal = RequirePrincipal(userId);
        if (!principal.IsAdministrator)
        {
            throw new SearchAuthException(403, "forbidden", "An administrator is required.");
        }
    }

    private static string? CleanClientQueryId(string? value)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > 80)
        {
            return null;
        }

        foreach (var character in value)
        {
            if (char.IsControl(character))
            {
                return null;
            }
        }

        return value;
    }
}

public sealed class SearchAuthException : Exception
{
    public SearchAuthException(int status, string code, string message) : base(message)
    {
        Status = status;
        Code = code;
    }

    public int Status { get; }
    public string Code { get; }
}

public sealed class SearchInputException : Exception
{
    public SearchInputException(string code, string message) : base(message)
    {
        Code = code;
    }

    public string Code { get; }
}
