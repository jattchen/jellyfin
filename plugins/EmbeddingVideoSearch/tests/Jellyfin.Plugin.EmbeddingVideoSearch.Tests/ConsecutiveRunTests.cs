using System.Text.Json;
using Microsoft.Data.Sqlite;
using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class ConsecutiveRunTests
{
    [Fact]
    public void Overlap_and_touch_collapse_only_inside_the_returned_page()
    {
        var film = Guid.NewGuid();
        var other = Guid.NewGuid();
        var ranked = new List<SearchHit>
        {
            Hit(film, "film", "v1", 100, 200, 9, "后一段更高"),
            Hit(film, "film", "v1", 0, 100, 8, "较早但更低"),
            Hit(film, "film", "v1", 200, 300, 7, null),
            Hit(film, "film", "v1", 1000, 1100, 6, "中间断开"),
            Hit(film, "other-source", "v1", 100, 200, 5, "另一条媒体源"),
            Hit(film, "film", "v2", 100, 200, 4, "另一内容版本"),
            Hit(other, "film", "v1", 100, 200, 3, "另一部影片")
        };

        var collapsed = RunCollapse.Apply(ranked, null);
        Assert.Equal(5, collapsed.Count);
        Assert.Equal([100L, 1000L, 100L, 100L, 100L], collapsed.Select(hit => hit.StartTicks).ToArray());
        Assert.Equal("后一段更高", collapsed[0].Transcript);
        Assert.Equal(100, collapsed[0].StartTicks);
        Assert.Equal(200, collapsed[0].EndTicks);
        var headRun = collapsed[0].Run;
        var gapRun = collapsed[1].Run;
        Assert.NotNull(headRun);
        Assert.NotNull(gapRun);
        Assert.Equal(0, headRun.StartTicks);
        Assert.Equal(300, headRun.EndTicks);
        Assert.Equal(3, headRun.MemberCount);
        Assert.Equal(1, gapRun.MemberCount);
        Assert.Equal(1000, collapsed[1].StartTicks);
        Assert.Equal("另一条媒体源", collapsed[2].Transcript);
        Assert.Equal("另一内容版本", collapsed[3].Transcript);
        Assert.Equal(other, collapsed[4].ItemId);
        Assert.All(collapsed, hit => Assert.False(hit.SimilarAvailable));
        Assert.All(ranked, hit => Assert.Null(hit.Run));
    }

    [Fact]
    public void A_one_tick_gap_stays_separate_and_equal_scores_keep_their_order()
    {
        var film = Guid.NewGuid();
        var first = Hit(film, "film", "v1", 0, 10, 1, null);
        var second = Hit(film, "film", "v1", 11, 20, 1, null);
        var collapsed = RunCollapse.Apply([first, second], null);
        Assert.Equal([first.SegmentId, second.SegmentId], collapsed.Select(hit => hit.SegmentId).ToArray());
        Assert.All(collapsed, hit => Assert.Equal(1, hit.Run?.MemberCount ?? 0));
    }

    [Fact]
    public void Joint_source_prefers_overlap_then_midpoint_then_earlier_start_then_id()
    {
        var item = Guid.NewGuid();
        var low = Guid.Parse("00000000-0000-0000-0000-000000000001");
        var high = Guid.Parse("00000000-0000-0000-0000-000000000002");
        var reference = Row(item, "v1", Guid.NewGuid(), 0, 100);
        var earlier = Row(item, "v1", Guid.NewGuid(), 0, 60);
        var later = Row(item, "v1", Guid.NewGuid(), 40, 100);
        var joint = Joint(earlier, later);
        Assert.Equal(earlier.SegmentId, JointSource.Select(reference, "transcriptText", joint)!.SegmentId);

        var misses = Row(item, "v1", Guid.NewGuid(), 0, 40);
        var contains = Row(item, "v1", Guid.NewGuid(), 40, 80);
        Assert.Equal(contains.SegmentId, JointSource.Select(reference, "transcriptText", Joint(misses, contains))!.SegmentId);

        var spoken = Row(item, "v1", Guid.NewGuid(), 100, 200);
        var small = Row(item, "v1", Guid.NewGuid(), 0, 150);
        var larger = Row(item, "v1", Guid.NewGuid(), 120, 400);
        Assert.Equal(larger.SegmentId, JointSource.Select(spoken, "transcriptText", Joint(small, larger))!.SegmentId);

        var sameStartLow = Row(item, "v1", low, 0, 100);
        var sameStartHigh = Row(item, "v1", high, 0, 100);
        Assert.Equal(low, JointSource.Select(reference, "transcriptText", Joint(sameStartHigh, sameStartLow))!.SegmentId);

        var otherVersion = Row(item, "replaced", Guid.NewGuid(), 0, 100);
        var weakSameVersion = Row(item, "v1", Guid.NewGuid(), 90, 190);
        Assert.Equal(weakSameVersion.SegmentId, JointSource.Select(reference, "transcriptText", Joint(otherVersion, weakSameVersion))!.SegmentId);
        Assert.Null(JointSource.Select(Row(item, "v1", Guid.NewGuid(), 500, 600), "transcriptText", joint));
        Assert.Equal(earlier.SegmentId, JointSource.Select(earlier, "jointAudioVideo", joint)!.SegmentId);
        Assert.Null(JointSource.Select(reference, "jointAudioVideo", joint));
    }

    [Fact]
    public void Mixed_representations_keep_the_representative_text_and_another_source_stays_apart()
    {
        var film = Guid.NewGuid();
        var spoken = Hit(film, "film", "v1", 100, 200, 9, "对白原文");
        spoken.Representation = "transcriptText";
        var picture = Hit(film, "film", "v1", 200, 300, 4, "画面旁白");
        var otherSource = Hit(film, "other", "v1", 100, 200, 8, "另一源原文");
        otherSource.Representation = "transcriptText";

        var collapsed = RunCollapse.Apply([spoken, picture, otherSource], null);
        Assert.Equal([spoken.SegmentId, otherSource.SegmentId], collapsed.Select(hit => hit.SegmentId).ToArray());
        Assert.Equal("对白原文", collapsed[0].Transcript);
        Assert.Equal("transcriptText", collapsed[0].Representation);
        Assert.Equal(100, collapsed[0].StartTicks);
        Assert.Equal(200, collapsed[0].EndTicks);
        var run = collapsed[0].Run;
        Assert.NotNull(run);
        Assert.Equal(100, run.StartTicks);
        Assert.Equal(300, run.EndTicks);
        Assert.Equal(2, run.MemberCount);
        Assert.Equal("另一源原文", collapsed[1].Transcript);
        Assert.Equal(1, collapsed[1].Run?.MemberCount ?? 0);
    }

    [Fact]
    public async Task Grouped_search_collapses_the_returned_page_and_leaves_the_old_response_unchanged()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        var films = new[] { Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid() };
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SetEmbeddingBaseUrl("http://synthetic.test");
        var joint = new List<CandidateRow>();
        var text = new List<CandidateRow>();
        void Add(Guid film, string channel, long start, long end, double strength, string? transcript, List<CandidateRow> target)
        {
            target.Add(new CandidateRow
            {
                SegmentId = Guid.NewGuid(),
                CandidateId = channel + ":" + film.ToString("N") + ":" + start,
                ItemId = film,
                MediaSourceId = "film",
                LibraryId = library,
                ContentVersion = "v1",
                StartTicks = start,
                EndTicks = end,
                RuntimeTicks = 100_000,
                Canonical = new CanonicalAnchor { SourceItemId = film, StartTicks = start, EndTicks = end },
                AnchorId = $"{film:N}:{start}:{end}",
                Transcript = transcript,
                Vector = Tilt(strength)
            });
        }

        Add(films[0], "joint", 0, 120, 0.99, null, joint);
        Add(films[0], "joint", 120, 240, 0.98, null, joint);
        Add(films[0], "joint", 240, 360, 0.97, null, joint);
        Add(films[0], "text", 5000, 5100, 0.96, "远处对白", text);
        Add(films[0], "joint", 8000, 8120, 0.95, null, joint);
        Add(films[0], "joint", 8120, 8240, 0.40, null, joint);
        Add(films[1], "joint", 0, 120, 0.90, null, joint);
        Add(films[2], "joint", 0, 120, 0.80, null, joint);
        foreach (var film in films)
        {
            fixture.Allow.Add(film);
            fixture.Store.SaveSucceededWindow(film, "film", library, "v1", 100_000, 0, 60, CoreTests.Axis(0), null);
        }

        Write(fixture, Bundle("joint", joint), Bundle("text", text));
        var calls = 0;
        fixture.Embeddings.Entered = () => calls++;
        var request = new SearchRequest { Query = "collapse-page", GroupByVideo = true, Limit = 3, Diagnostics = true };
        var plain = await fixture.Runtime.SearchAsync(fixture.UserId, request, CancellationToken.None);
        var collapsed = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest
        {
            Query = request.Query,
            GroupByVideo = true,
            Limit = 3,
            CollapseRuns = true,
            Diagnostics = true
        }, CancellationToken.None);

        Assert.Equal(1, calls);
        Assert.Equal("referenceTailR", plain.QualityPolicy.ScoreMeaning);
        Assert.Equal("referenceTailR", collapsed.QualityPolicy.ScoreMeaning);
        Assert.Equal(7, plain.Results.Count);
        Assert.DoesNotContain("\"run\"", JsonProtocol.Serialize(plain), StringComparison.Ordinal);
        Assert.DoesNotContain("similarAvailable", JsonProtocol.Serialize(plain), StringComparison.Ordinal);
        Assert.Equal(5, collapsed.Results.Count);
        var head = collapsed.Results[0];
        Assert.Equal(films[0], head.ItemId);
        Assert.Equal(0, head.StartTicks);
        Assert.Equal(120, head.EndTicks);
        Assert.Null(head.Transcript);
        var headRun = head.Run;
        Assert.NotNull(headRun);
        Assert.Equal(0, headRun.StartTicks);
        Assert.Equal(360, headRun.EndTicks);
        Assert.Equal(3, headRun.MemberCount);
        Assert.True(head.SimilarAvailable);
        var spoken = Assert.Single(collapsed.Results, hit => hit.Transcript == "远处对白");
        Assert.Equal(5000, spoken.StartTicks);
        Assert.Equal(5100, spoken.EndTicks);
        var spokenRun = spoken.Run;
        Assert.NotNull(spokenRun);
        Assert.Equal(1, spokenRun.MemberCount);
        Assert.False(spoken.SimilarAvailable);
        var distant = Assert.Single(collapsed.Results, hit => hit.ItemId == films[0] && hit.StartTicks == 8000);
        Assert.Equal(8120, distant.EndTicks);
        var distantRun = distant.Run;
        Assert.NotNull(distantRun);
        Assert.Equal(1, distantRun.MemberCount);
        Assert.Equal([films[0], films[1], films[2]], collapsed.Results.Select(hit => hit.ItemId).Distinct().ToArray());
        Assert.DoesNotContain(collapsed.Results, hit => hit.StartTicks == 8120);
        Assert.All(collapsed.RawResults!, hit => Assert.Null(hit.Run));
        Assert.True(collapsed.RawResults!.Count > collapsed.Results.Count);
        var capabilities = await fixture.Runtime.CapabilitiesAsync(fixture.UserId, CancellationToken.None);
        Assert.Contains(ApiV1.CollapsedRunsFeature, capabilities.Features);
        Assert.Contains(ApiV1.GroupedVideoFeature, capabilities.Features);
        Assert.Contains(ApiV1.SimilarSegmentsFeature, capabilities.Features);
    }

    [Fact]
    public async Task Window_search_does_not_pull_a_hit_past_the_limit_into_a_run()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SetEmbeddingBaseUrl("http://synthetic.test");
        fixture.Allow.Add(item);
        fixture.Store.SaveSucceededWindow(item, "film", library, "v1", 200_000_000, 0, 60_000_000, TiltFloat(0.9f), null);
        fixture.Store.SaveSucceededWindow(item, "film", library, "v1", 200_000_000, 60_000_000, 120_000_000, TiltFloat(0.6f), null);
        fixture.Store.SaveSucceededWindow(item, "film", library, "v1", 200_000_000, 120_000_000, 180_000_000, TiltFloat(0.5f), null);
        var calls = 0;
        fixture.Embeddings.Entered = () => calls++;
        var collapsed = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest
        {
            Query = "window-run",
            Limit = 2,
            CollapseRuns = true
        }, CancellationToken.None);
        Assert.Equal(1, calls);
        var only = Assert.Single(collapsed.Results);
        Assert.Equal(0, only.StartTicks);
        Assert.Equal(60_000_000, only.EndTicks);
        var run = only.Run;
        Assert.NotNull(run);
        Assert.Equal(0, run.StartTicks);
        Assert.Equal(120_000_000, run.EndTicks);
        Assert.Equal(2, run.MemberCount);
        Assert.False(only.SimilarAvailable);
        Assert.Equal(0.9, only.Score, 5);
    }

    private static SearchHit Hit(Guid item, string source, string version, long start, long end, double score, string? transcript) => new()
    {
        ItemId = item,
        MediaSourceId = source,
        ContentVersion = version,
        SegmentId = Guid.NewGuid(),
        StartTicks = start,
        EndTicks = end,
        Score = score,
        Transcript = transcript,
        Representation = "jointAudioVideo"
    };

    private static CandidateRow Row(Guid item, string version, Guid segment, long start, long end) => new()
    {
        ItemId = item,
        MediaSourceId = "film",
        ContentVersion = version,
        SegmentId = segment,
        StartTicks = start,
        EndTicks = end
    };

    private static CandidateRepresentation Joint(params CandidateRow[] rows) => new() { Channel = "joint", Candidates = rows };

    private static double[] Tilt(double first)
    {
        var vector = new double[ApiV1.Dimension];
        vector[0] = first;
        vector[1] = Math.Sqrt(1 - (first * first));
        return vector;
    }

    private static float[] TiltFloat(float first)
    {
        var vector = new float[ApiV1.Dimension];
        vector[0] = first;
        vector[1] = MathF.Sqrt(1 - (first * first));
        return vector;
    }

    private static CandidateRepresentation Bundle(string channel, List<CandidateRow> rows) => new()
    {
        Channel = channel,
        Version = channel + "-run-v1",
        Model = ApiV1.ModelId,
        Revision = ApiV1.Revision,
        ProcessingId = "proc-a",
        Dimension = 768,
        Normalized = true,
        ContentVersion = "content-v1",
        InputVersion = channel == "joint" ? ApiV1.InputVersion : "transcript-document-cache-v1",
        EmbeddingTask = channel == "joint" ? "video_audio" : "document",
        SegmentationVersion = "ranges-v1",
        ReferenceVersion = channel + "-curve-v1",
        ReferenceQueryVersion = "query-v1",
        Scale = 0.02,
        Background = [0.5, 0.6, 0.7],
        Candidates = rows.ToArray()
    };

    private static void Write(Fixture fixture, params CandidateRepresentation[] bundles)
    {
        foreach (var bundle in bundles)
        {
            bundle.Validate();
        }

        using var connection = new SqliteConnection("Data Source=" + fixture.Store.DatabasePath);
        connection.Open();
        using var transaction = connection.BeginTransaction();
        foreach (var bundle in bundles)
        {
            using var command = connection.CreateCommand();
            command.Transaction = transaction;
            command.CommandText = "INSERT OR REPLACE INTO candidate_representations VALUES ($channel,$version,$json,1)";
            command.Parameters.AddWithValue("$channel", bundle.Channel);
            command.Parameters.AddWithValue("$version", bundle.Version);
            command.Parameters.AddWithValue("$json", JsonSerializer.Serialize(bundle));
            command.ExecuteNonQuery();
        }

        transaction.Commit();
    }
}
