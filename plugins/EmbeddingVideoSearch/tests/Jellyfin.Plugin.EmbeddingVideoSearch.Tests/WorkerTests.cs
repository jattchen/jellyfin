using System.Diagnostics;
using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class WorkerTests
{
    [Fact]
    public async Task One_synthetic_window_is_searchable_before_the_video_is_complete()
    {
        var ffmpeg = FindTool("ffmpeg");
        var ffprobe = FindTool("ffprobe");
        Assert.False(string.IsNullOrEmpty(ffmpeg));
        Assert.False(string.IsNullOrEmpty(ffprobe));
        var root = Directory.CreateTempSubdirectory("evs-worker-").FullName;
        var media = Path.Combine(root, "synthetic.mp4");
        await Run(ffmpeg!, ["-y", "-f", "lavfi", "-i", "testsrc=duration=10:size=160x120:rate=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", media], CancellationToken.None);

        await using var server = StubServer.Start();
        server.ReportProcessing = true;
        server.ReportedDuration = 6;
        using var store = IndexStore.Open(Path.Combine(root, "index"));
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-test"));
        store.SetEmbeddingBaseUrl(server.BaseUrl);
        store.ReplaceLibraries([library]);
        var file = new FileInfo(media);
        var version = ContentVersion.FromMediaFacts(file.Length, TimeSpan.FromSeconds(10).Ticks, new DateTimeOffset(file.LastWriteTimeUtc), "mp4", 1);
        store.UpsertVideo(item, "media-1", library, TimeSpan.FromSeconds(10).Ticks, version, media, true, 1);
        var catalog = new MemoryCatalog();
        catalog.Videos.Add(new IndexableVideo { ItemId = item, MediaSourceId = "media-1", LibraryId = library, RuntimeTicks = TimeSpan.FromSeconds(10).Ticks, ContentVersion = version, SourcePath = media, HasAudio = true, AudioStreamIndex = 1 });
        Assert.Equal(2, store.CountWindows("pending"));
        var arguments = FfmpegWindowClipper.BuildArguments(media, 4, 6, "out.mp4", 1);
        var inputAt = Array.IndexOf(arguments, "-i");
        Assert.True(Array.IndexOf(arguments, "-ss") < inputAt);
        Assert.True(Array.IndexOf(arguments, "-accurate_seek") < inputAt);
        Assert.Contains("0:1", arguments);

        var coordinator = new IndexCoordinator(store, new HttpEmbeddingClient(), catalog, new FfmpegWindowClipper(ffmpeg!, ffprobe!), Path.Combine(root, "tmp"));
        var first = await coordinator.DispatchOneAsync(CancellationToken.None);
        Assert.Equal(DispatchStep.Published, first);
        Assert.Equal(1, store.CountWindows("succeeded"));
        Assert.Equal(1, store.CountWindows("pending"));
        using var runtime = new SearchRuntime(store, new FakeEmbeddings { ProcessingId = "proc-test" }, new AllowAny(), new SingleLibrary(library), new Version(12, 2, 0), new EchoFacts(store));
        var partial = await runtime.SearchAsync(AllowAny.User, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        var hit = Assert.Single(partial.Results);
        Assert.False(hit.IndexComplete);
        Assert.Equal(0, hit.StartTicks);
        Assert.Equal(ApiV1.WindowTicks, hit.EndTicks);

        var second = await coordinator.DispatchOneAsync(CancellationToken.None);
        Assert.Equal(DispatchStep.Published, second);
        var done = await runtime.SearchAsync(AllowAny.User, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        Assert.Equal(2, done.Results.Count);
        Assert.All(done.Results, result => Assert.True(result.IndexComplete));
        Assert.True(done.IndexComplete);
    }

    [Fact]
    public async Task Incomplete_processing_does_not_publish_the_window()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-bad-").FullName);
        var library = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.SetEmbeddingBaseUrl("http://127.0.0.1");
        store.ReplaceLibraries([library]);
        store.UpsertVideo(Guid.NewGuid(), "media-1", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", true);
        var model = new ScriptedModel { Duration = 1 };
        var coordinator = new IndexCoordinator(store, model, new MemoryCatalog(), new AcceptingClipper(), Path.Combine(store.DatabasePath, "..", "tmp"));
        Assert.Equal(DispatchStep.Failed, await coordinator.DispatchOneAsync(CancellationToken.None));
        Assert.Equal(0, store.CountWindows("succeeded"));
        Assert.Equal(1, store.CountWindows("failed"));
    }

    [Fact]
    public async Task Expired_lease_can_be_retried_and_a_late_token_cannot_publish()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-lease-").FullName);
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.SetEmbeddingBaseUrl("http://127.0.0.1");
        store.ReplaceLibraries([library]);
        store.UpsertVideo(item, "media-1", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        var first = store.TryLease();
        Assert.NotNull(first);
        store.ExpireLease(first!.Token);
        var second = store.TryLease();
        Assert.NotNull(second);
        Assert.NotEqual(first.Token, second!.Token);
        Assert.Equal(LeaseOutcome.Stale, store.CompleteLease(first.Token, CoreTests.Axis(0)));
        Assert.Equal(LeaseOutcome.Published, store.CompleteLease(second.Token, CoreTests.Axis(0)));
        Assert.Equal(1, store.CountWindows("succeeded"));
        store.RecoverLeases();
        Assert.Equal(0, store.CountWindows("leased"));
    }

    [Fact]
    public void Start_reclaims_a_leased_window_and_a_later_pass_hides_a_missing_item()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-reconcile-").FullName);
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.ReplaceLibraries([library]);
        store.UpsertVideo(item, "media-1", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        Assert.NotNull(store.TryLease());
        var catalog = new MemoryCatalog();
        catalog.Videos.Add(new IndexableVideo
        {
            ItemId = item,
            MediaSourceId = "media-1",
            LibraryId = library,
            RuntimeTicks = ApiV1.WindowTicks,
            ContentVersion = "v1",
            SourcePath = "synthetic.mp4"
        });
        var coordinator = new IndexCoordinator(store, new ScriptedModel(), catalog, new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-reconcile-tmp-").FullName);
        coordinator.Start();
        Assert.Equal(0, store.CountWindows("leased"));
        coordinator.ProcessEvents();
        Assert.Equal(1, store.CountWindows("pending"));
        catalog.Videos.Clear();
        coordinator.ReconcileInterval = TimeSpan.Zero;
        coordinator.ProcessEvents();
        Assert.Equal(0, store.CountWindows("pending"));
        Assert.Equal(1, store.CountWindows("superseded"));
    }

    [Fact]
    public async Task Manual_pause_survives_playback_and_blocks_new_work()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-pause-").FullName);
        var library = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.SetEmbeddingBaseUrl("http://127.0.0.1");
        store.ReplaceLibraries([library]);
        var item = Guid.NewGuid();
        store.UpsertVideo(item, "media-1", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        var catalog = new MemoryCatalog();
        catalog.Videos.Add(new IndexableVideo { ItemId = item, MediaSourceId = "media-1", LibraryId = library, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "v1", SourcePath = "synthetic.mp4" });
        var coordinator = new IndexCoordinator(store, new ScriptedModel(), catalog, new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-pause-tmp-").FullName);
        store.SetManualPause(true);
        catalog.Playback = true;
        Assert.Equal(DispatchStep.Paused, await coordinator.DispatchOneAsync(CancellationToken.None));
        catalog.Playback = false;
        Assert.Equal(DispatchStep.Paused, await coordinator.DispatchOneAsync(CancellationToken.None));
        store.SetManualPause(false);
        catalog.Playback = true;
        Assert.Equal(DispatchStep.Playback, await coordinator.DispatchOneAsync(CancellationToken.None));
        catalog.Playback = false;
        Assert.Equal(DispatchStep.Published, await coordinator.DispatchOneAsync(CancellationToken.None));
    }

    [Fact]
    public void Delete_hides_rows_and_a_failed_read_does_not()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-change-").FullName);
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.ReplaceLibraries([library]);
        store.UpsertVideo(item, "media-1", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        store.MarkUnknown(item);
        Assert.Equal(1, store.CountWindows("pending"));
        store.Enqueue("removed", item);
        var coordinator = new IndexCoordinator(store, new ScriptedModel(), new MemoryCatalog(), new AcceptingClipper(), Directory.CreateTempSubdirectory().FullName);
        coordinator.ProcessEvents();
        Assert.Equal(0, store.CountWindows("pending"));
        Assert.Equal(1, store.CountWindows("superseded"));
    }

    [Fact]
    public async Task Rebuild_during_search_does_not_mark_the_new_generation_incompatible()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SaveSucceededWindow(fixture.AllowItem, "media-1", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), null);
        var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        fixture.Embeddings.Entered = () => entered.TrySetResult(true);
        fixture.Embeddings.Release = () => release.Task;
        var search = fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        await entered.Task;
        fixture.Store.BeginRebuild(Fixture.Model("proc-new"));
        var other = Guid.NewGuid();
        fixture.Allow.Add(other);
        fixture.Store.SaveSucceededWindow(other, "media-1", library, "v2", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(1), null);
        release.SetResult(true);
        var response = await search;
        Assert.False(fixture.Store.Incompatible);
        Assert.Empty(response.Results);
        Assert.Equal(2, fixture.Store.WindowCount());
    }

    [Fact]
    public async Task Disabling_the_user_during_search_is_rejected()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SaveSucceededWindow(fixture.AllowItem, "media-1", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), null);
        var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        fixture.Embeddings.Entered = () => entered.TrySetResult(true);
        fixture.Embeddings.Release = () => release.Task;
        var search = fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        await entered.Task;
        fixture.Disabled = true;
        release.SetResult(true);
        var error = await Assert.ThrowsAsync<SearchAuthException>(() => search);
        Assert.Equal(403, error.Status);
    }

    [Fact]
    public async Task Revoking_a_library_or_replacing_content_during_search_drops_the_old_hit()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SaveSucceededWindow(fixture.AllowItem, "media-1", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), null);
        var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        fixture.Embeddings.Entered = () => entered.TrySetResult(true);
        fixture.Embeddings.Release = () => release.Task;
        var search = fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        await entered.Task;
        fixture.Store.ReplaceLibraries([]);
        release.SetResult(true);
        var revoked = await search;
        Assert.Empty(revoked.Results);

        fixture.Store.ReplaceLibraries([library]);
        entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        fixture.Embeddings.Entered = () => entered.TrySetResult(true);
        fixture.Embeddings.Release = () => release.Task;
        search = fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        await entered.Task;
        fixture.Store.UpsertVideo(fixture.AllowItem, "media-1", library, ApiV1.WindowTicks, "replaced", "synthetic.mp4", false);
        release.SetResult(true);
        var replaced = await search;
        Assert.Empty(replaced.Results);
        Assert.Equal(1, fixture.Store.CountWindows("superseded"));
    }

    [Fact]
    public async Task Chunked_body_stops_at_the_byte_limit()
    {
        var stream = new CountingStream();
        var error = await Assert.ThrowsAsync<SearchInputException>(() => RequestBounds.ReadUtf8Async(stream, CancellationToken.None));
        Assert.Equal("invalidRequest", error.Code);
        Assert.True(stream.ReadBytes <= RequestBounds.MaxBytes + 1);
    }

    private static string? FindTool(string name)
    {
        foreach (var candidate in new[] { "/opt/homebrew/bin/" + name, "/usr/local/bin/" + name, "/usr/bin/" + name })
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    private static async Task Run(string fileName, IReadOnlyList<string> arguments, CancellationToken cancellationToken)
    {
        var ran = await CapturedProcess.RunAsync(fileName, arguments, cancellationToken);
        Assert.Equal(0, ran.ExitCode);
    }

    [Fact]
    public async Task Replacement_during_inference_does_not_publish_the_old_lease()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-race-file-").FullName);
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.SetEmbeddingBaseUrl("http://127.0.0.1");
        store.ReplaceLibraries([library]);
        store.UpsertVideo(item, "source-b", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        store.SaveSucceededWindow(item, "source-b", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), null);
        var catalog = new MemoryCatalog();
        catalog.Videos.Add(new IndexableVideo { ItemId = item, MediaSourceId = "source-a", LibraryId = library, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "v1", SourcePath = "other.mp4" });
        catalog.Videos.Add(new IndexableVideo { ItemId = item, MediaSourceId = "source-b", LibraryId = library, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "v2", SourcePath = "replaced.mp4" });
        using var runtime = new SearchRuntime(store, new FakeEmbeddings { ProcessingId = "proc-a" }, new AllowAny(), new SingleLibrary(library), new Version(12, 2, 0), catalog);
        var segmentId = IndexStore.SegmentId(item, "source-b", "v1", 0, ApiV1.WindowTicks, store.ActiveGeneration);
        Assert.NotEqual(200, runtime.GetSegment(AllowAny.User, segmentId).StatusCode);
        Assert.Empty((await runtime.SearchAsync(AllowAny.User, new SearchRequest { Query = "synthetic" }, CancellationToken.None)).Results);
        Assert.Equal(1, store.CountWindows("succeeded"));

        var fresh = Guid.NewGuid();
        catalog.Videos.Add(new IndexableVideo { ItemId = fresh, MediaSourceId = "source-a", LibraryId = library, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "v1", SourcePath = "other.mp4" });
        catalog.Videos.Add(new IndexableVideo { ItemId = fresh, MediaSourceId = "source-b", LibraryId = library, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "v1", SourcePath = "synthetic.mp4" });
        store.UpsertVideo(fresh, "source-b", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        var model = new ScriptedModel();
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        model.Started = () => entered.TrySetResult();
        model.Gate = release.Task;
        var coordinator = new IndexCoordinator(store, model, catalog, new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-race-file-tmp-").FullName);
        var dispatch = coordinator.DispatchOneAsync(CancellationToken.None);
        await entered.Task.WaitAsync(TimeSpan.FromSeconds(5), TestContext.Current.CancellationToken);
        var freshSource = catalog.Videos.FindIndex(video => video.ItemId == fresh && video.MediaSourceId == "source-b");
        catalog.Videos[freshSource] = new IndexableVideo { ItemId = fresh, MediaSourceId = "source-b", LibraryId = library, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "v2", SourcePath = "replaced.mp4" };
        catalog.Title = "renamed-and-replaced";
        coordinator.Enqueue("updated", fresh);
        release.TrySetResult();
        var step = await dispatch.WaitAsync(TimeSpan.FromSeconds(5), TestContext.Current.CancellationToken);
        Assert.NotEqual(DispatchStep.Published, step);
        Assert.Equal(1, store.CountWindows("succeeded"));
        Assert.NotEqual(200, runtime.GetSegment(AllowAny.User, segmentId).StatusCode);
        store.HideItem(fresh);

        var titled = Guid.NewGuid();
        catalog.Videos.Add(new IndexableVideo { ItemId = titled, MediaSourceId = "source-b", LibraryId = library, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "v1", SourcePath = "synthetic.mp4" });
        catalog.Title = "title only";
        store.UpsertVideo(titled, "source-b", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        model.Started = () => entered.TrySetResult();
        model.Gate = release.Task;
        dispatch = coordinator.DispatchOneAsync(CancellationToken.None);
        await entered.Task.WaitAsync(TimeSpan.FromSeconds(5), TestContext.Current.CancellationToken);
        catalog.Title = "renamed";
        coordinator.Enqueue("updated", titled);
        release.TrySetResult();
        Assert.Equal(DispatchStep.Published, await dispatch.WaitAsync(TimeSpan.FromSeconds(5), TestContext.Current.CancellationToken));
        var kept = IndexStore.SegmentId(titled, "source-b", "v1", 0, ApiV1.WindowTicks, store.ActiveGeneration);
        Assert.Equal(200, runtime.GetSegment(AllowAny.User, kept).StatusCode);
    }

    [Fact]
    public async Task Unreadable_media_blocks_output_without_deleting_the_row()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-unknown-file-").FullName);
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.ReplaceLibraries([library]);
        store.SaveSucceededWindow(item, "media-1", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), null);
        var catalog = new MemoryCatalog();
        catalog.UnreadableSources.Add("media-1");
        using var runtime = new SearchRuntime(store, new FakeEmbeddings { ProcessingId = "proc-a" }, new AllowAny(), new SingleLibrary(library), new Version(12, 2, 0), catalog);
        var segmentId = IndexStore.SegmentId(item, "media-1", "v1", 0, ApiV1.WindowTicks, store.ActiveGeneration);
        var decision = runtime.GetSegment(AllowAny.User, segmentId);
        Assert.Equal(409, decision.StatusCode);
        Assert.Equal("unknown", decision.Code);
        Assert.Empty((await runtime.SearchAsync(AllowAny.User, new SearchRequest { Query = "synthetic" }, CancellationToken.None)).Results);
        Assert.Equal(1, store.CountWindows("succeeded"));
    }

    [Fact]
    public async Task Unsupported_host_does_not_start_or_dispatch()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-host13-").FullName);
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.ReplaceLibraries([library]);
        store.UpsertVideo(item, "media-1", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        Assert.NotNull(store.TryLease());
        var coordinator = new IndexCoordinator(store, new ScriptedModel(), new MemoryCatalog(), new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-host13-tmp-").FullName, hostVersion: new Version(13, 0, 0));
        coordinator.Start();
        Assert.Equal("hostIncompatible", coordinator.DisabledReason);
        Assert.Equal(1, store.CountWindows("leased"));
        coordinator.Enqueue("removed", item);
        coordinator.ProcessEvents();
        Assert.Equal(1, store.CountWindows("leased"));
        Assert.Equal(DispatchStep.Blocked, await coordinator.DispatchOneAsync(CancellationToken.None));

        using var runtime = new SearchRuntime(store, new FakeEmbeddings(), new AllowAny(), new SingleLibrary(library), new Version(13, 0, 0), new EchoFacts(store));
        var capabilities = await runtime.CapabilitiesAsync(AllowAny.User, CancellationToken.None);
        Assert.False(capabilities.Compatibility.HostCompatible);
        Assert.Equal("hostIncompatible", capabilities.Availability.Search);
        Assert.Equal("blocked", capabilities.Availability.Indexing);
        Assert.Contains("hostIncompatible", capabilities.Availability.IndexingReasons);
    }

    [Fact]
    public async Task Newer_schema_disables_the_worker_without_throwing()
    {
        var directory = Directory.CreateTempSubdirectory("evs-schema-start-").FullName;
        Guid item;
        using (var created = IndexStore.Open(directory))
        {
            var library = Guid.NewGuid();
            item = Guid.NewGuid();
            created.EnsureActiveSignature(Fixture.Model("proc-a"));
            created.ReplaceLibraries([library]);
            created.UpsertVideo(item, "media-1", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
            Assert.NotNull(created.TryLease());
        }

        using (var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={Path.Combine(directory, "index.db")};Pooling=False"))
        {
            connection.Open();
            using var command = connection.CreateCommand();
            command.CommandText = "PRAGMA user_version = 99";
            command.ExecuteNonQuery();
        }

        using var store = IndexStore.Open(directory);
        Assert.True(store.SchemaReadOnly);
        var coordinator = new IndexCoordinator(store, new ScriptedModel(), new MemoryCatalog(), new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-schema-start-tmp-").FullName);
        coordinator.Start();
        Assert.Equal("schemaNewer", coordinator.DisabledReason);
        Assert.Equal(1, store.CountWindows("leased"));
        coordinator.Enqueue("removed", item);
        coordinator.ProcessEvents();
        Assert.Equal(1, store.CountWindows("leased"));
        Assert.Equal(DispatchStep.Blocked, await coordinator.DispatchOneAsync(CancellationToken.None));

        using var runtime = new SearchRuntime(store, new FakeEmbeddings(), new AllowAny(), new SingleLibrary(Guid.NewGuid()), new Version(12, 2, 0), new EchoFacts(store));
        var capabilities = await runtime.CapabilitiesAsync(AllowAny.User, CancellationToken.None);
        Assert.Equal("schemaNewer", capabilities.Availability.Search);
        Assert.Equal("blocked", capabilities.Availability.Indexing);
    }

    [Fact]
    public async Task Retry_failed_reuses_finished_windows_and_leaves_pause_and_other_rows()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-retry-").FullName);
        var enabled = Guid.NewGuid();
        var disabled = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.ReplaceLibraries([enabled]);
        var oldItem = Guid.NewGuid();
        store.UpsertVideo(oldItem, "media-1", enabled, ApiV1.WindowTicks, "v1", "old.mp4", false);
        var oldLease = store.TryLease();
        store.FailLease(oldLease!.Token, "clip_failed", false);
        var firstGeneration = store.ActiveGeneration;
        store.BeginRebuild(Fixture.Model("proc-b"));
        Assert.NotEqual(firstGeneration, store.ActiveGeneration);

        var main = Guid.NewGuid();
        store.UpsertVideo(main, "media-1", enabled, TimeSpan.FromSeconds(10).Ticks, "v1", "synthetic.mp4", false);
        var failed = store.TryLease();
        store.FailLease(failed!.Token, "clip_failed", false);
        var finished = store.TryLease();
        Assert.Equal(LeaseOutcome.Published, store.CompleteLease(finished!.Token, CoreTests.Axis(0)));
        var keptSegment = finished.SegmentId;

        var other = Guid.NewGuid();
        store.UpsertVideo(other, "media-1", disabled, ApiV1.WindowTicks, "v1", "disabled.mp4", false);
        store.FailLease(store.TryLease()!.Token, "clip_failed", false);
        store.SetManualPause(true);

        var catalog = new MemoryCatalog();
        catalog.Videos.Add(new IndexableVideo { ItemId = main, MediaSourceId = "media-1", LibraryId = enabled, RuntimeTicks = TimeSpan.FromSeconds(10).Ticks, ContentVersion = "v1", SourcePath = "synthetic.mp4" });
        using var runtime = new SearchRuntime(store, new FakeEmbeddings { ProcessingId = "proc-b" }, new AllowAny(), new SingleLibrary(enabled), new Version(12, 2, 0), catalog);
        var viewer = new SearchRuntime(store, new FakeEmbeddings { ProcessingId = "proc-b" }, new ViewerAccess(), new SingleLibrary(enabled), new Version(12, 2, 0), catalog);
        var denied = await Assert.ThrowsAsync<SearchAuthException>(() => viewer.RetryFailedAsync(Guid.NewGuid(), CancellationToken.None));
        Assert.Equal(403, denied.Status);

        var retry = await runtime.RetryFailedAsync(AllowAny.User, CancellationToken.None);
        Assert.True(retry.Accepted);
        Assert.False(retry.ReindexStarted);
        Assert.True(retry.GenerationUnchanged);
        Assert.True(retry.ManualPause);
        Assert.True(store.ManualPause);
        Assert.Equal(1, retry.ResetWindows);
        Assert.Equal(store.ActiveGeneration, retry.ActiveGeneration);
        Assert.Equal(1, store.CountWindows("pending"));
        Assert.Equal(2, store.CountWindows("failed"));
        Assert.Equal("succeeded", WindowState(store, keptSegment));
        Assert.Equal(LeaseOutcome.Stale, store.CompleteLease(failed.Token, CoreTests.Axis(1)));
        Assert.Equal("partial", MediaState(store, main, "media-1"));

        store.SetManualPause(false);
        store.SetEmbeddingBaseUrl("http://127.0.0.1");
        var coordinator = new IndexCoordinator(store, new ScriptedModel { ProcessingId = "proc-b" }, catalog, new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-retry-tmp-").FullName);
        Assert.Equal(DispatchStep.Published, await coordinator.DispatchOneAsync(CancellationToken.None));
        Assert.Equal("succeeded", WindowState(store, keptSegment));
        Assert.Equal(0, store.CountWindows("pending"));
        Assert.Equal("complete", MediaState(store, main, "media-1"));

        var unknown = Guid.NewGuid();
        store.UpsertVideo(unknown, "media-1", enabled, ApiV1.WindowTicks, "v1", "missing.mp4", false);
        store.FailLease(store.TryLease()!.Token, "clip_failed", false);
        catalog.UnreadableSources.Add("media-1");
        var changed = Guid.NewGuid();
        store.UpsertVideo(changed, "media-2", enabled, ApiV1.WindowTicks, "v1", "changed.mp4", false);
        store.FailLease(store.TryLease()!.Token, "clip_failed", false);
        catalog.Videos.Add(new IndexableVideo { ItemId = changed, MediaSourceId = "media-2", LibraryId = enabled, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "replaced", SourcePath = "changed.mp4" });
        store.SetManualPause(true);
        var second = await runtime.RetryFailedAsync(AllowAny.User, CancellationToken.None);
        Assert.True(store.ManualPause);
        Assert.Equal(0, second.ResetWindows);
        Assert.True(second.GenerationUnchanged);
        Assert.Equal("failed", WindowState(store, IndexStore.SegmentId(unknown, "media-1", "v1", 0, ApiV1.WindowTicks, store.ActiveGeneration)));
        Assert.Equal("superseded", WindowState(store, IndexStore.SegmentId(changed, "media-2", "v1", 0, ApiV1.WindowTicks, store.ActiveGeneration)));
        Assert.Equal("complete", MediaState(store, main, "media-1"));

        var wrongHost = new SearchRuntime(store, new FakeEmbeddings(), new AllowAny(), new SingleLibrary(enabled), new Version(13, 0, 0), catalog);
        var refused = await Assert.ThrowsAsync<SearchInputException>(() => wrongHost.RetryFailedAsync(AllowAny.User, CancellationToken.None));
        Assert.Equal("hostIncompatible", refused.Code);
    }

    [Fact]
    public async Task Playback_pause_is_reported_separately_from_manual_pause()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-playback-status-").FullName);
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.ReplaceLibraries([library]);
        store.SaveSucceededWindow(item, "media-1", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), null);
        var catalog = new MemoryCatalog { Playback = true };
        catalog.Videos.Add(new IndexableVideo { ItemId = item, MediaSourceId = "media-1", LibraryId = library, RuntimeTicks = ApiV1.WindowTicks, ContentVersion = "v1", SourcePath = "synthetic.mp4" });
        using var runtime = new SearchRuntime(store, new FakeEmbeddings { ProcessingId = "proc-a" }, new AllowAny(), new SingleLibrary(library), new Version(12, 2, 0), catalog, () => catalog.Playback);
        var capabilities = await runtime.CapabilitiesAsync(AllowAny.User, CancellationToken.None);
        Assert.Equal("available", capabilities.Availability.Search);
        Assert.Equal("paused", capabilities.Availability.Indexing);
        Assert.Equal(["playback"], capabilities.Availability.IndexingReasons);
        var status = await runtime.AdminStatusAsync(AllowAny.User, CancellationToken.None);
        Assert.False(status.Pause.Manual);
        Assert.True(status.Pause.Playback);
        Assert.Equal(["playback"], status.Pause.Reasons);
        Assert.False(store.ManualPause);

        store.SetManualPause(true);
        capabilities = await runtime.CapabilitiesAsync(AllowAny.User, CancellationToken.None);
        status = await runtime.AdminStatusAsync(AllowAny.User, CancellationToken.None);
        Assert.Equal("paused", capabilities.Availability.Indexing);
        Assert.Contains("manual", capabilities.Availability.IndexingReasons);
        Assert.Contains("playback", capabilities.Availability.IndexingReasons);
        Assert.True(status.Pause.Manual);
        Assert.True(status.Pause.Playback);

        catalog.Playback = false;
        capabilities = await runtime.CapabilitiesAsync(AllowAny.User, CancellationToken.None);
        status = await runtime.AdminStatusAsync(AllowAny.User, CancellationToken.None);
        Assert.Equal("paused", capabilities.Availability.Indexing);
        Assert.Equal(["manual"], capabilities.Availability.IndexingReasons);
        Assert.True(status.Pause.Manual);
        Assert.False(status.Pause.Playback);
        Assert.True(store.ManualPause);
        Assert.Equal(["manual"], status.Pause.Reasons);
    }

    [Fact]
    public async Task Retry_failed_refuses_a_newer_index_schema()
    {
        var directory = Directory.CreateTempSubdirectory("evs-retry-schema-").FullName;
        using (var created = IndexStore.Open(directory))
        {
            created.EnsureActiveSignature(Fixture.Model("proc-a"));
        }

        using (var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={Path.Combine(directory, "index.db")};Pooling=False"))
        {
            connection.Open();
            using var command = connection.CreateCommand();
            command.CommandText = "PRAGMA user_version = 99";
            command.ExecuteNonQuery();
        }

        using var store = IndexStore.Open(directory);
        using var runtime = new SearchRuntime(store, new FakeEmbeddings(), new AllowAny(), new SingleLibrary(Guid.NewGuid()), new Version(12, 2, 0), new EchoFacts(store));
        var refused = await Assert.ThrowsAsync<SearchInputException>(() => runtime.RetryFailedAsync(AllowAny.User, CancellationToken.None));
        Assert.Equal("schemaNewer", refused.Code);
        Assert.Equal(0, store.WindowCount());
    }

    [Fact]
    public async Task Imported_candidate_mode_does_not_plan_full_film_windows()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-import-mode-").FullName);
        var library = Guid.NewGuid();
        var sample = Guid.NewGuid();
        var film = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.SetEmbeddingBaseUrl("http://127.0.0.1");
        store.ReplaceLibraries([library]);
        store.SaveSucceededWindow(sample, "sample", library, "v1", 120_000_000, 0, 60_000_000, CoreTests.Axis(0), "sample.mp4");
        InsertPartialFilm(store, film, library);
        store.SetManualPause(true);
        store.SetImportedCandidatesOnly(true);
        var before = store.WindowCount();
        var hash = WindowDigest(store);
        var catalog = new MemoryCatalog();
        catalog.Videos.Add(new IndexableVideo
        {
            ItemId = film,
            MediaSourceId = "film",
            LibraryId = library,
            RuntimeTicks = TimeSpan.FromMinutes(20).Ticks,
            ContentVersion = "film-changed",
            SourcePath = "film.mp4",
            HasAudio = true,
            AudioStreamIndex = 1
        });
        var model = new ScriptedModel();
        var posts = 0;
        model.Started = () => posts++;
        var coordinator = new IndexCoordinator(store, model, catalog, new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-import-mode-tmp-").FullName);
        store.RequestReconcile();
        coordinator.Start();
        coordinator.ReconcileInterval = TimeSpan.Zero;
        coordinator.ProcessEvents();
        coordinator.Enqueue("updated", film);
        coordinator.ProcessEvents();
        Assert.Equal(DispatchStep.Blocked, await coordinator.DispatchOneAsync(CancellationToken.None));
        Assert.Equal(before, store.WindowCount());
        Assert.Equal(hash, WindowDigest(store));
        Assert.Equal(0, posts);
        Assert.Equal("partial", MediaState(store, film, "film"));
        Assert.Equal("succeeded", WindowState(store, IndexStore.SegmentId(sample, "sample", "v1", 0, 60_000_000, store.ActiveGeneration)));
        using var runtime = new SearchRuntime(store, new FakeEmbeddings(), new AllowAny(), new SingleLibrary(library), new Version(12, 2, 0), new EchoFacts(store));
        var status = await runtime.AdminStatusAsync(AllowAny.User, CancellationToken.None);
        Assert.Contains("importedCandidatesOnly", status.Pause.Reasons);
        var capabilities = await runtime.CapabilitiesAsync(AllowAny.User, CancellationToken.None);
        Assert.Equal("paused", capabilities.Availability.Indexing);
        Assert.Contains("importedCandidatesOnly", capabilities.Availability.IndexingReasons);
        var refused = await Assert.ThrowsAsync<SearchInputException>(() => runtime.RetryFailedAsync(AllowAny.User, CancellationToken.None));
        Assert.Equal("importedCandidatesOnly", refused.Code);
        Assert.Equal(before, store.WindowCount());
        Assert.Equal(hash, WindowDigest(store));
    }

    [Fact]
    public async Task Manual_pause_still_lets_reconcile_plan_windows_until_import_mode_is_set()
    {
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-pause-reconcile-").FullName);
        var library = Guid.NewGuid();
        var sample = Guid.NewGuid();
        var film = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-a"));
        store.SetEmbeddingBaseUrl("http://127.0.0.1");
        store.ReplaceLibraries([library]);
        store.SaveSucceededWindow(sample, "sample", library, "v1", 120_000_000, 0, 60_000_000, CoreTests.Axis(0), "sample.mp4");
        store.SetManualPause(true);
        Assert.False(store.ImportedCandidatesOnly);
        var before = store.WindowCount();
        var catalog = new MemoryCatalog();
        catalog.Videos.Add(new IndexableVideo
        {
            ItemId = film,
            MediaSourceId = "film",
            LibraryId = library,
            RuntimeTicks = TimeSpan.FromMinutes(20).Ticks,
            ContentVersion = "film-v",
            SourcePath = "film.mp4",
            HasAudio = true,
            AudioStreamIndex = 1
        });
        catalog.Videos.Add(new IndexableVideo
        {
            ItemId = sample,
            MediaSourceId = "sample",
            LibraryId = library,
            RuntimeTicks = 120_000_000,
            ContentVersion = "v1",
            SourcePath = "sample.mp4"
        });
        var model = new ScriptedModel();
        var posts = 0;
        model.Started = () => posts++;
        var coordinator = new IndexCoordinator(store, model, catalog, new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-pause-reconcile-tmp-").FullName);
        store.RequestReconcile();
        coordinator.Start();
        coordinator.ProcessEvents();
        Assert.True(store.WindowCount() > before);
        Assert.Equal(DispatchStep.Paused, await coordinator.DispatchOneAsync(CancellationToken.None));
        Assert.Equal(0, posts);
    }

    private static void InsertPartialFilm(IndexStore store, Guid film, Guid library)
    {
        using var connection = new Microsoft.Data.Sqlite.SqliteConnection("Data Source=" + store.DatabasePath);
        connection.Open();
        using var command = connection.CreateCommand();
        command.CommandText = """
            INSERT INTO media (
              item_id, media_source_id, generation, library_id, content_version,
              runtime_ticks, state, source_path, has_audio, audio_stream_index)
            VALUES (
              $item, 'film', (SELECT generation FROM signatures WHERE active = 1), $library,
              'film-v', $runtime, 'partial', 'film.mp4', 1, 1)
            """;
        command.Parameters.AddWithValue("$item", film.ToString("D"));
        command.Parameters.AddWithValue("$library", library.ToString("D"));
        command.Parameters.AddWithValue("$runtime", TimeSpan.FromMinutes(20).Ticks);
        command.ExecuteNonQuery();
    }

    private static string WindowDigest(IndexStore store)
    {
        using var connection = new Microsoft.Data.Sqlite.SqliteConnection("Data Source=" + store.DatabasePath + ";Mode=ReadOnly;Pooling=False");
        connection.Open();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT segment_id, item_id, media_source_id, content_version, start_ticks, end_ticks, state FROM windows ORDER BY segment_id";
        using var reader = command.ExecuteReader();
        var builder = new System.Text.StringBuilder();
        while (reader.Read())
        {
            builder.Append(reader.GetString(0)).Append('|').Append(reader.GetString(1)).Append('|')
                .Append(reader.GetString(2)).Append('|').Append(reader.GetString(3)).Append('|')
                .Append(reader.GetInt64(4)).Append('|').Append(reader.GetInt64(5)).Append('|')
                .Append(reader.GetString(6)).Append('\n');
        }

        return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(builder.ToString())));
    }

    private static string WindowState(IndexStore store, Guid segmentId)
    {
        using var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={store.DatabasePath};Mode=ReadOnly;Pooling=False");
        connection.Open();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT state FROM windows WHERE segment_id = $id";
        command.Parameters.AddWithValue("$id", segmentId.ToString("D"));
        return (string)command.ExecuteScalar()!;
    }

    private static string MediaState(IndexStore store, Guid itemId, string mediaSourceId)
    {
        using var connection = new Microsoft.Data.Sqlite.SqliteConnection($"Data Source={store.DatabasePath};Mode=ReadOnly;Pooling=False");
        connection.Open();
        using var command = connection.CreateCommand();
        command.CommandText = """
            SELECT state FROM media
            WHERE item_id = $item AND media_source_id = $source
              AND generation = (SELECT generation FROM signatures WHERE active = 1)
            """;
        command.Parameters.AddWithValue("$item", itemId.ToString("D"));
        command.Parameters.AddWithValue("$source", mediaSourceId);
        return (string)command.ExecuteScalar()!;
    }
}

internal sealed class ViewerAccess : IMediaAccess
{
    public bool TryGetUser(Guid userId, out MediaPrincipal? principal)
    {
        principal = new MediaPrincipal(false, false, _ => true, (_, _) => true);
        return true;
    }
}

internal sealed class MemoryCatalog : IIndexCatalog
{
    public bool Playback { get; set; }
    public string Title { get; set; } = "";
    public List<IndexableVideo> Videos { get; } = [];
    public HashSet<Guid> UnreadableItems { get; } = [];
    public HashSet<string> UnreadableSources { get; } = [];

    public bool IsVideoPlaybackActive() => Playback;

    public CatalogScan ReadLibraries(IReadOnlyCollection<Guid> libraryIds)
        => CatalogScan.Ok(Videos.Where(video => libraryIds.Contains(video.LibraryId)).ToList(), []);

    public CatalogItem ReadItem(Guid itemId, IReadOnlyCollection<Guid> enabledLibraryIds)
    {
        if (UnreadableItems.Contains(itemId))
        {
            return new CatalogItem { Presence = ItemPresence.Unreadable };
        }

        var matches = Videos.Where(video => video.ItemId == itemId && enabledLibraryIds.Contains(video.LibraryId)).ToList();
        if (matches.Count == 0)
        {
            return new CatalogItem { Presence = ItemPresence.Missing };
        }

        return new CatalogItem { Presence = ItemPresence.Readable, Video = matches[0], Videos = matches };
    }

    public CatalogItem ReadSource(Guid itemId, string mediaSourceId, IReadOnlyCollection<Guid> enabledLibraryIds)
    {
        if (UnreadableItems.Contains(itemId) || UnreadableSources.Contains(mediaSourceId))
        {
            return new CatalogItem { Presence = ItemPresence.Unreadable };
        }

        var match = Videos.FirstOrDefault(video => video.ItemId == itemId && video.MediaSourceId == mediaSourceId && enabledLibraryIds.Contains(video.LibraryId));
        if (match is null)
        {
            return new CatalogItem { Presence = ItemPresence.Missing };
        }

        return new CatalogItem { Presence = ItemPresence.Readable, Video = match, Videos = [match] };
    }
}

internal sealed class AcceptingClipper : IWindowClipper
{
    public Task<ClipCheck> ClipAsync(LeaseTicket lease, string outputPath, CancellationToken cancellationToken)
    {
        var directory = Path.GetDirectoryName(outputPath);
        if (!string.IsNullOrEmpty(directory))
        {
            Directory.CreateDirectory(directory);
        }

        File.WriteAllBytes(outputPath, [0, 0, 0, 1]);
        var duration = (lease.EndTicks - lease.StartTicks) / (double)ApiV1.TicksPerSecond;
        return Task.FromResult(new ClipCheck
        {
            Accepted = true,
            RequestedStartSeconds = lease.StartTicks / (double)ApiV1.TicksPerSecond,
            RequestedDurationSeconds = duration,
            ProbedDurationSeconds = duration,
            HasVideo = true,
            HasAudio = lease.HasAudio,
            AudioDurationSeconds = lease.HasAudio ? duration : null,
            SlackSeconds = 0.1,
            StartSeconds = 0
        });
    }
}

internal sealed class ScriptedModel : IEmbeddingClient
{
    public double Duration { get; set; } = 6;
    public string ProcessingId { get; set; } = "proc-a";

    public Task<ModelObservation> ObserveAsync(string? baseUrl, CancellationToken cancellationToken)
        => Task.FromResult(Fixture.Model(ProcessingId));

    public Task<EmbeddingCall> EmbedTextAsync(string? baseUrl, string text, CancellationToken cancellationToken, Func<Task<EmbeddingCall?>>? beforeSend = null)
        => Task.FromResult(new EmbeddingCall { Success = true, Vector = CoreTests.Axis(0), Observation = Fixture.Model(ProcessingId) });

    public Task? Gate { get; set; }
    public Action? Started { get; set; }

    public async Task<EmbeddingCall> EmbedVideoAsync(string? baseUrl, Stream clip, CancellationToken cancellationToken, Func<Task<EmbeddingCall?>>? beforeSend = null)
    {
        Started?.Invoke();
        if (Gate is not null)
        {
            await Gate.ConfigureAwait(false);
        }

        return new EmbeddingCall
        {
            Success = true,
            Vector = CoreTests.Axis(0),
            Observation = Fixture.Model(ProcessingId),
            Processing = new WindowProcessing { VideoDurationSeconds = Duration, AudioIncluded = true, AudioDurationSeconds = Duration }
        };
    }
}

internal sealed class AllowAny : IMediaAccess
{
    public static readonly Guid User = Guid.Parse("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");

    public bool TryGetUser(Guid userId, out MediaPrincipal? principal)
    {
        principal = new MediaPrincipal(true, false, _ => true, (_, _) => true);
        return userId == User;
    }
}

internal sealed class SingleLibrary : ILibraryCatalog
{
    private readonly Guid _library;

    public SingleLibrary(Guid library) => _library = library;

    public IReadOnlyList<LibraryListing> ListLibraries() => [new LibraryListing { Id = _library, Name = "synthetic" }];

    public ParentBinding ResolveParent(Guid parentId) => new(parentId == _library, parentId == _library);

    public bool ItemBelongsToParent(Guid itemId, Guid parentId) => parentId == _library;
}

internal sealed class CountingStream : Stream
{
    public int ReadBytes { get; private set; }

    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
    public override void Flush() { }
    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

    public override int Read(byte[] buffer, int offset, int count)
    {
        ReadBytes += count;
        buffer.AsSpan(offset, count).Fill((byte)'a');
        return count;
    }

    public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        ReadBytes += buffer.Length;
        buffer.Span.Fill((byte)'a');
        return ValueTask.FromResult(buffer.Length);
    }
}
