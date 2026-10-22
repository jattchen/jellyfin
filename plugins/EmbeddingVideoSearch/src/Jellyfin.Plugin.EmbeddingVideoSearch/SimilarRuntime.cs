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
        var snapshot = ReadCandidatesOrIncompatible();
        var located = FindCandidate(snapshot, request.SegmentId);
        if (located is null)
        {
            throw NotFound();
        }

        var selection = SelectLibraries(principal, new SearchRequest { ParentId = request.ParentId });
        var referenceRow = located.Value.Row;
        if (!SourceVisible(principal, selection, referenceRow))
        {
            throw NotFound();
        }

        var signature = _store.ActiveSignature();
        RequireCompatible(snapshot, signature);
        if (!SourceCurrent(referenceRow, selection.Libraries))
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
        var ranked = RankJoint(joint, referenceRow, source, principal, selection);
        cancellationToken.ThrowIfCancellationRequested();

        principal = RequirePrincipal(userId);
        var fresh = ReadCandidatesOrIncompatible();
        var active = _store.ActiveSignature();
        if (active is null || active.Generation != generation || fresh.Identity != snapshot.Identity)
        {
            throw Incompatible();
        }

        RequireCompatible(fresh, active);
        selection = SelectLibraries(principal, new SearchRequest { ParentId = request.ParentId });
        var freshReference = FindCandidate(fresh, request.SegmentId);
        if (freshReference is null || !SourceVisible(principal, selection, freshReference.Value.Row))
        {
            throw NotFound();
        }

        if (!SourceCurrent(freshReference.Value.Row, selection.Libraries))
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

        var allowed = freshJoint.Candidates
            .Where(row => !JointSource.Excluded(row, freshReference.Value.Row, freshSource))
            .Where(row => SourceVisible(principal, selection, row) && SourceCurrent(row, selection.Libraries))
            .Select(row => row.SegmentId)
            .ToHashSet();
        ranked.RemoveAll(hit => !allowed.Contains(hit.SegmentId));
        var selected = request.GroupByVideo
            ? CandidateRanking.TakeByVideo(ranked, limit, ApiV1.GroupedHitsPerVideo)
            : ranked.Take(limit).ToList();
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
        MediaPrincipal principal, (List<Guid> Libraries, Guid? Container) selection)
    {
        var hits = new List<SearchHit>();
        foreach (var row in joint.Candidates)
        {
            if (JointSource.Excluded(row, reference, source) || !SourceVisible(principal, selection, row) || !SourceCurrent(row, selection.Libraries))
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
        ApplyStoredCoverage(hits);
        return hits;
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
