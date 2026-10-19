using System.Net;
using Xunit;
using System.Net.Sockets;
using System.Text;
using System.Threading.Channels;

#pragma warning disable xUnit1051 // Caller cancellation in these tests is intentionally not the test token.

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class DispatchTests
{
    [Fact]
    public async Task Dispatched_post_ignores_caller_cancel_and_a_queued_cancel_is_never_sent()
    {
        await using var server = GateServer.Start();
        var dispatcher = new ModelDispatcher();
        var client = new HttpEmbeddingClient(dispatcher: dispatcher);
        using var cancelA = new CancellationTokenSource();
        var callA = client.EmbedTextAsync(server.BaseUrl, "query-A", cancelA.Token);
        await server.Received(1).WaitAsync(TimeSpan.FromSeconds(5));
        cancelA.Cancel();
        var resultA = await callA.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("cancelled", resultA.ErrorCode);
        Assert.False(resultA.CompletionUnknown);
        Assert.Equal(1, server.EmbeddingPosts);

        using var cancelB = new CancellationTokenSource();
        var callB = client.EmbedTextAsync(server.BaseUrl, "query-B", cancelB.Token);
        cancelB.Cancel();
        var resultB = await callB.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("cancelled", resultB.ErrorCode);
        Assert.Equal(1, server.EmbeddingPosts);
        Assert.DoesNotContain("query-B", string.Join('\n', server.Bodies));

        var callC = client.EmbedTextAsync(server.BaseUrl, "query-C", CancellationToken.None);
        Assert.Equal(1, server.EmbeddingPosts);
        server.Release();
        await server.Received(2).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Contains("query-C", server.Bodies[1]);
        Assert.DoesNotContain("query-B", server.Bodies[1]);
        server.Release();
        var resultC = await callC.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.True(resultC.Success);
        Assert.Equal(2, dispatcher.PostsStarted);
    }

    [Fact]
    public async Task Latest_query_is_sent_after_the_active_video_and_before_the_next_window()
    {
        await using var server = GateServer.Start();
        var client = new HttpEmbeddingClient(dispatcher: new ModelDispatcher());
        var video = new TrackStream([1, 2, 3, 4]);
        var window = new TrackStream([5, 6, 7, 8]);
        var videoCall = client.EmbedVideoAsync(server.BaseUrl, video, CancellationToken.None);
        await server.Received(1).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(0, video.Disposes);

        var first = client.EmbedTextAsync(server.BaseUrl, "query-one", CancellationToken.None);
        using var cancelSecond = new CancellationTokenSource();
        var second = client.EmbedTextAsync(server.BaseUrl, "query-two", cancelSecond.Token);
        var third = client.EmbedTextAsync(server.BaseUrl, "query-three", CancellationToken.None);
        cancelSecond.Cancel();
        Assert.Equal("cancelled", (await second.WaitAsync(TimeSpan.FromSeconds(5))).ErrorCode);
        var windowCall = client.EmbedVideoAsync(server.BaseUrl, window, CancellationToken.None);
        Assert.Equal(1, server.EmbeddingPosts);
        Assert.Equal(0, video.Disposes);
        Assert.Equal(0, window.Disposes);

        server.Release();
        await server.Received(2).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Contains("query-one", server.Bodies[1]);
        Assert.DoesNotContain("query-two", string.Join('\n', server.Bodies));
        Assert.Equal(0, window.Disposes);
        server.Release();
        await server.Received(3).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Contains("query-three", server.Bodies[2]);
        Assert.Equal(0, window.Disposes);
        server.Release();
        await server.Received(4).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Contains("video_audio", server.Bodies[3]);
        Assert.Equal(0, window.Disposes);
        server.Release();
        Assert.True((await first.WaitAsync(TimeSpan.FromSeconds(5))).Success);
        Assert.True((await third.WaitAsync(TimeSpan.FromSeconds(5))).Success);
        Assert.True((await windowCall.WaitAsync(TimeSpan.FromSeconds(5))).Success);
        Assert.True((await videoCall.WaitAsync(TimeSpan.FromSeconds(5))).Success);
        Assert.True(window.Disposes >= 1);
    }

    [Fact]
    public async Task Two_live_callers_both_run_after_the_active_request()
    {
        await using var server = GateServer.Start();
        var client = new HttpEmbeddingClient(dispatcher: new ModelDispatcher());
        var active = client.EmbedTextAsync(server.BaseUrl, "active-user", CancellationToken.None);
        await server.Received(1).WaitAsync(TimeSpan.FromSeconds(5));
        var first = client.EmbedTextAsync(server.BaseUrl, "caller-one", CancellationToken.None);
        var second = client.EmbedTextAsync(server.BaseUrl, "caller-two", CancellationToken.None);
        Assert.Equal(1, server.EmbeddingPosts);
        server.Release();
        await server.Received(2).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Contains("caller-one", server.Bodies[1]);
        server.Release();
        await server.Received(3).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Contains("caller-two", server.Bodies[2]);
        server.Release();
        Assert.True((await active.WaitAsync(TimeSpan.FromSeconds(5))).Success);
        Assert.True((await first.WaitAsync(TimeSpan.FromSeconds(5))).Success);
        Assert.True((await second.WaitAsync(TimeSpan.FromSeconds(5))).Success);
    }

    [Fact]
    public async Task A_full_local_queue_returns_queue_full_without_another_post()
    {
        await using var server = GateServer.Start();
        var client = new HttpEmbeddingClient(dispatcher: new ModelDispatcher());
        var active = client.EmbedTextAsync(server.BaseUrl, "holding", CancellationToken.None);
        await server.Received(1).WaitAsync(TimeSpan.FromSeconds(5));
        using var waiting = new CancellationTokenSource();
        var queued = new List<Task<EmbeddingCall>>();
        for (var index = 0; index < ModelDispatcher.MaxPending; index++)
        {
            queued.Add(client.EmbedTextAsync(server.BaseUrl, "queued-" + index, waiting.Token));
        }

        var overflow = await client.EmbedTextAsync(server.BaseUrl, "overflow", CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("queueFull", overflow.ErrorCode);
        Assert.True(overflow.Retryable);
        Assert.Equal(1, server.EmbeddingPosts);
        Assert.DoesNotContain("overflow", string.Join('\n', server.Bodies));
        waiting.Cancel();
        await Task.WhenAll(queued).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(1, server.EmbeddingPosts);
        server.Release();
        Assert.True((await active.WaitAsync(TimeSpan.FromSeconds(5))).Success);
    }

    [Fact]
    public async Task Abort_after_accept_does_not_resend_until_two_idle_samples()
    {
        await AssertHeldUntilIdle(GatePlan.Abort, "modelOffline");
    }

    [Fact]
    public async Task Hard_timeout_while_reading_the_body_does_not_resend_until_two_idle_samples()
    {
        await AssertHeldUntilIdle(GatePlan.StallHeaders, "timeout");
    }

    [Fact]
    public async Task Mismatched_signature_blocks_the_next_post_until_it_is_renewed()
    {
        var waits = Channel.CreateUnbounded<TaskCompletionSource>();
        await using var server = GateServer.Start();
        server.Plans.Enqueue(GatePlan.Abort);
        server.Admitted.Enqueue(0);
        server.Admitted.Enqueue(0);
        server.ProcessingId = "proc-other";
        var dispatcher = new ModelDispatcher(wait: (delay, token) => Wait(waits, delay, token));
        dispatcher.AllowResume = observation => observation.ProcessingId == "proc-test";
        var client = new HttpEmbeddingClient(dispatcher: dispatcher);
        var first = await client.EmbedTextAsync(server.BaseUrl, "lost", CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.True(first.CompletionUnknown);
        var second = client.EmbedTextAsync(server.BaseUrl, "after", CancellationToken.None);
        await ReleaseProbes(waits, second, server, 1);
        var blocked = await second.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("incompatible", blocked.ErrorCode);
        Assert.Equal(1, server.EmbeddingPosts);

        server.ProcessingId = "proc-test";
        dispatcher.AllowResume = _ => true;
        dispatcher.NoteSignatureRenewed();
        var third = client.EmbedTextAsync(server.BaseUrl, "renewed", CancellationToken.None);
        await server.Received(2).WaitAsync(TimeSpan.FromSeconds(5));
        server.Release();
        Assert.True((await third.WaitAsync(TimeSpan.FromSeconds(5))).Success);
    }

    [Fact]
    public async Task Complete_429_releases_the_slot_and_the_worker_backs_off()
    {
        await using var server = GateServer.Start();
        server.Plans.Enqueue(GatePlan.Status429);
        var dispatcher = new ModelDispatcher();
        var client = new HttpEmbeddingClient(dispatcher: dispatcher);
        using var store = IndexStore.Open(Directory.CreateTempSubdirectory("evs-429-").FullName);
        var library = Guid.NewGuid();
        store.EnsureActiveSignature(Fixture.Model("proc-test"));
        store.SetEmbeddingBaseUrl(server.BaseUrl);
        store.ReplaceLibraries([library]);
        store.UpsertVideo(Guid.NewGuid(), "media-1", library, ApiV1.WindowTicks, "v1", "synthetic.mp4", false);
        var coordinator = new IndexCoordinator(store, client, new MemoryCatalog(), new AcceptingClipper(), Directory.CreateTempSubdirectory("evs-429-tmp-").FullName, dispatcher: dispatcher);
        Assert.Equal(DispatchStep.Failed, await coordinator.DispatchOneAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.Equal(1, server.EmbeddingPosts);
        Assert.Equal(1, store.CountWindows("pending"));
        Assert.Equal(DispatchStep.Backoff, await coordinator.DispatchOneAsync(CancellationToken.None));
        Assert.Equal(1, server.EmbeddingPosts);

        var query = client.EmbedTextAsync(server.BaseUrl, "during-backoff", CancellationToken.None);
        await server.Received(2).WaitAsync(TimeSpan.FromSeconds(5));
        server.Release();
        Assert.True((await query.WaitAsync(TimeSpan.FromSeconds(5))).Success);
        Assert.False(dispatcher.CompletionUnknown);
    }

    private static async Task AssertHeldUntilIdle(GatePlan plan, string error)
    {
        var waits = Channel.CreateUnbounded<TaskCompletionSource>();
        await using var server = GateServer.Start();
        server.Plans.Enqueue(plan);
        server.Admitted.Enqueue(0);
        server.Admitted.Enqueue(1);
        server.Admitted.Enqueue(0);
        server.Admitted.Enqueue(0);
        var hard = plan == GatePlan.StallHeaders ? TimeSpan.FromMilliseconds(300) : ModelDispatcher.QueryHardTimeout;
        var dispatcher = new ModelDispatcher(queryHard: hard, wait: (delay, token) => Wait(waits, delay, token));
        var client = new HttpEmbeddingClient(dispatcher: dispatcher);
        var first = await client.EmbedTextAsync(server.BaseUrl, "lost", CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(error, first.ErrorCode);
        Assert.True(first.CompletionUnknown);
        Assert.Equal(1, server.EmbeddingPosts);

        var second = client.EmbedTextAsync(server.BaseUrl, "retry", CancellationToken.None);
        await ReleaseProbes(waits, server.Received(2), server, 1);
        Assert.Equal(2, server.EmbeddingPosts);
        Assert.Contains("retry", server.Bodies[^1]);
        server.Release();
        Assert.True((await second.WaitAsync(TimeSpan.FromSeconds(5))).Success);
    }

    private static Task Wait(Channel<TaskCompletionSource> waits, TimeSpan delay, CancellationToken token)
    {
        Assert.True(delay >= TimeSpan.FromSeconds(1));
        var gate = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        waits.Writer.TryWrite(gate);
        return gate.Task.WaitAsync(token);
    }

    private static async Task ReleaseProbes(Channel<TaskCompletionSource> waits, Task until, GateServer server, int posts)
    {
        while (!until.IsCompleted)
        {
            var next = waits.Reader.ReadAsync().AsTask();
            var finished = await Task.WhenAny(until, next).WaitAsync(TimeSpan.FromSeconds(5));
            if (finished == until)
            {
                return;
            }

            Assert.Equal(posts, server.EmbeddingPosts);
            (await next).SetResult();
        }
    }
}

internal enum GatePlan
{
    Hold,
    Abort,
    Status429,
    StallHeaders
}

internal sealed class TrackStream : MemoryStream
{
    public TrackStream(byte[] data) : base(data)
    {
    }

    public int Disposes { get; private set; }

    protected override void Dispose(bool disposing)
    {
        Disposes++;
        base.Dispose(disposing);
    }
}

internal sealed class GateServer : IAsyncDisposable
{
    private readonly HttpListener _listener;
    private readonly CancellationTokenSource _stop = new();
    private readonly Task _loop;
    private readonly object _gate = new();
    private readonly Queue<HttpListenerContext> _held = new();
    private readonly List<TaskCompletionSource> _signals = [];
    private int _posts;

    public string BaseUrl { get; }
    public List<string> Bodies { get; } = [];
    public Queue<GatePlan> Plans { get; } = new();
    public Queue<int> Admitted { get; } = new();
    public string ProcessingId { get; set; } = "proc-test";
    public int EmbeddingPosts
    {
        get
        {
            lock (_gate)
            {
                return _posts;
            }
        }
    }

    public static GateServer Start()
    {
        var port = FreePort();
        var listener = new HttpListener();
        listener.Prefixes.Add($"http://127.0.0.1:{port}/");
        listener.Start();
        return new GateServer(listener, $"http://127.0.0.1:{port}");
    }

    private GateServer(HttpListener listener, string baseUrl)
    {
        _listener = listener;
        BaseUrl = baseUrl;
        _loop = Task.Run(Loop);
    }

    public Task Received(int count)
    {
        lock (_gate)
        {
            if (_posts >= count)
            {
                return Task.CompletedTask;
            }

            var signal = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            _signals.Add(signal);
            return signal.Task;
        }
    }

    public void Release()
    {
        HttpListenerContext context;
        lock (_gate)
        {
            context = _held.Dequeue();
        }

        var bytes = Encoding.UTF8.GetBytes(EmbeddingJson());
        context.Response.StatusCode = 200;
        context.Response.ContentType = "application/json";
        context.Response.ContentLength64 = bytes.Length;
        context.Response.OutputStream.Write(bytes, 0, bytes.Length);
        context.Response.Close();
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
                if (path == "/readyz")
                {
                    var admitted = Admitted.Count > 0 ? Admitted.Dequeue() : 0;
                    var payload = Encoding.UTF8.GetBytes($"{{\"ready\":true,\"admitted_requests\":{admitted}}}");
                    context.Response.StatusCode = 200;
                    context.Response.ContentType = "application/json";
                    context.Response.ContentLength64 = payload.Length;
                    await context.Response.OutputStream.WriteAsync(payload).ConfigureAwait(false);
                    context.Response.Close();
                    continue;
                }

                if (path == "/v1/models")
                {
                    var payload = Encoding.UTF8.GetBytes(ModelsJson());
                    context.Response.StatusCode = 200;
                    context.Response.ContentType = "application/json";
                    context.Response.ContentLength64 = payload.Length;
                    await context.Response.OutputStream.WriteAsync(payload).ConfigureAwait(false);
                    context.Response.Close();
                    continue;
                }

                var plan = GatePlan.Hold;
                lock (_gate)
                {
                    Bodies.Add(body);
                    _posts++;
                    if (Plans.Count > 0)
                    {
                        plan = Plans.Dequeue();
                    }

                    foreach (var signal in _signals)
                    {
                        signal.TrySetResult();
                    }
                }

                if (plan == GatePlan.Abort)
                {
                    context.Response.Abort();
                    continue;
                }

                if (plan == GatePlan.Status429)
                {
                    var payload = Encoding.UTF8.GetBytes("{\"error\":{\"code\":\"queue_full\",\"message\":\"busy\"}}");
                    context.Response.StatusCode = 429;
                    context.Response.ContentType = "application/json";
                    context.Response.ContentLength64 = payload.Length;
                    await context.Response.OutputStream.WriteAsync(payload).ConfigureAwait(false);
                    context.Response.Close();
                    continue;
                }

                if (plan == GatePlan.StallHeaders)
                {
                    context.Response.StatusCode = 200;
                    context.Response.SendChunked = true;
                    context.Response.ContentType = "application/json";
                    var started = Encoding.UTF8.GetBytes("{");
                    await context.Response.OutputStream.WriteAsync(started).ConfigureAwait(false);
                    await context.Response.OutputStream.FlushAsync().ConfigureAwait(false);
                    continue;
                }

                lock (_gate)
                {
                    _held.Enqueue(context);
                }
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
        }
    }

    private string EmbeddingJson()
    {
        var vector = new float[ApiV1.Dimension];
        vector[0] = 1f;
        return System.Text.Json.JsonSerializer.Serialize(new
        {
            model = ApiV1.ModelId,
            revision = ApiV1.Revision,
            processing_id = ProcessingId,
            dimension = ApiV1.Dimension,
            normalized = true,
            data = new[] { new { index = 0, embedding = vector } }
        });
    }

    private string ModelsJson()
        => System.Text.Json.JsonSerializer.Serialize(new
        {
            data = new[]
            {
                new
                {
                    id = ApiV1.ModelId,
                    revision = ApiV1.Revision,
                    processing_id = ProcessingId,
                    dimension = ApiV1.Dimension,
                    normalized = true,
                    ready = true
                }
            }
        });

    private static int FreePort()
    {
        using var tcp = new TcpListener(IPAddress.Loopback, 0);
        tcp.Start();
        return ((IPEndPoint)tcp.LocalEndpoint).Port;
    }
}
