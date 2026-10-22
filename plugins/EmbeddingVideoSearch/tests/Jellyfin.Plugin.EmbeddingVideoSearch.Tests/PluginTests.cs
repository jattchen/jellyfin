using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Data.Sqlite;
using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class CoreTests
{
    [Fact]
    public void Windows_cover_the_timeline_including_the_tail()
    {
        AssertCovered(TimeSpan.FromSeconds(6).Ticks);
        AssertCovered(TimeSpan.FromSeconds(7).Ticks);
        AssertCovered(TimeSpan.FromSeconds(10).Ticks);
        AssertCovered(TimeSpan.FromSeconds(14).Ticks);
        AssertCovered(1);
        AssertCovered(TimeSpan.FromHours(1).Ticks);
        var ten = WindowPlanner.Plan(TimeSpan.FromSeconds(10).Ticks);
        Assert.Equal(2, ten.Count);
        Assert.Equal(TimeSpan.FromSeconds(10).Ticks, ten[^1].EndTicks);
        Assert.Equal(ApiV1.WindowTicks, ten[^1].EndTicks - ten[^1].StartTicks);
    }

    [Fact]
    public void Content_version_ignores_title_and_changes_with_size()
    {
        var modified = new DateTimeOffset(2026, 10, 14, 0, 0, 0, TimeSpan.Zero);
        var left = ContentVersion.FromMediaFacts(10, 60_000_000, modified, "mp4", 0);
        var again = ContentVersion.FromMediaFacts(10, 60_000_000, modified, "mp4", 0);
        var resized = ContentVersion.FromMediaFacts(11, 60_000_000, modified, "mp4", 0);
        var later = ContentVersion.FromMediaFacts(10, 60_000_000, modified.AddMilliseconds(400), "mp4", 0);
        Assert.Equal(left, again);
        Assert.NotEqual(left, resized);
        Assert.NotEqual(left, later);
    }

    [Fact]
    public void Dot_product_of_unit_axes_is_exact()
    {
        var first = Axis(0);
        var second = Axis(1);
        Assert.Equal(1f, VectorMath.Dot(first, first));
        Assert.Equal(0f, VectorMath.Dot(first, second));
        Assert.True(VectorMath.IsUnit(VectorMath.FromBlob(VectorMath.ToBlob(first))));
    }

    [Theory]
    [InlineData(12, 2, 0, true)]
    [InlineData(12, 2, -1, true)]
    [InlineData(12, 2, 1, false)]
    [InlineData(13, 0, 0, false)]
    public void Host_support_is_only_12_2_0(int major, int minor, int build, bool supported)
    {
        var version = build < 0 ? new Version(major, minor) : new Version(major, minor, build);
        Assert.Equal(supported, HostCompatibility.IsSupported(version));
    }

    private static void AssertCovered(long ticks)
    {
        var windows = WindowPlanner.Plan(ticks);
        Assert.True(WindowPlanner.Covers(ticks, windows));
    }

    internal static float[] Axis(int index)
    {
        var vector = new float[ApiV1.Dimension];
        vector[index] = 1f;
        return vector;
    }
}

