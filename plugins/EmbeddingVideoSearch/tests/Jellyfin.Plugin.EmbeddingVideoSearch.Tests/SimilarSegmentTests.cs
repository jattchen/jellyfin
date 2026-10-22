using System.Reflection;
using System.Text.Json;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Data.Sqlite;
using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class SimilarSegmentTests
{
    [Fact]
    public void Similar_route_is_on_the_user_controller()
    {
        Assert.Contains(typeof(EmbeddingVideoSearchController).GetMethods(), method => method.GetCustomAttribute<HttpPostAttribute>()?.Template == "similar");
        Assert.DoesNotContain(typeof(EmbeddingVideoSearchAdminController).GetMethods(), method => method.GetCustomAttribute<HttpPostAttribute>()?.Template == "similar");
    }

    [Fact]
    public void Excluded_span_drops_same_source_contact_and_keeps_other_films()
    {
        var item = Guid.NewGuid();
        var reference = Row(item, "film", Guid.NewGuid(), 0, 150);
        var source = Row(item, "film", Guid.NewGuid(), 0, 120);
        var touch = Row(item, "film", Guid.NewGuid(), 150, 270);
        var gap = Row(item, "film", Guid.NewGuid(), 151, 270);
        var otherSource = Row(item, "other", Guid.NewGuid(), 0, 150);
        var otherFilm = Row(Guid.NewGuid(), "film", Guid.NewGuid(), 0, 150);
        Assert.Equal((0L, 150L), JointSource.ExcludedSpan(reference, source));
        Assert.True(JointSource.Excluded(source, reference, source));
        Assert.True(JointSource.Excluded(reference, reference, source));
        Assert.True(JointSource.Excluded(touch, reference, source));
        Assert.False(JointSource.Excluded(gap, reference, source));
        Assert.False(JointSource.Excluded(otherSource, reference, source));
        Assert.False(JointSource.Excluded(otherFilm, reference, source));
    }

    [Fact]
    public void Joint_click_uses_document_cosine_and_keeps_distant_and_other_films()
    {
        using var catalog = Catalog.Create();
        var calls = 0;
        catalog.Fixture.Embeddings.Entered = () => calls++;
        catalog.Fixture.Store.SetEmbeddingBaseUrl("");
        var source = catalog.Add("joint", catalog.FilmA, "film", 0, 1000, Axis(0));
        catalog.Add("joint", catalog.FilmA, "film", 1000, 2000, Axis(0));
        catalog.Add("joint", catalog.FilmA, "film", 500, 1600, Tilt(0.2));
        var far = catalog.Add("joint", catalog.FilmA, "film", 5000, 6000, Axis(0));
        var gap = catalog.Add("joint", catalog.FilmA, "film", 1001, 1800, Tilt(0.4));
        var decoy = catalog.Add("joint", catalog.FilmA, "film", 9000, 10000, Axis(1));
        var otherFilm = catalog.Add("joint", catalog.FilmB, "film", 0, 1000, Tilt(0.6));
        var otherSource = catalog.Add("joint", catalog.FilmA, "other", 0, 1001, Tilt(0.7));
        var tiedEarly = catalog.Add("joint", catalog.FilmA, "film", 12000, 13000, Axis(0));
        var tiedLate = catalog.Add("joint", catalog.FilmA, "film", 14000, 15000, Axis(0));
        var opposite = catalog.Add("joint", catalog.FilmA, "film", 20000, 21000, Negative());
        catalog.Save();

        var response = catalog.Ask(source, "jointAudioVideo");
        Assert.Equal(0, calls);
        Assert.Equal(ApiV1.DocumentCosineScoreMeaning, response.QualityPolicy.ScoreMeaning);
        Assert.Null(response.QualityPolicy.RankingVersion);
        Assert.DoesNotContain("referenceTailR", JsonProtocol.Serialize(response), StringComparison.Ordinal);
        Assert.Equal(source.SegmentId, response.Reference.SegmentId);
        Assert.Equal(source.ItemId, response.Reference.ItemId);
        Assert.Equal("jointAudioVideo", response.Reference.Representation);
        Assert.Equal(source.SegmentId, response.Reference.Source.SegmentId);
        Assert.Equal("jointAudioVideo", response.Reference.Source.Representation);
        Assert.Equal(0, response.Reference.Excluded.StartTicks);
        Assert.Equal(1000, response.Reference.Excluded.EndTicks);
        Assert.Equal(1d, response.Results[0].Score, 8);
        Assert.Equal(response.Results[0].Score, response.Results[0].RawScore);
        Assert.Equal("candidate", response.Results[0].Quality);
        Assert.Equal("jointAudioVideo", response.Results[0].Representation);
        Assert.False(string.IsNullOrEmpty(response.Results[0].CandidateId));
        Assert.Matches("^[0-9a-f]{64}$", response.Reference.BindingVersion);
        Assert.Matches("^[0-9a-f]{64}$", response.Reference.Source.BindingVersion);
        Assert.Matches("^[0-9a-f]{64}$", response.Results[0].BindingVersion);
        Assert.DoesNotContain(response.Results, hit => hit.StartTicks is 1000 or 500);
        Assert.Equal(
            new[] { tiedEarly.SegmentId, tiedLate.SegmentId, far.SegmentId, otherSource.SegmentId, otherFilm.SegmentId, gap.SegmentId, decoy.SegmentId },
            response.Results.Select(hit => hit.SegmentId).ToArray());
        Assert.Equal(otherSource.SegmentId, response.Results[3].SegmentId);
        Assert.Equal(0.7, response.Results[3].Score, 8);
        Assert.Equal(otherFilm.SegmentId, response.Results[4].SegmentId);
        Assert.Equal(gap.SegmentId, response.Results[5].SegmentId);
        Assert.Equal(decoy.SegmentId, response.Results[6].SegmentId);
        Assert.Equal(0d, response.Results[6].Score, 8);
        Assert.DoesNotContain(response.Results, hit => hit.SegmentId == opposite.SegmentId);
        Assert.All(response.Results, hit => Assert.Null(hit.Run));
    }

    [Fact]
    public void Transcript_click_uses_the_overlapping_joint_window_not_the_text_vector()
    {
        using var catalog = Catalog.Create();
        var text = catalog.Add("text", catalog.FilmA, "film", 0, 150, Axis(1), "对白原文");
        var chosen = catalog.Add("joint", catalog.FilmA, "film", 0, 120, Axis(0));
        var smaller = catalog.Add("joint", catalog.FilmA, "film", 100, 220, Axis(1));
        var touch = catalog.Add("joint", catalog.FilmA, "film", 150, 270, Axis(0));
        var kept = catalog.Add("joint", catalog.FilmA, "film", 151, 271, Axis(0));
        var wrongDirection = catalog.Add("joint", catalog.FilmA, "film", 8000, 8120, Axis(1));
        catalog.Save();

        var response = catalog.Ask(text, "transcriptText");
        Assert.Equal(text.SegmentId, response.Reference.SegmentId);
        Assert.Equal("transcriptText", response.Reference.Representation);
        Assert.Equal("对白原文", text.Transcript);
        Assert.DoesNotContain("对白原文", response.Reference.Source.BindingVersion, StringComparison.Ordinal);
        Assert.Equal(chosen.SegmentId, response.Reference.Source.SegmentId);
        Assert.Equal(0, response.Reference.Source.StartTicks);
        Assert.Equal(120, response.Reference.Source.EndTicks);
        Assert.Equal(0, response.Reference.Excluded.StartTicks);
        Assert.Equal(150, response.Reference.Excluded.EndTicks);
        Assert.Equal(new[] { kept.SegmentId, wrongDirection.SegmentId }, response.Results.Select(hit => hit.SegmentId).ToArray());
        Assert.Equal(1d, response.Results[0].Score, 8);
        Assert.Equal(0d, response.Results[1].Score, 8);
        Assert.DoesNotContain(response.Results, hit => hit.SegmentId == smaller.SegmentId || hit.SegmentId == touch.SegmentId || hit.SegmentId == chosen.SegmentId);
        Assert.Equal(0, catalog.Calls);
    }

    [Fact]
    public void Missing_joint_coverage_revocation_binding_content_and_signature_fail_closed()
    {
        using var catalog = Catalog.Create();
        var text = catalog.Add("text", catalog.FilmA, "film", 0, 50, Axis(1), "没有声画窗");
        var far = catalog.Add("joint", catalog.FilmA, "film", 1000, 1120, Axis(0));
        var kept = catalog.Add("joint", catalog.FilmA, "film", 8000, 8120, Axis(0));
        var target = catalog.Add("joint", catalog.FilmB, "film", 4000, 4120, Axis(0));
        catalog.Save();
        var unavailable = Assert.Throws<SearchInputException>(() => catalog.Ask(text, "transcriptText"));
        Assert.Equal("referenceUnavailable", unavailable.Code);
        Assert.DoesNotContain("没有声画窗", unavailable.Message, StringComparison.Ordinal);
        Assert.Equal(0, catalog.Calls);

        var source = far;
        catalog.Fixture.Allow.Remove(catalog.FilmA);
        var hidden = Assert.Throws<SearchAuthException>(() => catalog.Ask(source, "jointAudioVideo"));
        var missing = Assert.Throws<SearchAuthException>(() => catalog.Fixture.Runtime.Similar(
            catalog.Fixture.UserId,
            new SimilarRequest { SegmentId = Guid.NewGuid(), ExpectedBindingVersion = new string('a', 64) },
            CancellationToken.None));
        Assert.Equal(404, hidden.Status);
        Assert.Equal("notFound", hidden.Code);
        Assert.Equal(hidden.Message, missing.Message);
        Assert.Equal("Segment was not found.", hidden.Message);
        catalog.Fixture.Allow.Add(catalog.FilmA);

        catalog.Fixture.Allow.Remove(catalog.FilmB);
        var withoutTarget = catalog.Ask(source, "jointAudioVideo");
        Assert.Equal(new[] { kept.SegmentId }, withoutTarget.Results.Select(hit => hit.SegmentId).ToArray());
        Assert.DoesNotContain(withoutTarget.Results, hit => hit.ItemId == catalog.FilmB || hit.SegmentId == target.SegmentId);
        catalog.Fixture.Allow.Add(catalog.FilmB);

        var mismatch = Assert.Throws<SearchInputException>(() => catalog.Fixture.Runtime.Similar(
            catalog.Fixture.UserId,
            new SimilarRequest { SegmentId = source.SegmentId, ExpectedBindingVersion = new string('b', 64) },
            CancellationToken.None));
        Assert.Equal("bindingMismatch", mismatch.Code);

        catalog.Fixture.Store.HideItem(catalog.FilmA, "film");
        var stale = Assert.Throws<SearchInputException>(() => catalog.Fixture.Runtime.Similar(
            catalog.Fixture.UserId,
            new SimilarRequest { SegmentId = source.SegmentId, ExpectedBindingVersion = new string('b', 64) },
            CancellationToken.None));
        Assert.Equal("stale", stale.Code);

        using var signed = Catalog.Create();
        var visible = signed.Add("joint", signed.FilmA, "film", 0, 1000, Axis(0));
        signed.Add("joint", signed.FilmA, "film", 5000, 6000, Axis(0));
        signed.Save();
        signed.Joint.ProcessingId = "other-processing";
        signed.Save();
        var incompatible = Assert.Throws<SearchInputException>(() => signed.Ask(visible, "jointAudioVideo"));
        Assert.Equal("incompatible", incompatible.Code);
        Assert.Equal(0, catalog.Calls + signed.Calls);
    }

    [Fact]
    public void Publication_recheck_drops_revoked_targets_and_refuses_a_changed_source()
    {
        using var catalog = Catalog.Create();
        var source = catalog.Add("joint", catalog.FilmA, "film", 0, 1000, Axis(0));
        var far = catalog.Add("joint", catalog.FilmA, "film", 5000, 6000, Axis(0));
        var target = catalog.Add("joint", catalog.FilmB, "film", 7000, 8000, Tilt(0.2));
        catalog.Save();
        catalog.Fixture.OnReadFacts = (item, _) =>
        {
            if (item == catalog.FilmB)
            {
                catalog.Fixture.Allow.Remove(catalog.FilmB);
            }
        };
        var published = catalog.Ask(source, "jointAudioVideo");
        Assert.Equal(new[] { far.SegmentId }, published.Results.Select(hit => hit.SegmentId).ToArray());
        Assert.DoesNotContain(published.Results, hit => hit.SegmentId == target.SegmentId);

        catalog.Fixture.Allow.Add(catalog.FilmB);
        catalog.Fixture.OnReadFacts = (_, _) => catalog.Fixture.Store.SetIncompatible(true);
        var blocked = Assert.Throws<SearchInputException>(() => catalog.Ask(source, "jointAudioVideo"));
        Assert.Equal("incompatible", blocked.Code);

        catalog.Fixture.Store.SetIncompatible(false);
        catalog.Fixture.OnReadFacts = (item, _) =>
        {
            if (item == catalog.FilmA)
            {
                catalog.Fixture.Allow.Remove(catalog.FilmA);
            }
        };
        var lost = Assert.Throws<SearchAuthException>(() => catalog.Ask(source, "jointAudioVideo"));
        Assert.Equal(404, lost.Status);
        Assert.Equal(0, catalog.Calls);
    }

    [Fact]
    public void Cancellation_stops_before_a_similar_response_is_published()
    {
        using var catalog = Catalog.Create();
        var source = catalog.Add("joint", catalog.FilmA, "film", 0, 1000, Axis(0));
        catalog.Add("joint", catalog.FilmA, "film", 5000, 6000, Axis(0));
        catalog.Save();
        using var cancelled = new CancellationTokenSource();
        cancelled.Cancel();
        Assert.Throws<OperationCanceledException>(() => catalog.Fixture.Runtime.Similar(
            catalog.Fixture.UserId, catalog.Request(source, "jointAudioVideo"), cancelled.Token));

        using var during = new CancellationTokenSource();
        catalog.Fixture.OnReadFacts = (_, _) => during.Cancel();
        Assert.Throws<OperationCanceledException>(() => catalog.Fixture.Runtime.Similar(
            catalog.Fixture.UserId, catalog.Request(source, "jointAudioVideo"), during.Token));
        Assert.Equal(0, catalog.Calls);
    }

    [Fact]
    public void Collapse_keeps_only_the_returned_page_and_parent_scope_can_hide_the_source()
    {
        using var catalog = Catalog.Create();
        var source = catalog.Add("joint", catalog.FilmA, "film", 0, 120, Axis(0));
        var chain = new List<CandidateRow>();
        for (var index = 0; index < 6; index++)
        {
            var start = 10_000 + (index * 120);
            chain.Add(catalog.Add("joint", catalog.FilmA, "film", start, start + 120, Tilt(0.99 - (index * 0.01))));
        }

        var other = catalog.Add("joint", catalog.FilmB, "film", 0, 120, Tilt(0.2));
        catalog.Save();
        var collapsed = catalog.Ask(source, "jointAudioVideo", new SimilarRequest { GroupByVideo = true, Limit = 1, CollapseRuns = true });
        var only = Assert.Single(collapsed.Results);
        Assert.Equal(chain[0].SegmentId, only.SegmentId);
        Assert.Equal(10_000, only.StartTicks);
        Assert.Equal(10_120, only.EndTicks);
        var run = only.Run;
        Assert.NotNull(run);
        Assert.Equal(10_000, run.StartTicks);
        Assert.Equal(10_600, run.EndTicks);
        Assert.Equal(5, run.MemberCount);
        Assert.True(only.SimilarAvailable);
        Assert.DoesNotContain(collapsed.Results, hit => hit.ItemId == catalog.FilmB || hit.SegmentId == chain[5].SegmentId);

        var open = catalog.Ask(source, "jointAudioVideo", new SimilarRequest { GroupByVideo = true, Limit = 3 });
        Assert.Contains(open.Results, hit => hit.SegmentId == other.SegmentId);
        Assert.DoesNotContain(open.Results, hit => hit.SegmentId == chain[5].SegmentId);
        Assert.All(open.Results, hit => Assert.Null(hit.Run));
        Assert.DoesNotContain("\"run\"", JsonProtocol.Serialize(open), StringComparison.Ordinal);

        var parent = Guid.NewGuid();
        catalog.Fixture.Containers.Add(parent);
        var outside = Assert.Throws<SearchAuthException>(() => catalog.Ask(source, "jointAudioVideo", new SimilarRequest { ParentId = parent }));
        Assert.Equal(404, outside.Status);
        catalog.Fixture.Parents.Add((catalog.FilmA, parent));
        var inside = catalog.Ask(source, "jointAudioVideo", new SimilarRequest { ParentId = parent });
        Assert.All(inside.Results, hit => Assert.Equal(catalog.FilmA, hit.ItemId));
        Assert.Equal(0, catalog.Calls);
    }

    [Fact]
    public async Task Invalid_similar_requests_do_not_read_the_model()
    {
        using var catalog = Catalog.Create();
        var source = catalog.Add("joint", catalog.FilmA, "film", 0, 1000, Axis(0));
        catalog.Save();
        var missingBinding = Assert.Throws<SearchInputException>(() => catalog.Fixture.Runtime.Similar(
            catalog.Fixture.UserId, new SimilarRequest { SegmentId = source.SegmentId }, CancellationToken.None));
        var badLimit = Assert.Throws<SearchInputException>(() => catalog.Fixture.Runtime.Similar(
            catalog.Fixture.UserId,
            new SimilarRequest { SegmentId = source.SegmentId, ExpectedBindingVersion = catalog.Bind(source, "jointAudioVideo"), Limit = 0 },
            CancellationToken.None));
        Assert.Equal("invalidRequest", missingBinding.Code);
        Assert.Equal("invalidRequest", badLimit.Code);
        Assert.Equal(0, catalog.Calls);
        var capabilities = await catalog.Fixture.Runtime.CapabilitiesAsync(catalog.Fixture.UserId, CancellationToken.None);
        Assert.Contains(ApiV1.SimilarSegmentsFeature, capabilities.Features);
        Assert.Contains(ApiV1.CollapsedRunsFeature, capabilities.Features);
    }

    [Fact]
    public void A_vector_of_the_wrong_length_is_incompatible_and_does_not_call_the_model()
    {
        using var catalog = Catalog.Create();
        var source = catalog.Add("joint", catalog.FilmA, "film", 0, 1000, Axis(0));
        var broken = catalog.Add("joint", catalog.FilmA, "film", 5000, 6000, Axis(0));
        broken.Vector = [1d, 0d];
        catalog.StoreUnchecked();
        var incompatible = Assert.Throws<SearchInputException>(() => catalog.Ask(source, "jointAudioVideo"));
        Assert.Equal("incompatible", incompatible.Code);
        Assert.Equal(0, catalog.Calls);
    }

    private static CandidateRow Row(Guid item, string source, Guid segment, long start, long end) => new()
    {
        ItemId = item,
        MediaSourceId = source,
        ContentVersion = "v1",
        SegmentId = segment,
        StartTicks = start,
        EndTicks = end
    };

    private static double[] Negative()
    {
        var vector = Axis(0);
        vector[0] = -1;
        return vector;
    }

    private static double[] Axis(int index)
    {
        var vector = new double[ApiV1.Dimension];
        vector[index] = 1;
        return vector;
    }

    private static double[] Tilt(double first)
    {
        var vector = new double[ApiV1.Dimension];
        vector[0] = first;
        vector[1] = Math.Sqrt(Math.Max(0, 1 - (first * first)));
        return vector;
    }

    private sealed class Catalog : IDisposable
    {
        public const string JointVersion = "joint-sim-v1";
        public const string TextVersion = "text-sim-v1";
        private readonly HashSet<(Guid Item, string Source)> _media = [];

        public Fixture Fixture { get; }
        public Guid LibraryId { get; } = Guid.NewGuid();
        public Guid FilmA { get; } = Guid.NewGuid();
        public Guid FilmB { get; } = Guid.NewGuid();
        public CandidateRepresentation Joint { get; }
        public CandidateRepresentation Text { get; }
        public int Calls { get; private set; }

        private Catalog(Fixture fixture)
        {
            Fixture = fixture;
            Joint = Bundle("joint", JointVersion);
            Text = Bundle("text", TextVersion);
            Fixture.Store.ReplaceLibraries([LibraryId]);
            Fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
            Fixture.Store.SetEmbeddingBaseUrl("http://synthetic.test");
            Fixture.Embeddings.Entered = () => Calls++;
        }

        public static Catalog Create() => new(Fixture.Create());

        public CandidateRow Add(string channel, Guid item, string source, long start, long end, double[] vector, string? transcript = null)
        {
            var row = new CandidateRow
            {
                SegmentId = Guid.NewGuid(),
                CandidateId = channel + ":" + item.ToString("N") + ":" + source + ":" + start,
                ItemId = item,
                MediaSourceId = source,
                LibraryId = LibraryId,
                ContentVersion = "v1",
                StartTicks = start,
                EndTicks = end,
                RuntimeTicks = 50_000_000_000,
                Canonical = new CanonicalAnchor { SourceItemId = item, StartTicks = start, EndTicks = end },
                AnchorId = $"{item:N}:{start}:{end}",
                Transcript = transcript,
                Vector = vector
            };
            (channel == "joint" ? Joint : Text).Candidates = (channel == "joint" ? Joint : Text).Candidates.Append(row).ToArray();
            if (_media.Add((item, source)))
            {
                Fixture.Allow.Add(item);
                Fixture.Store.SaveSucceededWindow(item, source, LibraryId, "v1", 50_000_000_000, 0, 12_000_000, CoreTests.Axis(0), null);
            }

            return row;
        }

        public void Save() => Write(validate: true);

        public void StoreUnchecked() => Write(validate: false);

        private void Write(bool validate)
        {
            if (Text.Candidates.Length == 0)
            {
                Text.TranscriptionChecked = true;
                Text.Scale = 0;
                Text.Background = [];
            }

            if (validate)
            {
                Joint.Validate();
                Text.Validate();
            }

            using var connection = new SqliteConnection("Data Source=" + Fixture.Store.DatabasePath);
            connection.Open();
            using var transaction = connection.BeginTransaction();
            foreach (var bundle in new[] { Joint, Text })
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

        public string Bind(CandidateRow row, string representation)
            => SegmentBinding.Version(row.SegmentId, row.ItemId, row.MediaSourceId, row.ContentVersion, row.StartTicks, row.EndTicks, representation, row.CandidateId, representation == "jointAudioVideo" ? JointVersion : TextVersion);

        public SimilarRequest Request(CandidateRow row, string representation, SimilarRequest? template = null)
        {
            var request = template ?? new SimilarRequest();
            request.SegmentId = row.SegmentId;
            request.ExpectedBindingVersion = Bind(row, representation);
            return request;
        }

        public SimilarResponse Ask(CandidateRow row, string representation, SimilarRequest? template = null)
            => Fixture.Runtime.Similar(Fixture.UserId, Request(row, representation, template), CancellationToken.None);

        public void Dispose() => Fixture.Dispose();

        private static CandidateRepresentation Bundle(string channel, string version) => new()
        {
            Channel = channel,
            Version = version,
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
            Candidates = []
        };
    }
}
