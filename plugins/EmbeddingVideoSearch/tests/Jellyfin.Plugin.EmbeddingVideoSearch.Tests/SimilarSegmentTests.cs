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

    [Fact]
    public void Real_scale_page_reads_each_source_once_per_pass()
    {
        // 117a54 checked every surviving joint window on both passes, about 16354 source checks
        // across 46 sources, then read media state for every positive hit, about 8176 times.
        const int windowCount = 8179;
        const int sourceCount = 46;
        using var catalog = Catalog.Create();
        var shared = Axis(0);
        var films = Enumerable.Range(0, sourceCount).Select(_ => Guid.NewGuid()).ToArray();
        var rows = new List<CandidateRow>(windowCount);
        var ordinal = new int[sourceCount];
        for (var index = 0; index < windowCount; index++)
        {
            var sourceIndex = index % sourceCount;
            var start = ordinal[sourceIndex]++ * 121L;
            rows.Add(Window(films[sourceIndex], "film", catalog.LibraryId, "v1", start, shared));
        }

        foreach (var film in films)
        {
            catalog.Persist(film, "film", catalog.LibraryId, "v1");
        }

        catalog.Adopt(rows);
        catalog.Save();
        var reference = rows[0];
        var facts = new Dictionary<Guid, int>();
        catalog.Fixture.OnReadFacts = (item, _) => facts[item] = facts.GetValueOrDefault(item) + 1;
        var store = catalog.Fixture.Store;
        var versions = store.StoredContentVersionReads;
        var states = store.MediaStateReads;
        var snapshots = store.CandidateSnapshotReads;
        var access = catalog.Fixture.AccessChecks;
        var response = catalog.Ask(reference, "jointAudioVideo", new SimilarRequest { GroupByVideo = true, Limit = 3, CollapseRuns = true });

        Assert.Equal(0, catalog.Calls);
        Assert.Equal(15, response.Results.Count);
        Assert.Equal(15, response.Results.Sum(hit => hit.Run!.MemberCount));
        Assert.All(response.Results, hit =>
        {
            Assert.Equal("candidate", hit.Quality);
            Assert.True(hit.IndexComplete);
            Assert.True(hit.Score >= 0);
        });
        Assert.Equal(sourceCount, facts.Count);
        Assert.All(facts.Values, count => Assert.Equal(2, count));
        Assert.Equal(sourceCount * 4, store.StoredContentVersionReads - versions);
        Assert.Equal(3, store.MediaStateReads - states);
        Assert.Equal(2, store.CandidateSnapshotReads - snapshots);
        Assert.Equal(sourceCount * 2, catalog.Fixture.AccessChecks - access);

        var firstItems = response.Results.Select(hit => hit.ItemId).ToHashSet();
        var victim = firstItems.First(item => item != reference.ItemId);
        catalog.Fixture.Allow.Remove(victim);
        facts.Clear();
        versions = store.StoredContentVersionReads;
        states = store.MediaStateReads;
        snapshots = store.CandidateSnapshotReads;
        access = catalog.Fixture.AccessChecks;
        var again = catalog.Ask(reference, "jointAudioVideo", new SimilarRequest { GroupByVideo = true, Limit = 3, CollapseRuns = true });
        Assert.Equal(15, again.Results.Count);
        Assert.DoesNotContain(again.Results, hit => hit.ItemId == victim);
        Assert.Contains(again.Results, hit => !firstItems.Contains(hit.ItemId));
        Assert.Equal(sourceCount - 1, facts.Count);
        Assert.DoesNotContain(victim, facts.Keys);
        Assert.All(facts.Values, count => Assert.Equal(2, count));
        Assert.Equal((sourceCount - 1) * 4, store.StoredContentVersionReads - versions);
        Assert.Equal(3, store.MediaStateReads - states);
        Assert.Equal(2, store.CandidateSnapshotReads - snapshots);
        Assert.Equal(sourceCount * 2, catalog.Fixture.AccessChecks - access);
        Assert.Equal(0, catalog.Calls);
    }

    [Fact]
    public void Distinct_source_version_and_library_do_not_share_a_verdict()
    {
        using var catalog = Catalog.Create();
        var vector = Axis(0);
        var library = catalog.LibraryId;
        var otherLibrary = Guid.NewGuid();
        var referenceItem = Guid.NewGuid();
        var versioned = Guid.NewGuid();
        var splitSource = Guid.NewGuid();
        var splitLibrary = Guid.NewGuid();
        var reference = Window(referenceItem, "film", library, "v1", 0, vector);
        var staleVersion = Window(versioned, "film", library, "v2", 1000, vector);
        var currentVersion = Window(versioned, "film", library, "v1", 2000, vector);
        var hiddenSource = Window(splitSource, "extra", library, "v1", 3000, vector);
        var keptSource = Window(splitSource, "film", library, "v1", 4000, vector);
        var outsideLibrary = Window(splitLibrary, "film", otherLibrary, "v1", 5000, vector);
        var insideLibrary = Window(splitLibrary, "film", library, "v1", 6000, vector);
        catalog.Persist(referenceItem, "film", library, "v1");
        catalog.Persist(versioned, "film", library, "v1");
        catalog.Persist(splitSource, "extra", library, "v1");
        catalog.Persist(splitSource, "film", library, "v1");
        catalog.Persist(splitLibrary, "film", library, "v1");
        catalog.Adopt([reference, staleVersion, currentVersion, hiddenSource, keptSource, outsideLibrary, insideLibrary]);
        catalog.Save();
        catalog.Fixture.Store.HideItem(splitSource, "extra");

        var response = catalog.Ask(reference, "jointAudioVideo", new SimilarRequest { GroupByVideo = false, Limit = 50 });
        var returned = response.Results.Select(hit => hit.SegmentId).ToHashSet();
        Assert.Contains(currentVersion.SegmentId, returned);
        Assert.Contains(keptSource.SegmentId, returned);
        Assert.Contains(insideLibrary.SegmentId, returned);
        Assert.DoesNotContain(staleVersion.SegmentId, returned);
        Assert.DoesNotContain(hiddenSource.SegmentId, returned);
        Assert.DoesNotContain(outsideLibrary.SegmentId, returned);
        Assert.Equal(0, catalog.Calls);
    }

    [Fact]
    public void Fresh_revocation_filters_the_ranked_list_before_choosing_the_page()
    {
        using var catalog = Catalog.Create();
        var reference = catalog.Add("joint", catalog.FilmA, "film", 0, 120, Axis(0));
        var targetHigh = catalog.Add("joint", catalog.FilmB, "film", 2000, 2120, Tilt(0.95));
        catalog.Add("joint", catalog.FilmB, "film", 3000, 3120, Tilt(0.9));
        var filler = catalog.Add("joint", Guid.NewGuid(), "film", 8000, 8120, Tilt(0.5));
        var backup = catalog.Add("joint", Guid.NewGuid(), "film", 9000, 9120, Tilt(0.3));
        catalog.Persist(filler.ItemId, "film", catalog.LibraryId, "v1");
        catalog.Persist(backup.ItemId, "film", catalog.LibraryId, "v1");
        catalog.Save();
        catalog.Fixture.OnReadFacts = (item, _) =>
        {
            if (item == catalog.FilmB)
            {
                catalog.Fixture.Allow.Remove(catalog.FilmB);
            }
        };

        var published = catalog.Ask(reference, "jointAudioVideo", new SimilarRequest { GroupByVideo = true, Limit = 2 });
        Assert.Equal(new[] { filler.ItemId, backup.ItemId }, published.Results.Select(hit => hit.ItemId).Distinct().ToArray());
        Assert.DoesNotContain(published.Results, hit => hit.ItemId == catalog.FilmB || hit.SegmentId == targetHigh.SegmentId);
        Assert.Equal(0, catalog.Calls);

        using var hidden = Catalog.Create();
        var kept = hidden.Add("joint", hidden.FilmA, "film", 0, 120, Axis(0));
        hidden.Add("joint", hidden.FilmA, "film", 5000, 5120, Axis(0));
        hidden.Add("joint", hidden.FilmB, "film", 7000, 7120, Axis(0));
        hidden.Save();
        hidden.Fixture.OnReadFacts = (item, _) =>
        {
            if (item == hidden.FilmA)
            {
                hidden.Fixture.Allow.Remove(hidden.FilmA);
            }
        };
        var lost = Assert.Throws<SearchAuthException>(() => hidden.Ask(kept, "jointAudioVideo"));
        Assert.Equal(404, lost.Status);
        Assert.Equal("Segment was not found.", lost.Message);
        Assert.Equal(2, hidden.Fixture.Store.CandidateSnapshotReads);
    }

    [Fact]
    public void Fresh_library_removal_backfills_and_a_new_signature_is_rejected()
    {
        using var catalog = Catalog.Create();
        var otherLibrary = Guid.NewGuid();
        var reference = Window(catalog.FilmA, "film", catalog.LibraryId, "v1", 0, Axis(0));
        var moved = Window(catalog.FilmB, "film", otherLibrary, "v1", 2000, Tilt(0.95));
        var filler = Window(Guid.NewGuid(), "film", catalog.LibraryId, "v1", 8000, Tilt(0.5));
        var backup = Window(Guid.NewGuid(), "film", catalog.LibraryId, "v1", 9000, Tilt(0.3));
        catalog.Persist(reference.ItemId, reference.MediaSourceId, catalog.LibraryId, "v1");
        catalog.Persist(moved.ItemId, moved.MediaSourceId, otherLibrary, "v1");
        catalog.Persist(filler.ItemId, filler.MediaSourceId, catalog.LibraryId, "v1");
        catalog.Persist(backup.ItemId, backup.MediaSourceId, catalog.LibraryId, "v1");
        catalog.Adopt([reference, moved, filler, backup]);
        catalog.Fixture.Store.ReplaceLibraries([catalog.LibraryId, otherLibrary]);
        catalog.Save();
        catalog.Fixture.OnReadFacts = (item, _) =>
        {
            if (item == reference.ItemId)
            {
                catalog.Fixture.Store.ReplaceLibraries([catalog.LibraryId]);
            }
        };

        var published = catalog.Ask(reference, "jointAudioVideo", new SimilarRequest { GroupByVideo = true, Limit = 2 });
        Assert.Equal(new[] { filler.ItemId, backup.ItemId }, published.Results.Select(hit => hit.ItemId).Distinct().ToArray());
        Assert.DoesNotContain(published.Results, hit => hit.ItemId == moved.ItemId);

        using var signed = Catalog.Create();
        var visible = signed.Add("joint", signed.FilmA, "film", 0, 120, Axis(0));
        signed.Add("joint", signed.FilmB, "film", 5000, 5120, Axis(0));
        signed.Save();
        var rebuilt = false;
        signed.Fixture.OnReadFacts = (item, _) =>
        {
            if (!rebuilt && item != visible.ItemId)
            {
                rebuilt = true;
                signed.Fixture.Store.BeginRebuild(Fixture.Model("proc-a"));
            }
        };
        var blocked = Assert.Throws<SearchInputException>(() => signed.Ask(visible, "jointAudioVideo"));
        Assert.Equal("incompatible", blocked.Code);
        Assert.True(rebuilt);
        Assert.Equal(2, signed.Fixture.Store.CandidateSnapshotReads);
        Assert.Equal(0, catalog.Calls + signed.Calls);
    }

    [Fact]
    public void Cancellation_does_not_start_the_next_source_or_the_second_snapshot()
    {
        using var catalog = Catalog.Create();
        var reference = catalog.Add("joint", catalog.FilmA, "film", 0, 120, Axis(0));
        catalog.Add("joint", catalog.FilmA, "film", 5000, 5120, Axis(0));
        var other = catalog.Add("joint", catalog.FilmB, "film", 8000, 8120, Axis(0));
        catalog.Save();
        using var during = new CancellationTokenSource();
        var probed = new List<Guid>();
        catalog.Fixture.OnReadFacts = (item, _) =>
        {
            probed.Add(item);
            during.Cancel();
        };
        var snapshots = catalog.Fixture.Store.CandidateSnapshotReads;
        var versions = catalog.Fixture.Store.StoredContentVersionReads;
        var access = catalog.Fixture.AccessChecks;
        Assert.Throws<OperationCanceledException>(() => catalog.Fixture.Runtime.Similar(
            catalog.Fixture.UserId, catalog.Request(reference, "jointAudioVideo"), during.Token));
        Assert.Equal(new[] { reference.ItemId }, probed);
        Assert.DoesNotContain(other.ItemId, probed);
        Assert.Equal(snapshots + 1, catalog.Fixture.Store.CandidateSnapshotReads);
        Assert.Equal(versions + 2, catalog.Fixture.Store.StoredContentVersionReads);
        Assert.Equal(access + 1, catalog.Fixture.AccessChecks);
        Assert.Equal(0, catalog.Calls);
    }

    [Fact]
    public async Task Real_scale_search_reads_each_source_once_per_pass()
    {
        // The old search walked every joint and text window twice: (8179 + 3293) * 2 checks,
        // then read media state for every positive hit. Counts below must follow the 46 sources.
        const int jointCount = 8179;
        const int textCount = 3293;
        const int sourceCount = 46;
        using var catalog = Catalog.Create();
        var shared = Axis(0);
        var films = Enumerable.Range(0, sourceCount).Select(_ => Guid.NewGuid()).ToArray();
        var joint = new List<CandidateRow>(jointCount);
        var text = new List<CandidateRow>(textCount);
        var jointOrdinal = new int[sourceCount];
        var textOrdinal = new int[sourceCount];
        for (var index = 0; index < jointCount; index++)
        {
            var sourceIndex = index % sourceCount;
            var start = jointOrdinal[sourceIndex]++ * 121L;
            joint.Add(Window(films[sourceIndex], "film", catalog.LibraryId, "v1", start, shared));
        }

        for (var index = 0; index < textCount; index++)
        {
            var sourceIndex = index % sourceCount;
            var start = 1_000_000 + (textOrdinal[sourceIndex]++ * 121L);
            text.Add(Window(films[sourceIndex], "film", catalog.LibraryId, "v1", start, shared, "text"));
        }

        foreach (var film in films)
        {
            catalog.Persist(film, "film", catalog.LibraryId, "v1");
        }

        catalog.Adopt(joint, text);
        catalog.Save();
        var facts = new Dictionary<Guid, int>();
        catalog.Fixture.OnReadFacts = (item, _) => facts[item] = facts.GetValueOrDefault(item) + 1;
        var store = catalog.Fixture.Store;
        var versions = store.StoredContentVersionReads;
        var states = store.MediaStateReads;
        var snapshots = store.CandidateSnapshotReads;
        var access = catalog.Fixture.AccessChecks;
        var plain = new SearchRequest { GroupByVideo = true, Limit = 3, CollapseRuns = true };
        var response = await catalog.Search("scale-search", plain, TestContext.Current.CancellationToken);

        Assert.Equal("referenceTailR", response.QualityPolicy.ScoreMeaning);
        Assert.Equal(1, catalog.Calls);
        Assert.Equal(15, response.Results.Count);
        Assert.Equal(15, response.Results.Sum(hit => hit.Run!.MemberCount));
        Assert.Null(response.RawResults);
        Assert.All(response.Results, hit =>
        {
            Assert.NotNull(hit.Run);
            Assert.Equal("candidate", hit.Quality);
            Assert.True(hit.IndexComplete);
            Assert.True(hit.Score >= 0);
        });
        Assert.Equal(sourceCount, facts.Count);
        Assert.All(facts.Values, count => Assert.Equal(2, count));
        Assert.Equal(sourceCount * 4, store.StoredContentVersionReads - versions);
        Assert.Equal(3, store.MediaStateReads - states);
        Assert.Equal(3, store.CandidateSnapshotReads - snapshots);
        Assert.Equal(sourceCount * 2, catalog.Fixture.AccessChecks - access);

        states = store.MediaStateReads;
        var diagnosed = await catalog.Search("scale-diag", new SearchRequest
        {
            GroupByVideo = true, Limit = 3, CollapseRuns = true, Diagnostics = true
        }, TestContext.Current.CancellationToken);
        var raw = diagnosed.RawResults;
        Assert.NotNull(raw);
        Assert.Equal(jointCount + textCount, raw.Count);
        Assert.All(raw, hit => Assert.Null(hit.Run));
        Assert.All(raw, hit => Assert.True(hit.IndexComplete));
        Assert.All(diagnosed.Results, hit => Assert.True(hit.IndexComplete));
        Assert.Equal(sourceCount, store.MediaStateReads - states);
        Assert.True(store.MediaStateReads - states < raw.Count);

        var firstItems = response.Results.Select(hit => hit.ItemId).ToHashSet();
        var victim = response.Results.Select(hit => hit.ItemId).First(item => item != response.Results[0].ItemId);
        catalog.Fixture.Allow.Remove(victim);
        facts.Clear();
        versions = store.StoredContentVersionReads;
        states = store.MediaStateReads;
        snapshots = store.CandidateSnapshotReads;
        access = catalog.Fixture.AccessChecks;
        var again = await catalog.Search("scale-search-again", plain, TestContext.Current.CancellationToken);
        Assert.Equal(15, again.Results.Count);
        Assert.Equal(15, again.Results.Sum(hit => hit.Run!.MemberCount));
        Assert.DoesNotContain(again.Results, hit => hit.ItemId == victim);
        Assert.Contains(again.Results, hit => !firstItems.Contains(hit.ItemId));
        Assert.Equal(sourceCount - 1, facts.Count);
        Assert.DoesNotContain(victim, facts.Keys);
        Assert.All(facts.Values, count => Assert.Equal(2, count));
        Assert.Equal((sourceCount - 1) * 4, store.StoredContentVersionReads - versions);
        Assert.Equal(3, store.MediaStateReads - states);
        Assert.Equal(3, store.CandidateSnapshotReads - snapshots);
        Assert.Equal(sourceCount * 2, catalog.Fixture.AccessChecks - access);
        Assert.Equal("referenceTailR", again.QualityPolicy.ScoreMeaning);
        Assert.Equal(3, catalog.Calls);
    }

    [Fact]
    public async Task Search_distinct_source_version_and_library_do_not_share_a_verdict()
    {
        using var catalog = Catalog.Create();
        var vector = Axis(0);
        var library = catalog.LibraryId;
        var otherLibrary = Guid.NewGuid();
        var opened = Guid.NewGuid();
        var versioned = Guid.NewGuid();
        var splitSource = Guid.NewGuid();
        var splitLibrary = Guid.NewGuid();
        var first = Window(opened, "film", library, "v1", 0, vector);
        var staleVersion = Window(versioned, "film", library, "v2", 1000, vector);
        var currentVersion = Window(versioned, "film", library, "v1", 2000, vector);
        var hiddenSource = Window(splitSource, "extra", library, "v1", 3000, vector);
        var keptSource = Window(splitSource, "film", library, "v1", 4000, vector);
        var outsideLibrary = Window(splitLibrary, "film", otherLibrary, "v1", 5000, vector);
        var insideLibrary = Window(splitLibrary, "film", library, "v1", 6000, vector);
        catalog.Persist(opened, "film", library, "v1");
        catalog.Persist(versioned, "film", library, "v1");
        catalog.Persist(splitSource, "extra", library, "v1");
        catalog.Persist(splitSource, "film", library, "v1");
        catalog.Persist(splitLibrary, "film", library, "v1");
        catalog.Adopt([first, staleVersion, currentVersion, hiddenSource, keptSource, outsideLibrary, insideLibrary]);
        catalog.Save();
        catalog.Fixture.Store.HideItem(splitSource, "extra");

        var response = await catalog.Search("distinct-search", new SearchRequest { GroupByVideo = false, Limit = 50 }, TestContext.Current.CancellationToken);
        var returned = response.Results.Select(hit => hit.SegmentId).ToHashSet();
        Assert.Equal(4, response.Results.Count);
        Assert.Contains(first.SegmentId, returned);
        Assert.Contains(currentVersion.SegmentId, returned);
        Assert.Contains(keptSource.SegmentId, returned);
        Assert.Contains(insideLibrary.SegmentId, returned);
        Assert.DoesNotContain(staleVersion.SegmentId, returned);
        Assert.DoesNotContain(hiddenSource.SegmentId, returned);
        Assert.DoesNotContain(outsideLibrary.SegmentId, returned);
        Assert.Equal("referenceTailR", response.QualityPolicy.ScoreMeaning);
        Assert.Equal(1, catalog.Calls);
    }

    [Fact]
    public async Task Fresh_search_revocation_filters_the_ranked_list_before_choosing_the_page()
    {
        using var catalog = Catalog.Create();
        catalog.Add("joint", catalog.FilmA, "film", 0, 120, Tilt(0.1));
        var blocked = catalog.Add("joint", catalog.FilmB, "film", 2000, 2120, Tilt(0.95));
        catalog.Add("joint", catalog.FilmB, "film", 3000, 3120, Tilt(0.9));
        var filler = catalog.Add("joint", Guid.NewGuid(), "film", 8000, 8120, Tilt(0.5));
        var backup = catalog.Add("joint", Guid.NewGuid(), "film", 9000, 9120, Tilt(0.3));
        catalog.Save();
        catalog.Fixture.OnReadFacts = (item, _) =>
        {
            if (item == catalog.FilmB)
            {
                catalog.Fixture.Allow.Remove(catalog.FilmB);
            }
        };

        var published = await catalog.Search("revoked-search", new SearchRequest { GroupByVideo = true, Limit = 2 }, TestContext.Current.CancellationToken);
        Assert.Equal(new[] { filler.ItemId, backup.ItemId }, published.Results.Select(hit => hit.ItemId).Distinct().ToArray());
        Assert.DoesNotContain(published.Results, hit => hit.ItemId == catalog.FilmB || hit.SegmentId == blocked.SegmentId);
        Assert.Equal(1, catalog.Calls);
    }

    [Fact]
    public async Task Fresh_search_library_removal_backfills_and_a_new_signature_is_rejected()
    {
        using var catalog = Catalog.Create();
        var otherLibrary = Guid.NewGuid();
        var weak = Window(catalog.FilmA, "film", catalog.LibraryId, "v1", 0, Tilt(0.1));
        var moved = Window(catalog.FilmB, "film", otherLibrary, "v1", 2000, Tilt(0.95));
        var filler = Window(Guid.NewGuid(), "film", catalog.LibraryId, "v1", 8000, Tilt(0.5));
        var backup = Window(Guid.NewGuid(), "film", catalog.LibraryId, "v1", 9000, Tilt(0.3));
        catalog.Persist(weak.ItemId, weak.MediaSourceId, catalog.LibraryId, "v1");
        catalog.Persist(moved.ItemId, moved.MediaSourceId, otherLibrary, "v1");
        catalog.Persist(filler.ItemId, filler.MediaSourceId, catalog.LibraryId, "v1");
        catalog.Persist(backup.ItemId, backup.MediaSourceId, catalog.LibraryId, "v1");
        catalog.Adopt([weak, moved, filler, backup]);
        catalog.Fixture.Store.ReplaceLibraries([catalog.LibraryId, otherLibrary]);
        catalog.Save();
        catalog.Fixture.OnReadFacts = (item, _) =>
        {
            if (item == weak.ItemId)
            {
                catalog.Fixture.Store.ReplaceLibraries([catalog.LibraryId]);
            }
        };

        var published = await catalog.Search("library-search", new SearchRequest { GroupByVideo = true, Limit = 2 }, TestContext.Current.CancellationToken);
        Assert.Equal(new[] { filler.ItemId, backup.ItemId }, published.Results.Select(hit => hit.ItemId).Distinct().ToArray());
        Assert.DoesNotContain(published.Results, hit => hit.ItemId == moved.ItemId);

        using var signed = Catalog.Create();
        var visible = signed.Add("joint", signed.FilmA, "film", 0, 120, Axis(0));
        signed.Add("joint", signed.FilmB, "film", 5000, 5120, Axis(0));
        signed.Save();
        var rebuilt = false;
        var snapshots = signed.Fixture.Store.CandidateSnapshotReads;
        signed.Fixture.OnReadFacts = (item, _) =>
        {
            if (!rebuilt && item != visible.ItemId)
            {
                rebuilt = true;
                signed.Fixture.Store.BeginRebuild(Fixture.Model("proc-a"));
            }
        };
        var blocked = await signed.Search("signature-search", cancellationToken: TestContext.Current.CancellationToken);
        Assert.Equal("incompatible", blocked.Availability.Search);
        Assert.Empty(blocked.Results);
        Assert.True(rebuilt);
        Assert.Equal(snapshots + 3, signed.Fixture.Store.CandidateSnapshotReads);
        Assert.Equal(2, catalog.Calls + signed.Calls);
    }

    [Fact]
    public async Task Search_cancellation_does_not_start_the_next_source_or_the_fresh_snapshot()
    {
        using var catalog = Catalog.Create();
        var first = catalog.Add("joint", catalog.FilmA, "film", 0, 120, Axis(0));
        catalog.Add("joint", catalog.FilmA, "film", 5000, 5120, Axis(0));
        var other = catalog.Add("joint", catalog.FilmB, "film", 8000, 8120, Axis(0));
        catalog.Save();
        using var during = new CancellationTokenSource();
        var probed = new List<Guid>();
        catalog.Fixture.OnReadFacts = (item, _) =>
        {
            probed.Add(item);
            during.Cancel();
        };
        var snapshots = catalog.Fixture.Store.CandidateSnapshotReads;
        var versions = catalog.Fixture.Store.StoredContentVersionReads;
        var access = catalog.Fixture.AccessChecks;
        await Assert.ThrowsAsync<OperationCanceledException>(() => catalog.Search("cancel-search", cancellationToken: during.Token));
        Assert.Equal(new[] { first.ItemId }, probed);
        Assert.DoesNotContain(other.ItemId, probed);
        Assert.Equal(snapshots + 2, catalog.Fixture.Store.CandidateSnapshotReads);
        Assert.Equal(versions + 2, catalog.Fixture.Store.StoredContentVersionReads);
        Assert.Equal(access + 1, catalog.Fixture.AccessChecks);
        Assert.Equal(1, catalog.Calls);
    }

    [Fact]
    public async Task Main_diagnostics_keeps_partial_coverage_for_off_page_sources()
    {
        using var catalog = Catalog.Create();
        for (var index = 0; index < 4; index++)
        {
            catalog.Add("joint", Guid.NewGuid(), "film", 0, 120, Axis(0));
        }

        catalog.Save();
        using (var connection = new SqliteConnection("Data Source=" + catalog.Fixture.Store.DatabasePath))
        {
            connection.Open();
            using var command = connection.CreateCommand();
            command.CommandText = "UPDATE media SET state = 'partial'";
            command.ExecuteNonQuery();
        }

        var response = await catalog.Fixture.Runtime.SearchAsync(catalog.Fixture.UserId, new SearchRequest
        {
            Query = "coverage-regression",
            GroupByVideo = true,
            Limit = 1,
            Diagnostics = true
        }, TestContext.Current.CancellationToken);
        Assert.Single(response.Results);
        Assert.False(response.Results[0].IndexComplete);
        var raw = response.RawResults;
        Assert.NotNull(raw);
        Assert.Equal(4, raw.Count);
        Assert.All(raw, hit => Assert.False(hit.IndexComplete));
    }

    [Fact]
    public async Task Diagnostics_reports_real_coverage_on_and_off_the_page()
    {
        using var catalog = Catalog.Create();
        var shown = catalog.Add("joint", Guid.NewGuid(), "film", 0, 120, Tilt(0.99));
        var partial = catalog.Add("joint", Guid.NewGuid(), "film", 2000, 2120, Tilt(0.5));
        catalog.Add("joint", partial.ItemId, "film", 4000, 4120, Tilt(0.4));
        var complete = catalog.Add("joint", Guid.NewGuid(), "film", 8000, 8120, Tilt(0.2));
        catalog.Save();
        using (var connection = new SqliteConnection("Data Source=" + catalog.Fixture.Store.DatabasePath))
        {
            connection.Open();
            using var command = connection.CreateCommand();
            command.CommandText = "UPDATE media SET state = 'partial' WHERE item_id = $item";
            command.Parameters.AddWithValue("$item", partial.ItemId.ToString("D"));
            command.ExecuteNonQuery();
        }

        var states = catalog.Fixture.Store.MediaStateReads;
        var response = await catalog.Fixture.Runtime.SearchAsync(catalog.Fixture.UserId, new SearchRequest
        {
            Query = "mixed-coverage",
            GroupByVideo = true,
            Limit = 1,
            Diagnostics = true
        }, TestContext.Current.CancellationToken);
        var raw = response.RawResults;
        Assert.NotNull(raw);
        Assert.Equal(4, raw.Count);
        Assert.Equal(3, catalog.Fixture.Store.MediaStateReads - states);
        Assert.True(catalog.Fixture.Store.MediaStateReads - states < raw.Count);
        var page = Assert.Single(response.Results);
        Assert.Equal(shown.ItemId, page.ItemId);
        Assert.True(page.IndexComplete);
        Assert.Contains(raw, hit => hit.ItemId == shown.ItemId && hit.IndexComplete);
        Assert.Equal(2, raw.Count(hit => hit.ItemId == partial.ItemId && !hit.IndexComplete));
        Assert.Contains(raw, hit => hit.ItemId == complete.ItemId && hit.IndexComplete);
        Assert.Contains(raw, hit => hit.ItemId != shown.ItemId && hit.IndexComplete);
        Assert.Contains(raw, hit => hit.ItemId != shown.ItemId && !hit.IndexComplete);
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

    private static CandidateRow Window(Guid item, string source, Guid library, string version, long start, double[] vector, string channel = "joint")
    {
        var end = start + 120;
        return new CandidateRow
        {
            SegmentId = Guid.NewGuid(),
            CandidateId = channel + ":" + item.ToString("N") + ":" + source + ":" + version + ":" + start,
            ItemId = item,
            MediaSourceId = source,
            LibraryId = library,
            ContentVersion = version,
            StartTicks = start,
            EndTicks = end,
            RuntimeTicks = 50_000_000_000,
            Canonical = new CanonicalAnchor { SourceItemId = item, StartTicks = start, EndTicks = end },
            AnchorId = $"{item:N}:{start}:{end}",
            Vector = vector
        };
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

        public void Adopt(IReadOnlyList<CandidateRow> rows, IReadOnlyList<CandidateRow>? text = null)
        {
            Joint.Candidates = rows.ToArray();
            if (text is not null)
            {
                Text.Candidates = text.ToArray();
            }
        }

        public Task<SearchResponse> Search(string query, SearchRequest? template = null, CancellationToken cancellationToken = default)
        {
            var request = template ?? new SearchRequest();
            request.Query = query;
            return Fixture.Runtime.SearchAsync(Fixture.UserId, request, cancellationToken);
        }

        public void Persist(Guid item, string source, Guid library, string version)
        {
            if (!_media.Add((item, source)))
            {
                return;
            }

            Fixture.Allow.Add(item);
            Fixture.Store.SaveSucceededWindow(item, source, library, version, 50_000_000_000, 0, 12_000_000, CoreTests.Axis(0), null);
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
