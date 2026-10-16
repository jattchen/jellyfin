namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed partial class SearchRuntime
{
    private SearchResponse SearchCandidates(Guid userId, SearchRequest request, float[] query,
        CandidateSnapshot candidates, StoredSignature signature, int limit, SearchResponse response)
    {
        var principal = RequirePrincipal(userId);
        var selection = SelectLibraries(principal, request);
        bool Allowed(CandidateRow row) => selection.Libraries.Contains(row.LibraryId)
            && principal.CanAccess(row.ItemId, row.MediaSourceId)
            && (selection.Container is not Guid parent || _libraries.ItemBelongsToParent(row.ItemId, parent))
            && _store.ReadStoredContentVersion(row.ItemId, row.MediaSourceId) == row.ContentVersion
            && FactsMatch(row.ItemId, row.MediaSourceId, row.ContentVersion, selection.Libraries);

        var ranked = CandidateRanking.Rank(query, candidates, Allowed);
        // No await follows publication checks. Re-read permissions and versions after ranking.
        principal = RequirePrincipal(userId);
        selection = SelectLibraries(principal, request);
        var fresh = _store.ReadCandidateSnapshot();
        if (_store.ActiveGeneration != signature.Generation || fresh.Identity != candidates.Identity
            || fresh.Representations.Any(r => !r.Matches(signature)) || _store.Incompatible)
        {
            response.Availability = Availability("incompatible", "incompatible");
            return response;
        }

        var allowedIds = candidates.Representations.SelectMany(r => r.Candidates).Where(Allowed).Select(r => r.SegmentId).ToHashSet();
        ranked.RemoveAll(hit => !allowedIds.Contains(hit.SegmentId));
        ApplyStoredCoverage(ranked);
        response.QualityPolicy = CandidateQuality(candidates);
        response.Results = request.GroupByVideo
            ? CandidateRanking.TakeByVideo(ranked, limit, ApiV1.GroupedHitsPerVideo)
            : ranked.Take(limit).ToList();
        response.RawResults = request.Diagnostics ? ranked : null;
        response.IndexComplete = _store.Coverage(selection.Libraries).IndexComplete;
        response.Availability = Availability(ranked.Count == 0 ? "empty" : "available", selection.Libraries.Count == 0 ? "noLibraries" : "none");
        return response;
    }

    private SegmentDecision? GetCandidateSegment(Guid userId, Guid segmentId)
    {
        CandidateSnapshot snapshot;
        try
        {
            snapshot = _store.ReadCandidateSnapshot();
        }
        catch (Exception e) when (e is InvalidDataException or System.Text.Json.JsonException)
        {
            return SegmentDecision.Stale("incompatible");
        }

        foreach (var representation in snapshot.Representations)
        {
            var row = representation.Candidates.FirstOrDefault(c => c.SegmentId == segmentId);
            if (row is null)
            {
                continue;
            }

            var principal = RequirePrincipal(userId);
            var libraries = VisibleEnabledLibraries(principal, null);
            if (!libraries.Contains(row.LibraryId) || !principal.CanAccess(row.ItemId, row.MediaSourceId))
            {
                return SegmentDecision.NotFound();
            }

            var signature = _store.ActiveSignature();
            if (!HostCompatibility.IsSupported(_hostVersion) || _store.SchemaReadOnly || _store.Incompatible
                || signature is null || !representation.Matches(signature))
            {
                return SegmentDecision.Stale("incompatible");
            }

            if (_store.ReadStoredContentVersion(row.ItemId, row.MediaSourceId) != row.ContentVersion
                || !FactsMatch(row.ItemId, row.MediaSourceId, row.ContentVersion, libraries))
            {
                return SegmentDecision.Stale("stale");
            }

            return SegmentDecision.Ok(new SegmentDetailResponse
            {
                ItemId = row.ItemId, MediaSourceId = row.MediaSourceId, SegmentId = row.SegmentId,
                StartTicks = row.StartTicks, EndTicks = row.EndTicks,
                IndexComplete = _store.ReadMediaState(row.ItemId, row.MediaSourceId) == "complete",
                Score = 0, Quality = "candidate", CandidateId = row.CandidateId, Canonical = row.Canonical,
                Representation = representation.Channel == "joint" ? "jointAudioVideo" : "transcriptText",
                Transcript = string.IsNullOrWhiteSpace(row.Transcript) ? null : row.Transcript,
                BindingVersion = SegmentBinding.Version(row.SegmentId, row.ItemId, row.MediaSourceId, row.ContentVersion, row.StartTicks, row.EndTicks, representation.Channel == "joint" ? "jointAudioVideo" : "transcriptText", row.CandidateId, representation.Version)
            });
        }

        return null;
    }

    private void ApplyStoredCoverage(IEnumerable<SearchHit> hits)
    {
        foreach (var hit in hits)
        {
            hit.IndexComplete = _store.ReadMediaState(hit.ItemId, hit.MediaSourceId) == "complete";
        }
    }

    private static QualityPolicyBody CandidateQuality(CandidateSnapshot snapshot) => new()
    {
        RelatedMinimum = null,
        Calibrated = false,
        ScoreMeaning = CandidateRanking.ScoreMeaning,
        RankingVersion = CandidateRanking.Version,
        Representations = snapshot.Representations.ToDictionary(
            r => r.Channel == "joint" ? "jointAudioVideo" : "transcriptText",
            r => r.Version)
    };
}