public class IndexAndSearchTests
{
    [Fact]
    public async Task Search_returns_one_window_and_hides_unauthorized_items()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        var item = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SaveSucceededWindow(item, "media-1", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), "SENTINEL_PRIVATE_PATH");
        fixture.Allow.Add(item);

        var response = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        var hit = Assert.Single(response.Results);
        Assert.Equal(item, hit.ItemId);
        Assert.Equal("media-1", hit.MediaSourceId);
        Assert.Equal(0, hit.StartTicks);
        Assert.Equal(ApiV1.WindowTicks, hit.EndTicks);
        Assert.Equal(1f, hit.Score);
        Assert.Equal("related", hit.Quality);
        Assert.True(hit.IndexComplete);
        Assert.Equal("available", response.Availability.Search);

        var json = JsonProtocol.Serialize(response);
        Assert.Contains("\"itemId\"", json);
        Assert.Contains("\"mediaSourceId\"", json);
        Assert.Contains("\"segmentId\"", json);
        Assert.Contains("\"startTicks\"", json);
        Assert.Contains("\"endTicks\"", json);
        Assert.Contains("\"score\"", json);
        Assert.Contains("\"quality\"", json);
        Assert.Contains("\"indexComplete\"", json);
        Assert.DoesNotContain("SENTINEL_PRIVATE_PATH", json);
        Assert.DoesNotContain("ItemId", json);

        fixture.Allow.Clear();
        var hidden = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        Assert.Empty(hidden.Results);
        Assert.Equal(1, fixture.Store.WindowCount());
    }

    [Fact]
    public async Task Parent_library_keeps_only_that_library_and_segment_route_uses_the_same_id()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        var other = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library, other]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SaveSucceededWindow(fixture.AllowItem, "media-1", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), null);
        var otherItem = Guid.NewGuid();
        fixture.Allow.Add(otherItem);
        fixture.Store.SaveSucceededWindow(otherItem, "media-1", other, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), null);

        var narrowed = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic", ParentId = library }, CancellationToken.None);
        var hit = Assert.Single(narrowed.Results);
        Assert.Equal(fixture.AllowItem, hit.ItemId);

        var fromRoute = FlexibleGuid.TryRead(hit.SegmentId.ToString("N"), out var parsed);
        Assert.True(fromRoute);
        var detail = fixture.Runtime.GetSegment(fixture.UserId, parsed);
        Assert.Equal(200, detail.StatusCode);
        Assert.Equal(hit.SegmentId, detail.Body!.SegmentId);
        Assert.Equal("jointAudioVideo", detail.Body.Representation);
        Assert.Equal(hit.StartTicks, detail.Body.StartTicks);
    }

    [Fact]
    public async Task Weak_hit_is_labeled_and_follows_related()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        fixture.Store.SaveSucceededWindow(fixture.AllowItem, "media-1", library, "v1", 10_000_000, 0, 10_000_000, CoreTests.Axis(0), null);
        var other = Guid.NewGuid();
        fixture.Allow.Add(other);
        fixture.Store.SaveSucceededWindow(other, "media-1", library, "v1", 10_000_000, 4_000_000, 10_000_000, CoreTests.Axis(1), null);

        var response = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic", Limit = 5 }, CancellationToken.None);
        Assert.Equal(["related", "weak"], response.Results.Select(hit => hit.Quality).ToArray());
        Assert.Equal(0f, response.Results[1].Score);
    }

    [Fact]
    public async Task Incompatible_model_keeps_rows_and_resume_does_not_clear_it()
    {
        using var fixture = Fixture.Create();
        var library = Guid.NewGuid();
        fixture.Store.ReplaceLibraries([library]);
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-old"));
        fixture.Store.SaveSucceededWindow(fixture.AllowItem, "media-1", library, "v1", ApiV1.WindowTicks, 0, ApiV1.WindowTicks, CoreTests.Axis(0), "SENTINEL_PRIVATE_PATH");
        fixture.Embeddings.ProcessingId = "proc-new";

        var blocked = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        Assert.Equal("incompatible", blocked.Availability.Search);
        Assert.Empty(blocked.Results);
        Assert.Equal(1, fixture.Store.WindowCount());
        Assert.Equal("SENTINEL_PRIVATE_PATH", StoredPath(fixture.Store.DatabasePath));

        fixture.Runtime.Resume(fixture.UserId);
        Assert.True(fixture.Store.Incompatible);
        Assert.False(fixture.Store.ManualPause);

        fixture.Runtime.Pause(fixture.UserId);
        fixture.Runtime.Resume(fixture.UserId);
        Assert.True(fixture.Store.Incompatible);

        var before = fixture.Store.WindowCount();
        var generation = await fixture.Runtime.RebuildAsync(fixture.UserId, CancellationToken.None);
        Assert.True(generation.ReindexStarted);
        Assert.True(generation.OldGenerationRetained);
        Assert.Equal(before, fixture.Store.WindowCount());
        Assert.Equal(2, fixture.Store.GenerationCount());
        Assert.False(fixture.Store.Incompatible);

        fixture.Embeddings.ProcessingId = "proc-new";
        var after = await fixture.Runtime.SearchAsync(fixture.UserId, new SearchRequest { Query = "synthetic" }, CancellationToken.None);
        Assert.Empty(after.Results);
        Assert.Equal(before, fixture.Store.WindowCount());
    }

    [Fact]
    public void Invalid_vector_does_not_leave_a_row()
    {
        using var fixture = Fixture.Create();
        fixture.Store.EnsureActiveSignature(Fixture.Model("proc-a"));
        var bad = new float[ApiV1.Dimension];
        Assert.Throws<InvalidDataException>(() => fixture.Store.SaveSucceededWindow(Guid.NewGuid(), "media-1", Guid.NewGuid(), "v1", 1, 0, 1, bad, null));
        Assert.Equal(0, fixture.Store.WindowCount());
    }

    [Fact]
    public void Newer_schema_refuses_writes_and_backup_can_be_opened()
    {
        var directory = Directory.CreateTempSubdirectory("evs-schema-").FullName;
        string backup;
        using (var store = IndexStore.Open(directory))
        {
            store.EnsureActiveSignature(Fixture.Model("proc-a"));
            store.SaveSucceededWindow(Guid.NewGuid(), "media-1", Guid.NewGuid(), "v1", 1, 0, 1, CoreTests.Axis(0), null);
            backup = store.CreateConsistentBackup();
        }

        var restoredDir = Directory.CreateTempSubdirectory("evs-restore-").FullName;
        File.Copy(backup, Path.Combine(restoredDir, "index.db"));
        using (var restored = IndexStore.Open(restoredDir))
        {
            Assert.Equal(1, restored.WindowCount());
        }

        var path = Path.Combine(directory, "index.db");
        using (var connection = new SqliteConnection($"Data Source={path};Pooling=False"))
        {
            connection.Open();
            using var command = connection.CreateCommand();
            command.CommandText = "PRAGMA user_version = 99";
            command.ExecuteNonQuery();
        }

        using var reopened = IndexStore.Open(directory);
        Assert.True(reopened.SchemaReadOnly);
        Assert.Equal(1, reopened.WindowCount());
        Assert.Throws<InvalidOperationException>(() => reopened.SetManualPause(true));
    }

    [Fact]
    public void Second_open_does_not_become_the_writer()
    {
        var directory = Directory.CreateTempSubdirectory("evs-lock-").FullName;
        using var first = IndexStore.Open(directory);
        using var second = IndexStore.Open(directory);
        Assert.True(first.HasWriterLock);
        Assert.False(second.HasWriterLock);
        Assert.Throws<InvalidOperationException>(() => second.SetManualPause(true));
    }

    private static string StoredPath(string databasePath)
    {
        using var connection = new SqliteConnection($"Data Source={databasePath};Mode=ReadOnly;Pooling=False");
        connection.Open();
        using var command = connection.CreateCommand();
        command.CommandText = "SELECT source_path FROM media LIMIT 1";
        return (string)command.ExecuteScalar()!;
    }
}

