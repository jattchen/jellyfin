namespace Jellyfin.Plugin.EmbeddingVideoSearch;

/// <summary>
/// Collapses one already limited result page. Overlap and endpoint contact are transitive,
/// but only inside that page: a lower-ranked neighbor outside the page cannot extend a run.
/// </summary>
public static class RunCollapse
{
    public static List<SearchHit> Apply(IReadOnlyList<SearchHit> selected, CandidateSnapshot? candidates)
    {
        var groups = new Dictionary<(Guid ItemId, string MediaSourceId, string ContentVersion), List<int>>();
        for (var index = 0; index < selected.Count; index++)
        {
            var hit = selected[index];
            var key = (hit.ItemId, hit.MediaSourceId, hit.ContentVersion ?? "");
            if (!groups.TryGetValue(key, out var members))
            {
                members = [];
                groups[key] = members;
            }

            members.Add(index);
        }

        var runs = new HitRun?[selected.Count];
        foreach (var members in groups.Values)
        {
            var byTime = members.OrderBy(index => selected[index].StartTicks)
                .ThenBy(index => selected[index].EndTicks)
                .ThenBy(index => index)
                .ToArray();
            var component = new List<int>();
            var componentEnd = 0L;
            void Emit()
            {
                if (component.Count == 0)
                {
                    return;
                }

                var best = component.Min();
                runs[best] = new HitRun
                {
                    StartTicks = component.Min(index => selected[index].StartTicks),
                    EndTicks = component.Max(index => selected[index].EndTicks),
                    MemberCount = component.Count
                };
                component.Clear();
            }

            foreach (var index in byTime)
            {
                var hit = selected[index];
                if (component.Count == 0)
                {
                    component.Add(index);
                    componentEnd = hit.EndTicks;
                    continue;
                }

                if (hit.StartTicks <= componentEnd)
                {
                    component.Add(index);
                    if (hit.EndTicks > componentEnd)
                    {
                        componentEnd = hit.EndTicks;
                    }
                }
                else
                {
                    Emit();
                    component.Add(index);
                    componentEnd = hit.EndTicks;
                }
            }

            Emit();
        }

        var collapsed = new List<SearchHit>(selected.Count);
        for (var index = 0; index < selected.Count; index++)
        {
            if (runs[index] is not HitRun run)
            {
                continue;
            }

            var copy = Copy(selected[index]);
            copy.Run = run;
            copy.SimilarAvailable = JointSource.Available(selected[index], candidates);
            collapsed.Add(copy);
        }

        return collapsed;
    }

    private static SearchHit Copy(SearchHit hit) => new()
    {
        ItemId = hit.ItemId,
        MediaSourceId = hit.MediaSourceId,
        SegmentId = hit.SegmentId,
        StartTicks = hit.StartTicks,
        EndTicks = hit.EndTicks,
        Score = hit.Score,
        RawScore = hit.RawScore,
        Representation = hit.Representation,
        CandidateId = hit.CandidateId,
        AnchorId = hit.AnchorId,
        Canonical = hit.Canonical is null ? null : new CanonicalAnchor
        {
            SourceItemId = hit.Canonical.SourceItemId,
            StartTicks = hit.Canonical.StartTicks,
            EndTicks = hit.Canonical.EndTicks
        },
        RepresentationVersion = hit.RepresentationVersion,
        ReferenceVersion = hit.ReferenceVersion,
        Quality = hit.Quality,
        IndexComplete = hit.IndexComplete,
        Transcript = hit.Transcript,
        BindingVersion = hit.BindingVersion,
        ContentVersion = hit.ContentVersion
    };
}

/// <summary>Chooses the stored joint window that a clicked candidate may use. Text vectors are never a source.</summary>
public static class JointSource
{
    public static long Overlap(long start, long end, long otherStart, long otherEnd)
    {
        var left = Math.Max(start, otherStart);
        var right = Math.Min(end, otherEnd);
        return right > left ? right - left : 0;
    }

    public static bool ContainsMidpoint(CandidateRow row, long midpoint)
        => row.StartTicks <= midpoint && midpoint < row.EndTicks;

    public static CandidateRow? Select(CandidateRow reference, string representation, CandidateRepresentation? joint)
    {
        if (joint is null)
        {
            return null;
        }

        if (representation == "jointAudioVideo")
        {
            return joint.Candidates.FirstOrDefault(row => row.SegmentId == reference.SegmentId
                && row.ItemId == reference.ItemId
                && row.MediaSourceId == reference.MediaSourceId
                && row.ContentVersion == reference.ContentVersion);
        }

        if (representation != "transcriptText")
        {
            return null;
        }

        var midpoint = reference.StartTicks + ((reference.EndTicks - reference.StartTicks) / 2);
        return joint.Candidates
            .Select(row => (Row: row, Overlap: Overlap(reference.StartTicks, reference.EndTicks, row.StartTicks, row.EndTicks)))
            .Where(item => item.Overlap > 0
                && item.Row.ItemId == reference.ItemId
                && item.Row.MediaSourceId == reference.MediaSourceId
                && item.Row.ContentVersion == reference.ContentVersion)
            .OrderByDescending(item => item.Overlap)
            .ThenByDescending(item => ContainsMidpoint(item.Row, midpoint))
            .ThenBy(item => item.Row.StartTicks)
            .ThenBy(item => item.Row.SegmentId.ToString("D"), StringComparer.Ordinal)
            .Select(item => item.Row)
            .FirstOrDefault();
    }

    public static bool Available(SearchHit hit, CandidateSnapshot? snapshot)
    {
        if (snapshot is null || hit.SegmentId == Guid.Empty)
        {
            return false;
        }

        CandidateRow? row = null;
        var representation = hit.Representation;
        foreach (var candidateRepresentation in snapshot.Representations)
        {
            var found = candidateRepresentation.Candidates.FirstOrDefault(candidate => candidate.SegmentId == hit.SegmentId);
            if (found is null)
            {
                continue;
            }

            row = found;
            representation ??= candidateRepresentation.Channel == "joint" ? "jointAudioVideo" : "transcriptText";
            break;
        }

        if (row is null)
        {
            return false;
        }

        var joint = snapshot.Representations.FirstOrDefault(candidateRepresentation => candidateRepresentation.Channel == "joint");
        return Select(row, representation ?? "", joint) is not null;
    }

    public static bool Excluded(CandidateRow row, CandidateRow reference, CandidateRow source)
    {
        if (row.SegmentId == reference.SegmentId || row.SegmentId == source.SegmentId)
        {
            return true;
        }

        if (row.ItemId != source.ItemId
            || row.MediaSourceId != source.MediaSourceId
            || row.ContentVersion != source.ContentVersion)
        {
            return false;
        }

        var start = Math.Min(reference.StartTicks, source.StartTicks);
        var end = Math.Max(reference.EndTicks, source.EndTicks);
        return row.StartTicks <= end && start <= row.EndTicks;
    }

    public static (long StartTicks, long EndTicks) ExcludedSpan(CandidateRow reference, CandidateRow source)
        => (Math.Min(reference.StartTicks, source.StartTicks), Math.Max(reference.EndTicks, source.EndTicks));
}
