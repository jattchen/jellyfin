namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed partial class SearchRuntime
{
    private const string SimilarNotFound = "Segment was not found.";

    /// <summary>
    /// Ranks stored joint audio-video vectors around one clicked candidate.
    /// The text vector is never the query, and the embedding service is not called.
    /// </summary>
    public SimilarResponse Similar(Guid userId, SimilarRequest request, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var principal = RequirePrincipal(userId);
        if (request.SegmentId == Guid.Empty)
        {
            throw new SearchInputException("invalidRequest", "segmentId is required.");
        }

        RequireBindingStamp(request.ExpectedBindingVersion);
        var limit = ResolveSimilarLimit(request.GroupByVideo, request.Limit);
        cancellationToken.ThrowIfCancellationRequested();
        var snapshot = ReadCandidatesOrIncompatible();
        cancellationToken.ThrowIfCancellationRequested();
        var located = FindCandidate(snapshot, request.SegmentId);
        if (located is null)
        {
            throw NotFound();
        }

        var selection = SelectLibraries(principal, new SearchRequest { ParentId = request.ParentId });
        var referenceRow = located.Value.Row;
        var probe = new SourceProbe(this, principal, selection, cancellationToken);
        if (!probe.Visible(referenceRow))
        {
            throw NotFound();
        }

        var signature = _store.ActiveSignature();
        RequireCompatible(snapshot, signature);
        if (!probe.Current(referenceRow))
        {
            throw Stale();
        }

        var referenceName = RepresentationName(located.Value.Representation);
        var referenceBinding = Binding(referenceRow, referenceName, located.Value.Representation.Version);
        if (!string.Equals(referenceBinding, request.ExpectedBindingVersion, StringComparison.Ordinal))
        {
            throw new SearchInputException("bindingMismatch", "The media binding does not match.");
        }

        var joint = snapshot.Representations.First(representation => representation.Channel == "joint");
        var source = JointSource.Select(referenceRow, referenceName, joint);
        if (source is null)
        {
            throw ReferenceUnavailable();
        }

        cancellationToken.ThrowIfCancellationRequested();
        var generation = signature!.Generation;
        var ranked = RankJoint(joint, referenceRow, source, probe, cancellationToken);
        cancellationToken.ThrowIfCancellationRequested();

        principal = RequirePrincipal(userId);
        cancellationToken.ThrowIfCancellationRequested();
        var fresh = ReadCandidatesOrIncompatible();
        cancellationToken.ThrowIfCancellationRequested();
        var active = _store.ActiveSignature();
        if (active is null || active.Generation != generation || fresh.Identity != snapshot.Identity)
        {
            throw Incompatible();
        }

        RequireCompatible(fresh, active);
        selection = SelectLibraries(principal, new SearchRequest { ParentId = request.ParentId });
        var freshProbe = new SourceProbe(this, principal, selection, cancellationToken);
        var freshReference = FindCandidate(fresh, request.SegmentId);
        if (freshReference is null || !freshProbe.Visible(freshReference.Value.Row))
        {
            throw NotFound();
        }

        if (!freshProbe.Current(freshReference.Value.Row))
        {
            throw Stale();
        }

        var freshName = RepresentationName(freshReference.Value.Representation);
        var freshBinding = Binding(freshReference.Value.Row, freshName, freshReference.Value.Representation.Version);
        if (!string.Equals(freshBinding, request.ExpectedBindingVersion, StringComparison.Ordinal))
        {
            throw new SearchInputException("bindingMismatch", "The media binding does not match.");
        }

        var freshJoint = fresh.Representations.First(representation => representation.Channel == "joint");
        var freshSource = JointSource.Select(freshReference.Value.Row, freshName, freshJoint);
        if (freshSource is null)
        {
            throw ReferenceUnavailable();
        }

        if (freshSource.SegmentId != source.SegmentId)
        {
            throw Incompatible();
        }

        var allowed = new HashSet<Guid>();
        foreach (var row in freshJoint.Candidates)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (JointSource.Excluded(row, freshReference.Value.Row, freshSource))
            {
                continue;
            }

            if (freshProbe.Visible(row) && freshProbe.Current(row))
            {
                allowed.Add(row.SegmentId);
            }
        }

        ranked.RemoveAll(hit => !allowed.Contains(hit.SegmentId));
        var selected = request.GroupByVideo
            ? CandidateRanking.TakeByVideo(ranked, limit, ApiV1.GroupedHitsPerVideo)
            : ranked.Take(limit).ToList();
        ApplyPageCoverage(selected, cancellationToken);
        if (request.CollapseRuns)
        {
            selected = RunCollapse.Apply(selected, fresh);
        }

        cancellationToken.ThrowIfCancellationRequested();
        var excluded = JointSource.ExcludedSpan(freshReference.Value.Row, freshSource);
        var coverage = _store.Coverage(selection.Libraries);
        return new SimilarResponse
        {
            QueryId = Guid.NewGuid(),
            ClientQueryId = CleanClientQueryId(request.ClientQueryId),
            Availability = Availability(selected.Count == 0 ? "empty" : "available", "none"),
            IndexComplete = coverage.IndexComplete,
            QualityPolicy = new QualityPolicyBody
            {
                RelatedMinimum = null,
                Calibrated = false,
                ScoreMeaning = ApiV1.DocumentCosineScoreMeaning,
                Representations = new Dictionary<string, string> { ["jointAudioVideo"] = freshJoint.Version }
            },
            Reference = new SimilarReferenceBody
            {
                SegmentId = freshReference.Value.Row.SegmentId,
                ItemId = freshReference.Value.Row.ItemId,
                MediaSourceId = freshReference.Value.Row.MediaSourceId,
                StartTicks = freshReference.Value.Row.StartTicks,
                EndTicks = freshReference.Value.Row.EndTicks,
                Representation = freshName,
                BindingVersion = freshBinding,
                Source = new SimilarSourceBody
                {
                    SegmentId = freshSource.SegmentId,
                    StartTicks = freshSource.StartTicks,
                    EndTicks = freshSource.EndTicks,
                    Representation = "jointAudioVideo",
                    BindingVersion = Binding(freshSource, "jointAudioVideo", freshJoint.Version)
                },
                Excluded = new SimilarSpanBody { StartTicks = excluded.StartTicks, EndTicks = excluded.EndTicks }
            },
            Results = selected
        };
    }

    private List<SearchHit> RankJoint(CandidateRepresentation joint, CandidateRow reference, CandidateRow source,
        SourceProbe probe, CancellationToken cancellationToken)
    {
        var hits = new List<SearchHit>();
        foreach (var row in joint.Candidates)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (JointSource.Excluded(row, reference, source) || !probe.Visible(row) || !probe.Current(row))
            {
                continue;
            }

            var score = CandidateRanking.Dot(source.Vector, row.Vector);
            // The page reads these rows as ordinary candidates and rejects a negative score for the whole response.
            if (!double.IsFinite(score) || score < 0)
            {
                continue;
            }

            hits.Add(new SearchHit
            {
                ItemId = row.ItemId,
                MediaSourceId = row.MediaSourceId,
                SegmentId = row.SegmentId,
                StartTicks = row.StartTicks,
                EndTicks = row.EndTicks,
                Score = score,
                RawScore = score,
                Quality = "candidate",
                Representation = "jointAudioVideo",
                CandidateId = row.CandidateId,
                AnchorId = row.AnchorId,
                Canonical = row.Canonical,
                RepresentationVersion = joint.Version,
                ContentVersion = row.ContentVersion,
                Transcript = string.IsNullOrWhiteSpace(row.Transcript) ? null : row.Transcript,
                BindingVersion = Binding(row, "jointAudioVideo", joint.Version)
            });
        }

        hits.Sort(static (left, right) =>
        {
            var byScore = right.Score.CompareTo(left.Score);
            if (byScore != 0)
            {
                return byScore;
            }

            var byAnchor = string.Compare(left.AnchorId, right.AnchorId, StringComparison.Ordinal);
            return byAnchor != 0 ? byAnchor : left.SegmentId.CompareTo(right.SegmentId);
        });
        return hits;
    }

    private void ApplyPageCoverage(IReadOnlyList<SearchHit> hits, CancellationToken cancellationToken)
    {
        var complete = new Dictionary<(Guid Item, string Source), bool>();
        foreach (var hit in hits)
        {
            var key = (hit.ItemId, hit.MediaSourceId);
            if (!complete.TryGetValue(key, out var ready))
            {
                cancellationToken.ThrowIfCancellationRequested();
                ready = _store.ReadMediaState(hit.ItemId, hit.MediaSourceId) == "complete";
                cancellationToken.ThrowIfCancellationRequested();
                complete[key] = ready;
            }

            hit.IndexComplete = ready;
        }
    }

    /// <summary>
    /// Remembers one pass only. Publication builds another probe, so an allow from ranking cannot hide a later revocation.
    /// The key includes the content version and the library so two sources, or two versions of one source, cannot share a verdict.
    /// </summary>
    private sealed class SourceProbe
    {
        private readonly SearchRuntime _runtime;
        private readonly MediaPrincipal _principal;
        private readonly (List<Guid> Libraries, Guid? Container) _selection;
        private readonly CancellationToken _cancellation;
        private readonly Dictionary<(Guid Item, string Source, Guid Library, string Version), Verdict> _verdicts = new();

        public SourceProbe(SearchRuntime runtime, MediaPrincipal principal, (List<Guid> Libraries, Guid? Container) selection, CancellationToken cancellation)
        {
            _runtime = runtime;
            _principal = principal;
            _selection = selection;
            _cancellation = cancellation;
        }

        public bool Visible(CandidateRow row)
        {
            _cancellation.ThrowIfCancellationRequested();
            return Resolve(row).Visible;
        }

        public bool Current(CandidateRow row)
        {
            _cancellation.ThrowIfCancellationRequested();
            var verdict = Resolve(row);
            if (!verdict.Visible || verdict.CurrentKnown)
            {
                return verdict.Visible && verdict.Current;
            }

            _cancellation.ThrowIfCancellationRequested();
            var stored = _runtime._store.ReadStoredContentVersion(row.ItemId, row.MediaSourceId);
            _cancellation.ThrowIfCancellationRequested();
            var current = stored == row.ContentVersion
                && _runtime.FactsMatch(row.ItemId, row.MediaSourceId, row.ContentVersion, _selection.Libraries);
            _cancellation.ThrowIfCancellationRequested();
            verdict.Current = current;
            verdict.CurrentKnown = true;
            return verdict.Current;
        }

        private Verdict Resolve(CandidateRow row)
        {
            var key = (row.ItemId, row.MediaSourceId, row.LibraryId, row.ContentVersion);
            if (_verdicts.TryGetValue(key, out var verdict))
            {
                return verdict;
            }

            _cancellation.ThrowIfCancellationRequested();
            verdict = new Verdict { Visible = _runtime.SourceVisible(_principal, _selection, row) };
            _cancellation.ThrowIfCancellationRequested();
            _verdicts[key] = verdict;
            return verdict;
        }

        private sealed class Verdict
        {
            public bool Visible { get; init; }
            public bool CurrentKnown { get; set; }
            public bool Current { get; set; }
        }
    }

    private CandidateSnapshot ReadCandidatesOrIncompatible()
    {
        try
        {
            return _store.ReadCandidateSnapshot();
        }
        catch (Exception exception) when (exception is InvalidDataException or System.Text.Json.JsonException)
        {
            throw Incompatible();
        }
    }

    private void RequireCompatible(CandidateSnapshot snapshot, StoredSignature? signature)
    {
        if (!HostCompatibility.IsSupported(_hostVersion) || _store.SchemaReadOnly || !_store.HasWriterLock
            || _store.Incompatible || signature is null || snapshot.Representations.Length == 0
            || snapshot.Representations.Any(representation => !representation.Matches(signature)))
        {
            throw Incompatible();
        }
    }

    private bool SourceVisible(MediaPrincipal principal, (List<Guid> Libraries, Guid? Container) selection, CandidateRow row)
    {
        if (!selection.Libraries.Contains(row.LibraryId) || !principal.CanAccess(row.ItemId, row.MediaSourceId))
        {
            return false;
        }

        return selection.Container is not Guid parent || _libraries.ItemBelongsToParent(row.ItemId, parent);
    }

    private bool SourceCurrent(CandidateRow row, IReadOnlyCollection<Guid> libraries)
        => _store.ReadStoredContentVersion(row.ItemId, row.MediaSourceId) == row.ContentVersion
           && FactsMatch(row.ItemId, row.MediaSourceId, row.ContentVersion, libraries);

    private static (CandidateRepresentation Representation, CandidateRow Row)? FindCandidate(CandidateSnapshot snapshot, Guid segmentId)
    {
        foreach (var representation in snapshot.Representations)
        {
            var row = representation.Candidates.FirstOrDefault(candidate => candidate.SegmentId == segmentId);
            if (row is not null)
            {
                return (representation, row);
            }
        }

        return null;
    }

    private static string RepresentationName(CandidateRepresentation representation)
        => representation.Channel == "joint" ? "jointAudioVideo" : "transcriptText";

    private static string Binding(CandidateRow row, string representation, string version)
        => SegmentBinding.Version(row.SegmentId, row.ItemId, row.MediaSourceId, row.ContentVersion, row.StartTicks, row.EndTicks, representation, row.CandidateId, version);

    private static int ResolveSimilarLimit(bool groupByVideo, int? requested)
    {
        if (groupByVideo)
        {
            var limit = requested ?? ApiV1.MaxGroupedVideos;
            if (limit < 1 || limit > ApiV1.MaxGroupedVideos)
            {
                throw new SearchInputException("invalidRequest", "Grouped search limit must be between 1 and 3 films.");
            }

            return limit;
        }

        var flat = requested ?? ApiV1.DefaultLimit;
        if (flat < 1 || flat > ApiV1.MaxLimit)
        {
            throw new SearchInputException("invalidRequest", "Limit must be between 1 and 50.");
        }

        return flat;
    }

    private static void RequireBindingStamp(string? value)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > 128 || value.Any(char.IsControl))
        {
            throw new SearchInputException("invalidRequest", "expectedBindingVersion is required.");
        }
    }

    private static SearchAuthException NotFound()
        => new(404, "notFound", SimilarNotFound);

    private static SearchInputException Stale()
        => new("stale", "Segment is no longer current.");

    private static SearchInputException Incompatible()
        => new("incompatible", "The index cannot serve this segment.");

    private static SearchInputException ReferenceUnavailable()
        => new("referenceUnavailable", "No joint audio-video window covers this segment.");
}