public class EmbeddingClientTests
{
    [Fact]
    public async Task Client_reads_model_text_and_video_contracts()
    {
        await using var server = StubServer.Start();
        var client = new HttpEmbeddingClient();
        var model = await client.ObserveAsync(server.BaseUrl, CancellationToken.None);
        Assert.True(model.IsExpectedModel);
        Assert.Equal("proc-test", model.ProcessingId);

        var text = await client.EmbedTextAsync(server.BaseUrl, "synthetic query", CancellationToken.None);
        Assert.True(text.Success);
        Assert.Equal(1f, text.Vector![0]);
        Assert.DoesNotContain(server.Requests, request => request.Contains("Origin", StringComparison.OrdinalIgnoreCase));

        await using var clip = new MemoryStream([1, 2, 3, 4]);
        var video = await client.EmbedVideoAsync(server.BaseUrl, clip, CancellationToken.None);
        Assert.True(video.Success);
        Assert.Contains(server.Bodies, body => body.Contains("video_audio", StringComparison.Ordinal) && body.Contains("clip", StringComparison.Ordinal));

        server.Dimension = 4;
        var rejected = await client.EmbedTextAsync(server.BaseUrl, "synthetic query", CancellationToken.None);
        Assert.False(rejected.Success);
        Assert.Equal("unexpectedModel", rejected.ErrorCode);
    }

    [Fact]
    public async Task Unconfigured_client_does_not_need_a_server()
    {
        var client = new HttpEmbeddingClient();
        var model = await client.ObserveAsync(null, CancellationToken.None);
        Assert.False(model.Reachable);
        Assert.Equal("notConfigured", model.ErrorCode);
    }

    [Fact]
    public void Video_timeout_is_longer_than_the_measured_window()
    {
        Assert.True(HttpEmbeddingClient.VideoTimeout > TimeSpan.FromSeconds(21));
        Assert.True(HttpEmbeddingClient.MetadataTimeout <= TimeSpan.FromSeconds(5));
        Assert.True(HttpEmbeddingClient.MetadataTimeout < HttpEmbeddingClient.TextTimeout);
        Assert.True(HttpEmbeddingClient.TextTimeout < HttpEmbeddingClient.VideoTimeout);
    }

