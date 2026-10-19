using System.Text.Json;
using Microsoft.Data.Sqlite;
using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class CandidateTests
{
    [Fact]
    public async Task Returning_to_a_query_reuses_only_its_vector_and_rechecks_permission()
    {
        using var fixture = Fixture.Create();
        Prepare(fixture);
        var calls = 0;
        fixture.Embeddings.Entered = () => calls++;
        var request = new SearchRequest { Query = "return-from-playback" };
        Assert.Equal(2, (await fixture.Runtime.SearchAsync(fixture.UserId, request, CancellationToken.None)).Results.Count);
        Assert.Equal(2, (await fixture.Runtime.SearchAsync(fixture.UserId, request, CancellationToken.None)).Results.Count);
        Assert.Equal(1, calls);
        fixture.Allow.Clear();
        Assert.Empty((await fixture.Runtime.SearchAsync(fixture.UserId, request, CancellationToken.None)).Results);
        Assert.Equal(1, calls);
    }

    [Fact]
    public async Task Cached_query_does_not_hide_a_model_outage_or_signature_change()
    {
        using var fixture = Fixture.Create();
        Prepare(fixture);
        var calls = 0;
        fixture.Embeddings.Entered = () => calls++;
        var request = new SearchRequest { Query = "cached-query" };
        Assert.NotEmpty((await fixture.Runtime.SearchAsync(fixture.UserId, request, CancellationToken.None)).Results);
        fixture.Store.SetEmbeddingBaseUrl("");
        Assert.Equal("modelOffline", (await fixture.Runtime.SearchAsync(fixture.UserId, request, CancellationToken.None)).Availability.Search);
        Assert.Equal(1, calls);
        fixture.Store.SetEmbeddingBaseUrl("http://synthetic.test");
        fixture.Embeddings.ProcessingId = "other-processing";
        Assert.Equal("incompatible", (await fixture.Runtime.SearchAsync(fixture.UserId, request, CancellationToken.None)).Availability.Search);
        Assert.Equal(1, calls);
    }

    [Theory]
    [InlineData("[]")]
    [InlineData("\"offline\"")]
    [InlineData("{\"data\":[\"invalid\"]}")]
    public void Malformed_model_metadata_is_a_controlled_failure(string json)
        => Assert.False(HttpEmbeddingClient.ParseModels(json).Reachable);

    [Fact]
    public void Reference_tail_is_not_probability_and_remains_finite_in_extreme_tail()
    {
        var representation = new CandidateRepresentation { Background = [0.6, 0.6], Scale = 0.01 };
        Assert.Equal(Math.Log10(2), CandidateRanking.ReferenceScore(0.6, representation), 12);
        Assert.True(CandidateRanking.ReferenceScore(0.99, representation) > 10);
        Assert.True(double.IsFinite(CandidateRanking.ReferenceScore(100, representation)));
    }

    [Fact]
    public async Task Imported_text_and_joint_keep_their_own_times_and_current_access()
    {
        using var fixture = Fixture.Create();
        var bundles = Prepare(fixture);
        var windows = fixture.Store.WindowCount();
        var generation = fixture.Store.ActiveGeneration;
        var response = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic", Diagnostics = true }, CancellationToken.None);
        Assert.Equal("referenceTailR", response.QualityPolicy.ScoreMeaning);
        Assert.Equal(CandidateRanking.Version, response.QualityPolicy.RankingVersion);
        Assert.Equal("joint-v1", response.QualityPolicy.Representations["jointAudioVideo"]);
        Assert.Equal("text-v1", response.QualityPolicy.Representations["transcriptText"]);
        var capabilities = await fixture.Runtime.CapabilitiesAsync(fixture.UserId, CancellationToken.None);
        Assert.Equal("referenceTailR", capabilities.QualityPolicy.ScoreMeaning);
        Assert.Contains(ApiV1.GroupedVideoFeature, capabilities.Features);
        Assert.Equal(CandidateRanking.Version, capabilities.QualityPolicy.RankingVersion);
        Assert.Null(response.QualityPolicy.RelatedMinimum);
        Assert.Equal(2, response.Results.Count);
        Assert.Equal(2, response.RawResults!.Count);
        var text = Assert.Single(response.Results, h => h.Representation == "transcriptText");
        Assert.Equal(10_000_000, text.StartTicks);
        Assert.Equal(40_000_000, text.EndTicks);
        Assert.Equal(2_070_000_000, text.Canonical!.StartTicks);
        Assert.Equal("candidate", text.Quality);
        Assert.True(text.IndexComplete);
        Assert.True(response.IndexComplete);
        Assert.NotNull(text.RawScore);
        Assert.Equal(windows, fixture.Store.WindowCount());
        Assert.Equal(generation, fixture.Store.ActiveGeneration);
        var detail = fixture.Runtime.GetSegment(fixture.UserId, text.SegmentId);
        Assert.Equal(200, detail.StatusCode);
        Assert.False(string.IsNullOrEmpty(text.BindingVersion));
        Assert.Equal(text.BindingVersion, detail.Body!.BindingVersion);
        fixture.Allow.Clear();
        Assert.Equal(404, fixture.Runtime.GetSegment(fixture.UserId, text.SegmentId).StatusCode);
        Assert.Empty((await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None)).Results);
        fixture.Allow.Add(bundles[0].Candidates[0].ItemId);
        fixture.Store.HideItem(text.ItemId, text.MediaSourceId);
        Assert.Equal(409, fixture.Runtime.GetSegment(fixture.UserId, text.SegmentId).StatusCode);
        Assert.Empty((await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None)).Results);
        fixture.Store.ReplaceLibraries([]);
        Assert.Equal(404, fixture.Runtime.GetSegment(fixture.UserId, text.SegmentId).StatusCode);
    }

    [Fact]
    public async Task Revocation_during_query_and_text_version_change_cannot_publish_old_results()
    {
        using var fixture = Fixture.Create();
        var bundles = Prepare(fixture);
        fixture.Embeddings.Entered = () => fixture.Allow.Clear();
        Assert.Empty((await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None)).Results);
        fixture.Allow.Add(bundles[0].Candidates[0].ItemId);
        fixture.Embeddings.Entered = () =>
        {
            bundles[1].Version = "text-new-version";
            Write(fixture, bundles);
        };
        var changed = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic-version-change" }, CancellationToken.None);
        Assert.Empty(changed.Results);
        Assert.Equal("incompatible", changed.Availability.Search);
    }

    [Fact]
    public void A_shared_anchor_never_promotes_a_lower_scoring_different_interval()
    {
        var item = Guid.NewGuid();
        CandidateRow Row(string id, string anchor, long start, double raw) => new()
        {
            ItemId = item, MediaSourceId = "same-source", CandidateId = id, AnchorId = anchor,
            StartTicks = start, EndTicks = start + 10, Vector = CoreTests.Axis(0).Select(v => (double)v * raw).ToArray()
        };
        var ranked = CandidateRanking.Rank(CoreTests.Axis(0), new CandidateSnapshot { Representations =
        [
            new CandidateRepresentation { Channel = "joint", Scale = 0.1, Background = [0.2],
                Candidates = [Row("high", "shared", 0, 0.9), Row("middle", "other", 20, 0.8)] },
            new CandidateRepresentation { Channel = "text", Scale = 0.1, Background = [0.2],
                Candidates = [Row("low", "shared", 5, 0.1)] }
        ] }, _ => true);
        Assert.Equal(new[] { "high", "middle", "low" }, ranked.Select(h => h.CandidateId!).ToArray());
    }

    [Fact]
    public void Same_playable_range_keeps_the_stronger_score_and_overlap_stays_separate()
    {
        var item = Guid.NewGuid();
        var anchor = $"{item:N}:100:200";
        CandidateRepresentation Make(string channel, long start, long end, double axis, string anchorId) => new()
        {
            Channel = channel, Scale = 0.02, Background = [0.2], Candidates =
            [
                new CandidateRow
                {
                    ItemId = item, MediaSourceId = "sample", StartTicks = start, EndTicks = end,
                    AnchorId = anchorId, CandidateId = channel + start, SegmentId = Guid.NewGuid(),
                    Canonical = new CanonicalAnchor { SourceItemId = item, StartTicks = start, EndTicks = end },
                    Vector = CoreTests.Axis(0).Select(v => (double)v * axis).ToArray()
                }
            ]
        };
        var same = CandidateRanking.Rank(CoreTests.Axis(0), new CandidateSnapshot
        {
            Representations =
            [
                Make("joint", 0, 60, 1, anchor),
                Make("text", 0, 60, 0.5, anchor + "-other")
            ]
        }, _ => true);
        var kept = Assert.Single(same);
        Assert.Equal("jointAudioVideo", kept.Representation);
        Assert.Equal(1d, kept.RawScore);

        var overlap = CandidateRanking.Rank(CoreTests.Axis(0), new CandidateSnapshot
        {
            Representations =
            [
                Make("joint", 0, 60, 1, anchor),
                Make("text", 10, 40, 1, anchor)
            ]
        }, _ => true);
        Assert.Equal(2, overlap.Count);
        Assert.All(overlap, hit => Assert.Equal(anchor, hit.AnchorId));
        Assert.Equal([0L, 10L], overlap.Select(hit => hit.StartTicks).Order().ToArray());
    }

    [Fact]
    public async Task Incompatible_import_is_refused_before_any_query_inference()
    {
        using var fixture = Fixture.Create();
        var bundles = Prepare(fixture);
        bundles[1].ProcessingId = "other-processing";
        Write(fixture, bundles);
        var submitted = 0;
        fixture.Embeddings.Entered = () => submitted++;
        var response = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        Assert.Equal("incompatible", response.Availability.Search);
        Assert.Equal(0, submitted);
        bundles[1].Candidates[0].StartTicks = 296 * ApiV1.TicksPerSecond;
        Assert.Throws<InvalidDataException>(() => bundles[1].Validate());
    }

    [Fact]
    public void Binding_stamp_includes_the_candidate_and_representation_version()
    {
        var segment = Guid.NewGuid();
        var item = Guid.NewGuid();
        var first = SegmentBinding.Version(segment, item, "source", "content", 0, 10, "transcriptText", "candidate-a", "representation-1");
        Assert.Equal(first, SegmentBinding.Version(segment, item, "source", "content", 0, 10, "transcriptText", "candidate-a", "representation-1"));
        Assert.NotEqual(first, SegmentBinding.Version(segment, item, "source", "content", 0, 10, "transcriptText", "candidate-b", "representation-1"));
        Assert.NotEqual(first, SegmentBinding.Version(segment, item, "source", "content", 0, 10, "transcriptText", "candidate-a", "representation-2"));
        Assert.NotEqual(first, SegmentBinding.Version(segment, item, "source", "content", 0, 10, "jointAudioVideo"));
    }

    [Fact]
    public void Exact_range_keeps_its_own_transcript_and_a_partial_overlap_does_not()
    {
        var item = Guid.NewGuid();
        var ranked = CandidateRanking.Rank(CoreTests.Axis(0), new CandidateSnapshot
        {
            Representations =
            [
                new CandidateRepresentation
                {
                    Channel = "joint", Scale = 0.02, Background = [0.2],
                    Candidates = [Row(item, "joint", 0, 60, 1, null)]
                },
                new CandidateRepresentation
                {
                    Channel = "text", Scale = 0.02, Background = [0.2],
                    Candidates = [Row(item, "text-same", 0, 60, 0.5, "同一段原文"), Row(item, "text-part", 10, 40, 0.4, "只盖住一部分")]
                }
            ]
        }, _ => true);
        var exact = Assert.Single(ranked, hit => hit.StartTicks == 0);
        Assert.Equal("同一段原文", exact.Transcript);
        var partial = Assert.Single(ranked, hit => hit.StartTicks == 10);
        Assert.Equal("只盖住一部分", partial.Transcript);
        Assert.DoesNotContain(ranked, hit => hit.StartTicks == 0 && hit.Transcript == "只盖住一部分");
    }

    [Fact]
    public async Task Grouped_results_follow_each_film_without_replacing_the_full_ranking()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        var films = new[] { Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid() };
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SetEmbeddingBaseUrl("http://synthetic.test");
        foreach (var film in films)
        {
            fixture.Allow.Add(film);
            fixture.Store.SaveSucceededWindow(film, "film", library, "v1", 10_000_000_000, 0, 60_000_000, CoreTests.Axis(0), null);
        }

        var joint = new List<CandidateRow>();
        var text = new List<CandidateRow>();
        void Add(Guid film, string channel, long start, double strength, string? transcript, List<CandidateRow> target)
        {
            var end = start + 100;
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
                RuntimeTicks = 10_000_000_000,
                Canonical = new CanonicalAnchor { SourceItemId = film, StartTicks = start, EndTicks = end },
                AnchorId = $"{film:N}:{start}:{end}",
                Transcript = transcript,
                Vector = Tilt(strength)
            });
        }

        Add(films[0], "joint", 0, 0.99, null, joint);
        Add(films[0], "text", 0, 0.20, "同一段原文", text);
        Add(films[0], "joint", 1000, 0.98, null, joint);
        Add(films[0], "joint", 2000, 0.97, null, joint);
        Add(films[0], "joint", 3000, 0.96, null, joint);
        Add(films[0], "joint", 4000, 0.95, null, joint);
        Add(films[0], "joint", 5000, 0.50, null, joint);
        Add(films[0], "text", 1500, 0.10, "只盖住一部分", text);
        Add(films[1], "joint", 0, 0.90, null, joint);
        Add(films[1], "joint", 1000, 0.40, null, joint);
        Add(films[2], "joint", 0, 0.80, null, joint);
        var bundles = new[] { Bundle("joint", joint), Bundle("text", text) };
        foreach (var bundle in bundles)
        {
            bundle.Validate();
        }

        Write(fixture, bundles);
        var calls = 0;
        fixture.Embeddings.Entered = () => calls++;
        var tooMany = await Assert.ThrowsAsync<SearchInputException>(() => fixture.Runtime.SearchAsync(
            fixture.UserId, new SearchRequest { Query = "grouped", GroupByVideo = true, Limit = 4 }, CancellationToken.None));
        Assert.Equal("invalidRequest", tooMany.Code);
        Assert.Equal(0, calls);

        var grouped = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest
        {
            Query = "grouped",
            GroupByVideo = true,
            Limit = 3,
            Diagnostics = true
        }, CancellationToken.None);
        Assert.Equal(1, calls);
        var filmOrder = new List<Guid>();
        foreach (var hit in grouped.Results)
        {
            if (filmOrder.Count == 0 || filmOrder[^1] != hit.ItemId)
            {
                filmOrder.Add(hit.ItemId);
            }
        }

        Assert.Equal(films, filmOrder);
        Assert.Equal(5, grouped.Results.Count(hit => hit.ItemId == films[0]));
        Assert.Equal(2, grouped.Results.Count(hit => hit.ItemId == films[1]));
        Assert.Equal(1, grouped.Results.Count(hit => hit.ItemId == films[2]));
        Assert.Equal("同一段原文", grouped.Results[0].Transcript);
        Assert.DoesNotContain(grouped.Results.Take(5), hit => hit.Transcript == "只盖住一部分");
        var raw = grouped.RawResults;
        Assert.NotNull(raw);
        Assert.Single(raw.Take(3).Select(hit => hit.ItemId).Distinct());
        Assert.True(raw.Count > grouped.Results.Count);
        Assert.Equal(raw.Select(hit => hit.CandidateId), raw.OrderByDescending(hit => hit.Score).Select(hit => hit.CandidateId));
        Assert.All(grouped.Results, hit => Assert.True(hit.IndexComplete));
        Assert.All(grouped.Results, hit => Assert.False(string.IsNullOrEmpty(hit.BindingVersion)));

        var top = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "grouped", Limit = 3, Diagnostics = true }, CancellationToken.None);
        Assert.Equal(1, calls);
        Assert.Equal(3, top.Results.Count);
        Assert.All(top.Results, hit => Assert.Equal(films[0], hit.ItemId));
        Assert.Equal(raw.Select(hit => hit.CandidateId), top.RawResults!.Select(hit => hit.CandidateId));

        var oneFilm = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "grouped", GroupByVideo = true, Limit = 1 }, CancellationToken.None);
        Assert.Equal(5, oneFilm.Results.Count);
        Assert.All(oneFilm.Results, hit => Assert.Equal(films[0], hit.ItemId));

        fixture.Allow.Clear();
        var hidden = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "grouped", GroupByVideo = true }, CancellationToken.None);
        Assert.Empty(hidden.Results);
        Assert.DoesNotContain("同一段原文", JsonProtocol.Serialize(hidden), StringComparison.Ordinal);
        Assert.Equal(404, fixture.Runtime.GetSegment(fixture.UserId, text[0].SegmentId).StatusCode);
    }

    [Fact]
    public async Task Partial_film_coverage_is_not_reported_as_a_complete_index()
    {
        using var fixture = Fixture.Create();
        var bundles = Prepare(fixture);
        using (var connection = new Microsoft.Data.Sqlite.SqliteConnection("Data Source=" + fixture.Store.DatabasePath))
        {
            connection.Open();
            using var command = connection.CreateCommand();
            command.CommandText = "UPDATE media SET state = 'partial'";
            command.ExecuteNonQuery();
        }

        var response = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "partial-film" }, CancellationToken.None);
        Assert.False(response.IndexComplete);
        Assert.All(response.Results, hit => Assert.False(hit.IndexComplete));
        var detail = fixture.Runtime.GetSegment(fixture.UserId, bundles[1].Candidates[0].SegmentId);
        Assert.Equal(200, detail.StatusCode);
        Assert.False(detail.Body!.IndexComplete);
        var capabilities = await fixture.Runtime.CapabilitiesAsync(fixture.UserId, CancellationToken.None);
        Assert.False(capabilities.Coverage.IndexComplete);
        Assert.True(capabilities.Coverage.VideosPartial > 0);
        fixture.Store.SetImportedCandidatesOnly(true);
        var still = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "partial-film" }, CancellationToken.None);
        Assert.NotEmpty(still.Results);
        var status = await fixture.Runtime.AdminStatusAsync(fixture.UserId, CancellationToken.None);
        Assert.Contains("importedCandidatesOnly", status.Pause.Reasons);
        Assert.Equal("paused", still.Availability.Indexing);
        Assert.Contains("importedCandidatesOnly", still.Availability.IndexingReasons);
    }

    [Fact]
    public async Task Grouped_search_without_imported_candidates_does_not_call_the_model()
    {
        using var fixture = Fixture.Create();
        fixture.Store.ReplaceLibraries([Guid.NewGuid()]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SetEmbeddingBaseUrl("http://synthetic.test");
        var calls = 0;
        fixture.Embeddings.Entered = () => calls++;
        var error = await Assert.ThrowsAsync<SearchInputException>(() => fixture.Runtime.SearchAsync(
            fixture.UserId, new SearchRequest { Query = "grouped", GroupByVideo = true }, CancellationToken.None));
        Assert.Equal("invalidRequest", error.Code);
        Assert.Equal(0, calls);
    }

    private static CandidateRepresentation[] Prepare(Fixture fixture)
    {
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SetEmbeddingBaseUrl("http://synthetic.test");
        fixture.Store.SaveSucceededWindow(item, "sample", library, "v1", 120_000_000, 0, 60_000_000, CoreTests.Axis(0), null);
        fixture.Allow.Add(item);
        CandidateRepresentation Make(string channel, long start, long end) => new()
        {
            Channel = channel, Version = channel + "-v1", Model = ApiV1.ModelId, Revision = ApiV1.Revision,
            ProcessingId = "proc-a", Dimension = 768, Normalized = true, ContentVersion = "content-v1",
            InputVersion = channel == "joint" ? ApiV1.InputVersion : "transcript-document-cache-v1",
            EmbeddingTask = channel == "joint" ? "video_audio" : "document",
            SegmentationVersion = "ranges-v1", ReferenceVersion = channel + "-curve-v1", ReferenceQueryVersion = "query-v1",
            Scale = 0.02, Background = [0.5, 0.6, 0.7], Candidates = [new CandidateRow
            {
                SegmentId = Guid.NewGuid(), CandidateId = channel + "-candidate", ItemId = item, MediaSourceId = "sample",
                LibraryId = library, ContentVersion = "v1", StartTicks = start, EndTicks = end, RuntimeTicks = 120_000_000,
                Canonical = new CanonicalAnchor { SourceItemId = item, StartTicks = 2_060_000_000 + start, EndTicks = 2_060_000_000 + end },
                AnchorId = $"{item:N}:{2_060_000_000 + start}:{2_060_000_000 + end}", Vector = CoreTests.Axis(0).Select(x => (double)x).ToArray()
            }]
        };
        var bundles = new[] { Make("joint", 0, 60_000_000), Make("text", 10_000_000, 40_000_000) };
        foreach (var bundle in bundles) bundle.Validate();
        Write(fixture, bundles);
        return bundles;
    }

    private static void Write(Fixture fixture, CandidateRepresentation[] bundles)
    {
        using var connection = new SqliteConnection("Data Source=" + fixture.Store.DatabasePath);
        connection.Open();
        using var transaction = connection.BeginTransaction();
        foreach (var bundle in bundles)
        {
            using var deactivate = connection.CreateCommand();
            deactivate.Transaction = transaction;
            deactivate.CommandText = "UPDATE candidate_representations SET active=0 WHERE channel=$channel";
            deactivate.Parameters.AddWithValue("$channel", bundle.Channel);
            deactivate.ExecuteNonQuery();
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

    private static CandidateRow Row(Guid item, string id, long start, long end, double axis, string? transcript) => new()
    {
        ItemId = item,
        MediaSourceId = "film",
        CandidateId = id,
        AnchorId = $"{item:N}:{start}:{end}",
        SegmentId = Guid.NewGuid(),
        StartTicks = start,
        EndTicks = end,
        Transcript = transcript,
        Canonical = new CanonicalAnchor { SourceItemId = item, StartTicks = start, EndTicks = end },
        Vector = CoreTests.Axis(0).Select(value => (double)value * axis).ToArray()
    };

    private static double[] Tilt(double first)
    {
        var vector = new double[ApiV1.Dimension];
        vector[0] = first;
        vector[1] = Math.Sqrt(1 - (first * first));
        return vector;
    }

    private static CandidateRepresentation Bundle(string channel, List<CandidateRow> rows) => new()
    {
        Channel = channel,
        Version = channel + "-group-v1",
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
}