    [Fact]
    public async Task Timeouts_are_separate_and_failures_are_not_retried()
    {
        // The stub withholds the answer until this test releases it. A call that returns while the
        // answer is still held used its own budget. The guard is only how long we wait to learn that;
        // the other budgets are longer, so they cannot be the reason the call returned.
        var guard = TimeSpan.FromSeconds(30);
        var otherBudget = TimeSpan.FromMinutes(2);
        await using var server = StubServer.Start();
        var client = new HttpEmbeddingClient(
            metadataTimeout: TimeSpan.FromMilliseconds(300),
            textTimeout: otherBudget,
            videoTimeout: otherBudget);

        server.HoldResponses = true;
        var metadataTask = client.ObserveAsync(server.BaseUrl, CancellationToken.None);
        await server.WaitForHits(1).WaitAsync(guard, TestContext.Current.CancellationToken);
        var metadata = await metadataTask.WaitAsync(guard, TestContext.Current.CancellationToken);
        Assert.Equal("timeout", metadata.ErrorCode);
        Assert.Equal(1, server.HitCount);
        Assert.Equal(new[] { "/v1/models" }, server.SnapshotPaths());
        Assert.Equal(0, client.Dispatcher.PostsStarted);
        await server.ReleaseResponses().WaitAsync(guard, TestContext.Current.CancellationToken);

        var video = await client.EmbedVideoAsync(server.BaseUrl, new MemoryStream([1, 2, 3]), CancellationToken.None).WaitAsync(guard, TestContext.Current.CancellationToken);
        Assert.True(video.Success);
        Assert.Equal(2, server.HitCount);
        Assert.Equal(1, client.Dispatcher.PostsStarted);
        Assert.Equal(new[] { "/v1/models", "/v1/embeddings" }, server.SnapshotPaths());

        server.ForcedStatus = 429;
        var busy = await client.EmbedVideoAsync(server.BaseUrl, new MemoryStream([1]), CancellationToken.None).WaitAsync(guard, TestContext.Current.CancellationToken);
        Assert.False(busy.Success);
        Assert.Equal("queueFull", busy.ErrorCode);
        Assert.True(busy.Retryable);
        Assert.False(busy.CompletionUnknown);
        Assert.Equal(3, server.HitCount);
        Assert.Equal(2, client.Dispatcher.PostsStarted);

        server.ForcedStatus = 200;
        using (var alreadyCancelled = new CancellationTokenSource())
        {
            alreadyCancelled.Cancel();
            var ignored = await client.EmbedTextAsync(server.BaseUrl, "not-sent", alreadyCancelled.Token).WaitAsync(guard, TestContext.Current.CancellationToken);
            Assert.Equal("cancelled", ignored.ErrorCode);
            Assert.False(ignored.Retryable);
            Assert.Equal(3, server.HitCount);
            Assert.Equal(2, client.Dispatcher.PostsStarted);
        }

        server.HoldResponses = true;
        using var cancel = new CancellationTokenSource();
        var pending = client.EmbedTextAsync(server.BaseUrl, "synthetic", cancel.Token);
        await server.WaitForHits(4).WaitAsync(guard, TestContext.Current.CancellationToken);
        Assert.Equal(3, client.Dispatcher.PostsStarted);
        cancel.Cancel();
        var cancelled = await pending.WaitAsync(guard, TestContext.Current.CancellationToken);
        Assert.Equal("cancelled", cancelled.ErrorCode);
        Assert.False(cancelled.Retryable);
        Assert.False(cancelled.CompletionUnknown);
        Assert.Equal(4, server.HitCount);
        Assert.Equal(3, client.Dispatcher.PostsStarted);
        await server.ReleaseResponses().WaitAsync(guard, TestContext.Current.CancellationToken);
        Assert.Equal(4, server.HitCount);
        Assert.Equal(3, client.Dispatcher.PostsStarted);

        await using (var videoServer = StubServer.Start())
        {
            videoServer.HoldResponses = true;
            var videoClient = new HttpEmbeddingClient(
                metadataTimeout: otherBudget,
                textTimeout: otherBudget,
                videoTimeout: TimeSpan.FromMilliseconds(300));
            var slowVideo = videoClient.EmbedVideoAsync(videoServer.BaseUrl, new MemoryStream([9]), CancellationToken.None);
            await videoServer.WaitForHits(1).WaitAsync(guard, TestContext.Current.CancellationToken);
            var videoTimeout = await slowVideo.WaitAsync(guard, TestContext.Current.CancellationToken);
            Assert.Equal("timeout", videoTimeout.ErrorCode);
            Assert.False(videoTimeout.Retryable);
            Assert.True(videoTimeout.CompletionUnknown);
            Assert.Equal(1, videoClient.Dispatcher.PostsStarted);
            Assert.Equal(new[] { "/v1/embeddings" }, videoServer.SnapshotPaths());
            await videoServer.ReleaseResponses().WaitAsync(guard, TestContext.Current.CancellationToken);
            Assert.Equal(1, videoClient.Dispatcher.PostsStarted);
            videoClient.Dispatcher.Dispose();
        }

        await using (var textServer = StubServer.Start())
        {
            textServer.HoldResponses = true;
            var textClient = new HttpEmbeddingClient(
                metadataTimeout: otherBudget,
                textTimeout: TimeSpan.FromMilliseconds(300),
                videoTimeout: otherBudget);
            var slowText = textClient.EmbedTextAsync(textServer.BaseUrl, "synthetic", CancellationToken.None);
            await textServer.WaitForHits(1).WaitAsync(guard, TestContext.Current.CancellationToken);
            var textTimeout = await slowText.WaitAsync(guard, TestContext.Current.CancellationToken);
            Assert.Equal("timeout", textTimeout.ErrorCode);
            Assert.False(textTimeout.Retryable);
            Assert.False(textTimeout.CompletionUnknown);
            Assert.Equal(1, textClient.Dispatcher.PostsStarted);
            Assert.Equal(new[] { "/v1/embeddings" }, textServer.SnapshotPaths());
            await textServer.ReleaseResponses().WaitAsync(guard, TestContext.Current.CancellationToken);
            var followed = await textClient.EmbedVideoAsync(textServer.BaseUrl, new MemoryStream([4]), CancellationToken.None).WaitAsync(guard, TestContext.Current.CancellationToken);
            Assert.True(followed.Success);
            Assert.Equal(2, textClient.Dispatcher.PostsStarted);
            Assert.Equal(2, textServer.SnapshotPaths().Count(path => path == "/v1/embeddings"));
            textClient.Dispatcher.Dispose();
        }

        client.Dispatcher.Dispose();
    }
}

public class ContractShapeTests
{
    [Fact]
    public void Controllers_use_host_auth_and_the_versioned_route()
    {
        AssertController(typeof(EmbeddingVideoSearchController), ApiV1.Route, elevated: false);
        AssertController(typeof(EmbeddingVideoSearchAdminController), ApiV1.AdminRoute, elevated: true);
        Assert.Contains(typeof(EmbeddingVideoSearchAdminController).GetMethods(), method => method.GetCustomAttribute<HttpPostAttribute>()?.Template == "retryFailed");
        Assert.DoesNotContain(typeof(EmbeddingVideoSearchController).GetMethods(), method => method.GetCustomAttribute<HttpPostAttribute>()?.Template == "retryFailed");
    }

    [Fact]
    public void Capabilities_json_is_camel_case()
    {
        var json = JsonProtocol.Serialize(new CapabilitiesResponse());
        using var document = JsonDocument.Parse(json);
        Assert.Equal("1", document.RootElement.GetProperty("apiVersion").GetString());
        Assert.Equal(10_000_000, document.RootElement.GetProperty("ticksPerSecond").GetInt64());
        Assert.True(document.RootElement.GetProperty("availability").TryGetProperty("search", out _));
        Assert.True(document.RootElement.GetProperty("compatibility").TryGetProperty("hostCompatible", out _));
        Assert.True(document.RootElement.GetProperty("coverage").TryGetProperty("windowsSearchable", out _));
        Assert.Equal("cosine", document.RootElement.GetProperty("qualityPolicy").GetProperty("scoreMeaning").GetString());
        Assert.Equal(ApiV1.GroupedVideoFeature, Assert.Single(document.RootElement.GetProperty("features").EnumerateArray()).GetString());
        Assert.DoesNotContain("ApiVersion", json);
    }

    [Fact]
    public void The_same_guid_is_accepted_with_or_without_hyphens()
    {
        var id = Guid.Parse("8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17");
        var withoutHyphens = id.ToString("N");
        var withHyphens = id.ToString("D");

        var librariesWithout = JsonProtocol.Deserialize<LibrarySelectionRequest>($"{{\"libraryIds\":[\"{withoutHyphens}\"]}}");
        var librariesWith = JsonProtocol.Deserialize<LibrarySelectionRequest>($"{{\"libraryIds\":[\"{withHyphens}\"]}}");
        Assert.Equal(librariesWith!.LibraryIds, librariesWithout!.LibraryIds);
        Assert.Equal(id, librariesWithout.LibraryIds![0]);

        var upper = JsonProtocol.Deserialize<LibrarySelectionRequest>($"{{\"libraryIds\":[\"{withoutHyphens.ToUpperInvariant()}\"]}}");
        Assert.Equal(id, upper!.LibraryIds![0]);

        var searchWithout = JsonProtocol.Deserialize<SearchRequest>($"{{\"query\":\"q\",\"parentId\":\"{withoutHyphens}\",\"libraryIds\":[\"{withoutHyphens}\"]}}");
        var searchWith = JsonProtocol.Deserialize<SearchRequest>($"{{\"query\":\"q\",\"parentId\":\"{withHyphens}\",\"libraryIds\":[\"{withHyphens}\"]}}");
        Assert.Equal(searchWith!.ParentId, searchWithout!.ParentId);
        Assert.Equal(searchWith.LibraryIds, searchWithout.LibraryIds);
        Assert.Equal(id, searchWithout.ParentId);

        var omitted = JsonProtocol.Deserialize<SearchRequest>("{\"query\":\"q\",\"parentId\":null}");
        Assert.Null(omitted!.ParentId);

        Assert.Throws<JsonException>(() => JsonProtocol.Deserialize<LibrarySelectionRequest>("{\"libraryIds\":[\"{8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17}\"]}"));

        Assert.True(FlexibleGuid.TryRead(withoutHyphens, out var routeWithout));
        Assert.True(FlexibleGuid.TryRead(withHyphens, out var routeWith));
        Assert.Equal(routeWithout, routeWith);
        Assert.Equal(id, routeWithout);
        Assert.False(FlexibleGuid.TryRead("{" + withHyphens + "}", out _));

        var written = JsonProtocol.Serialize(new LibraryCoverageBody { LibraryId = id, Name = "synthetic" });
        Assert.Contains($"\"libraryId\":\"{withHyphens}\"", written, StringComparison.Ordinal);
        Assert.DoesNotContain(withoutHyphens, written, StringComparison.Ordinal);
    }

    private static void AssertController(Type type, string route, bool elevated)
    {
        Assert.True(typeof(ControllerBase).IsAssignableFrom(type));
        Assert.DoesNotContain("Jellyfin.Api", type.BaseType!.FullName, StringComparison.Ordinal);
        Assert.Equal(route, type.GetCustomAttribute<RouteAttribute>()!.Template);
        Assert.Contains(type.GetCustomAttributes<AuthorizeAttribute>(), attribute => attribute.Policy is null);
        if (elevated)
        {
            Assert.Contains(type.GetCustomAttributes<AuthorizeAttribute>(), attribute => attribute.Policy == "RequiresElevation");
        }
    }
}

internal sealed class Fixture : IDisposable
{
    public IndexStore Store { get; }
    public SearchRuntime Runtime { get; private set; } = null!;
    public FakeEmbeddings Embeddings { get; }
    public HashSet<Guid> Allow { get; } = [];
    public HashSet<Guid> Containers { get; } = [];
    public HashSet<(Guid ItemId, Guid ParentId)> Parents { get; } = [];
    public Action<Guid, string>? OnReadFacts { get; set; }
    public bool Disabled { get; set; }
    public TaskCompletionSource<bool>? EmbedRelease { get; set; }
    public TaskCompletionSource<bool>? EmbedEntered { get; set; }
    public Guid UserId { get; } = Guid.NewGuid();
    public Guid AllowItem { get; } = Guid.NewGuid();
    private readonly string _directory;

    private Fixture(string directory, IndexStore store, SearchRuntime runtime, FakeEmbeddings embeddings)
    {
        _directory = directory;
        Store = store;
        Runtime = runtime;
        Embeddings = embeddings;
        Allow.Add(AllowItem);
    }

    public static Fixture Create()
    {
        var directory = Directory.CreateTempSubdirectory("evs-test-").FullName;
        var store = IndexStore.Open(directory);
        var embeddings = new FakeEmbeddings();
        var access = new FakeAccess();
        var fixture = new Fixture(directory, store, null!, embeddings);
        access.Owner = fixture;
        var libraries = new FakeLibraries();
        libraries.Owner = fixture;
        var facts = new EchoFacts(store) { Owner = fixture };
        fixture.Runtime = new SearchRuntime(store, embeddings, access, libraries, new Version(12, 2, 0), facts);
        return fixture;
    }

    public static ModelObservation Model(string processingId) => new()
    {
        Reachable = true,
        Ready = true,
        Model = ApiV1.ModelId,
        Revision = ApiV1.Revision,
        ProcessingId = processingId,
        Dimension = ApiV1.Dimension,
        Normalized = true
    };

    public void Dispose()
    {
        Runtime.Dispose();
        Directory.Delete(_directory, recursive: true);
    }
}

internal sealed class FakeAccess : IMediaAccess
{
    public Fixture Owner { get; set; } = null!;

    public bool TryGetUser(Guid userId, out MediaPrincipal? principal)
    {
        principal = null;
        if (userId != Owner.UserId)
        {
            return false;
        }

        principal = new MediaPrincipal(true, Owner.Disabled, _ => true, (itemId, _) => Owner.Allow.Contains(itemId));
        return true;
    }
}

internal sealed class FakeLibraries : ILibraryCatalog
{
    public Fixture Owner { get; set; } = null!;

    public IReadOnlyList<LibraryListing> ListLibraries()
        => Owner.Store.EnabledLibraryIds().Select(id => new LibraryListing { Id = id, Name = "synthetic" }).ToList();

    public ParentBinding ResolveParent(Guid parentId)
    {
        if (Owner.Store.EnabledLibraryIds().Contains(parentId))
        {
            return new ParentBinding(true, true);
        }

        return Owner.Containers.Contains(parentId)
            ? new ParentBinding(true, false)
            : new ParentBinding(false, false);
    }

    public bool ItemBelongsToParent(Guid itemId, Guid parentId)
        => Owner.Parents.Contains((itemId, parentId));
}

internal sealed class EchoFacts : IMediaFacts
{
    private readonly IndexStore _store;

    public EchoFacts(IndexStore store) => _store = store;

    public Fixture? Owner { get; set; }

    public CatalogItem ReadSource(Guid itemId, string mediaSourceId, IReadOnlyCollection<Guid> enabledLibraryIds)
    {
        Owner?.OnReadFacts?.Invoke(itemId, mediaSourceId);
        var version = _store.ReadStoredContentVersion(itemId, mediaSourceId);
        if (version is null)
        {
            return new CatalogItem { Presence = ItemPresence.Unreadable };
        }

        return new CatalogItem
        {
            Presence = ItemPresence.Readable,
            Video = new IndexableVideo { ItemId = itemId, MediaSourceId = mediaSourceId, ContentVersion = version }
        };
    }
}

internal sealed class FakeEmbeddings : IEmbeddingClient
{
    public string ProcessingId { get; set; } = "proc-a";

    public Task<ModelObservation> ObserveAsync(string? baseUrl, CancellationToken cancellationToken)
        => Task.FromResult(Fixture.Model(ProcessingId));

    public async Task<EmbeddingCall> EmbedTextAsync(string? baseUrl, string text, CancellationToken cancellationToken, Func<Task<EmbeddingCall?>>? beforeSend = null)
    {
        var processingId = ProcessingId;
        OwnerEntered();
        if (OwnerRelease() is Task release)
        {
            await release.ConfigureAwait(false);
        }

        return new EmbeddingCall { Success = true, Vector = CoreTests.Axis(0), Observation = Fixture.Model(processingId) };
    }

    public Func<Task?>? Release { get; set; }
    public Action? Entered { get; set; }

    private void OwnerEntered() => Entered?.Invoke();

    private Task? OwnerRelease() => Release?.Invoke();

    public Task<EmbeddingCall> EmbedVideoAsync(string? baseUrl, Stream clip, CancellationToken cancellationToken, Func<Task<EmbeddingCall?>>? beforeSend = null)
        => Task.FromResult(new EmbeddingCall { Success = true, Vector = CoreTests.Axis(0), Observation = Fixture.Model(ProcessingId) });
}

internal sealed class StubServer : IAsyncDisposable
{
    private readonly HttpListener _listener;
    private readonly CancellationTokenSource _stop = new();
    private readonly Task _loop;
    private readonly List<string> _paths = [];
    private readonly List<(int Count, TaskCompletionSource<bool> Done)> _hitWaiters = [];
    private bool _holdResponses;
    private TaskCompletionSource<bool>? _heldRelease;
    private TaskCompletionSource<bool>? _released;
    public string BaseUrl { get; }
    public List<string> Requests { get; } = [];
    public List<string> Bodies { get; } = [];
    public int ForcedStatus { get; set; } = 200;
    public TimeSpan ResponseDelay { get; set; }
    public int HitCount { get; private set; }
    public bool ReportProcessing { get; set; }
    public double ReportedDuration { get; set; } = 6;
    public int Dimension { get; set; } = ApiV1.Dimension;

    public bool HoldResponses
    {
        get
        {
            lock (Requests)
            {
                return _holdResponses;
            }
        }

        set
        {
            lock (Requests)
            {
                _holdResponses = value;
            }
        }
    }

    public static StubServer Start()
    {
        var port = FreePort();
        var listener = new HttpListener();
        listener.Prefixes.Add($"http://127.0.0.1:{port}/");
        listener.Start();
        return new StubServer(listener, $"http://127.0.0.1:{port}");
    }

    private StubServer(HttpListener listener, string baseUrl)
    {
        _listener = listener;
        BaseUrl = baseUrl;
        _loop = Task.Run(Loop);
    }

    public Task WaitForHits(int count)
    {
        lock (Requests)
        {
            if (HitCount >= count)
            {
                return Task.CompletedTask;
            }

            var done = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            _hitWaiters.Add((count, done));
            return done.Task;
        }
    }

    public string[] SnapshotPaths()
    {
        lock (Requests)
        {
            return _paths.ToArray();
        }
    }

    public Task ReleaseResponses()
    {
        TaskCompletionSource<bool>? parked;
        Task<bool> finished;
        lock (Requests)
        {
            parked = _heldRelease;
            _heldRelease = null;
            _holdResponses = false;
            if (parked is null)
            {
                return Task.CompletedTask;
            }

            var signal = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            _released = signal;
            finished = signal.Task;
        }

        parked.TrySetResult(true);
        return finished;
    }

    public async ValueTask DisposeAsync()
    {
        _stop.Cancel();
        _listener.Stop();
        try
        {
            await _loop.ConfigureAwait(false);
        }
        catch (Exception)
        {
        }
    }

    private async Task Loop()
    {
        while (!_stop.IsCancellationRequested)
        {
            HttpListenerContext context;
            try
            {
                context = await _listener.GetContextAsync().ConfigureAwait(false);
            }
            catch (Exception) when (_stop.IsCancellationRequested)
            {
                break;
            }

            try
            {
                using var reader = new StreamReader(context.Request.InputStream, context.Request.ContentEncoding);
                var body = await reader.ReadToEndAsync().ConfigureAwait(false);
                var path = context.Request.Url?.AbsolutePath ?? "";
                TaskCompletionSource<bool>? parked = null;
                lock (Requests)
                {
                    Requests.Add(context.Request.Headers.ToString() ?? "");
                    Bodies.Add(body);
                    _paths.Add(path);
                    HitCount++;
                    if (_holdResponses)
                    {
                        _heldRelease ??= new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                        parked = _heldRelease;
                    }

                    for (var index = _hitWaiters.Count - 1; index >= 0; index--)
                    {
                        if (HitCount < _hitWaiters[index].Count)
                        {
                            continue;
                        }

                        _hitWaiters[index].Done.TrySetResult(true);
                        _hitWaiters.RemoveAt(index);
                    }
                }

                if (parked is not null)
                {
                    await parked.Task.WaitAsync(_stop.Token).ConfigureAwait(false);
                }
                else if (ResponseDelay > TimeSpan.Zero)
                {
                    await Task.Delay(ResponseDelay).ConfigureAwait(false);
                }

                if (ForcedStatus != 200)
                {
                    var error = Encoding.UTF8.GetBytes("{\"error\":{\"code\":\"queue_full\",\"message\":\"busy\"}}");
                    context.Response.StatusCode = ForcedStatus;
                    context.Response.ContentType = "application/json";
                    context.Response.ContentLength64 = error.Length;
                    await context.Response.OutputStream.WriteAsync(error).ConfigureAwait(false);
                    context.Response.Close();
                    continue;
                }

                var vector = new float[Dimension == ApiV1.Dimension ? ApiV1.Dimension : Dimension];
                if (vector.Length > 0)
                {
                    vector[0] = 1f;
                }

                var payload = context.Request.Url?.AbsolutePath == "/v1/models"
                    ? JsonSerializer.Serialize(new
                    {
                        data = new[]
                        {
                            new
                            {
                                id = ApiV1.ModelId,
                                revision = ApiV1.Revision,
                                processing_id = "proc-test",
                                dimension = Dimension,
                                normalized = true,
                                ready = true
                            }
                        }
                    })
                    : JsonSerializer.Serialize(new
                    {
                        model = ApiV1.ModelId,
                        revision = ApiV1.Revision,
                        processing_id = "proc-test",
                        dimension = Dimension,
                        normalized = true,
                        data = new[]
                        {
                            new
                            {
                                index = 0,
                                embedding = vector,
                                processing = ReportProcessing ? new
                                {
                                    video = new { duration_seconds = ReportedDuration, audio_included = true, sampled_seconds = new[] { 0, 1, 2, 3, 4, 5 } },
                                    audio = new { duration_seconds = ReportedDuration }
                                } : null
                            }
                        }
                    });
                var bytes = Encoding.UTF8.GetBytes(payload);
                context.Response.ContentType = "application/json";
                context.Response.ContentLength64 = bytes.Length;
                await context.Response.OutputStream.WriteAsync(bytes).ConfigureAwait(false);
                context.Response.Close();
            }
            catch (Exception)
            {
                try
                {
                    context.Response.Abort();
                }
                catch (Exception)
                {
                }
            }
            finally
            {
                SignalReleased();
            }
        }
    }

    private void SignalReleased()
    {
        TaskCompletionSource<bool>? finished;
        lock (Requests)
        {
            finished = _released;
            _released = null;
        }

        finished?.TrySetResult(true);
    }

    private static int FreePort()
    {
        using var tcp = new TcpListener(IPAddress.Loopback, 0);
        tcp.Start();
        return ((IPEndPoint)tcp.LocalEndpoint).Port;
    }
}
