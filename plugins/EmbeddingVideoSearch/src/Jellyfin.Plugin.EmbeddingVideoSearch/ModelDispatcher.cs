using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

/// <summary>
/// One shared sender for query and video posts. At most one post is without a real terminal response.
/// Caller cancellation stops waiting only. It does not cancel the HTTP call or free the slot.
/// </summary>
public sealed class ModelDispatcher : IDisposable
{
    public const int MaxPending = 8;
    public const int IdleSamplesRequired = 2;
    public static readonly TimeSpan IdleSampleGap = TimeSpan.FromSeconds(1);
    public static readonly TimeSpan QuerySoftTimeout = TimeSpan.FromSeconds(30);
    public static readonly TimeSpan QueryHardTimeout = TimeSpan.FromMinutes(2);
    public static readonly TimeSpan VideoHardTimeout = TimeSpan.FromMinutes(3);
    public static readonly TimeSpan ProbeTimeout = TimeSpan.FromSeconds(5);

    private readonly HttpClient _http;
    private readonly bool _ownsHttp;
    private readonly TimeSpan _querySoft;
    private readonly TimeSpan _queryHard;
    private readonly TimeSpan _videoHard;
    private readonly Func<TimeSpan, CancellationToken, Task> _wait;
    private readonly ILogger? _logger;
    private readonly object _lock = new();
    private readonly List<Work> _queue = [];
    private readonly CancellationTokenSource _stop = new();
    private Work? _active;
    private bool _pumping;
    private bool _unknown;
    private bool _postsAllowed = true;
    private string? _unknownBaseUrl;
    private int _posts;
    private int _sequence;

    public ModelDispatcher(
        HttpClient? http = null,
        ILogger? logger = null,
        TimeSpan? querySoft = null,
        TimeSpan? queryHard = null,
        TimeSpan? videoHard = null,
        Func<TimeSpan, CancellationToken, Task>? wait = null)
    {
        _ownsHttp = http is null;
        _http = http ?? new HttpClient();
        _http.Timeout = Timeout.InfiniteTimeSpan;
        _logger = logger;
        _querySoft = querySoft ?? QuerySoftTimeout;
        _queryHard = queryHard ?? QueryHardTimeout;
        _videoHard = videoHard ?? VideoHardTimeout;
        _wait = wait ?? ((delay, token) => Task.Delay(delay, token));
    }

    public HttpClient Http => _http;

    public int PostsStarted => Volatile.Read(ref _posts);

    public bool CompletionUnknown
    {
        get
        {
            lock (_lock)
            {
                return _unknown;
            }
        }
    }

    public bool PostsAllowed
    {
        get
        {
            lock (_lock)
            {
                return _postsAllowed && !_unknown;
            }
        }
    }

    public bool BlocksNewVideo
    {
        get
        {
            lock (_lock)
            {
                return _unknown || !_postsAllowed || _active is not null || _queue.Exists(item => item.Kind == PostKind.Query && !item.Cancelled);
            }
        }
    }

    /// <summary>Return false when the stored signature does not match. The slot is released and further posts wait for <see cref="NoteSignatureRenewed"/>.</summary>
    public Func<ModelObservation, bool>? AllowResume { get; set; }

    public Task<EmbeddingCall> QueryAsync(string? baseUrl, string text, CancellationToken caller, Func<Task<EmbeddingCall?>>? beforeSend)
        => Submit(PostKind.Query, baseUrl, text, null, _querySoft, caller, beforeSend);

    public Task<EmbeddingCall> VideoAsync(string? baseUrl, Stream clip, CancellationToken caller, Func<Task<EmbeddingCall?>>? beforeSend)
        => Submit(PostKind.Video, baseUrl, null, clip, soft: null, caller, beforeSend);

    public void NoteSignatureRenewed()
    {
        lock (_lock)
        {
            _postsAllowed = true;
        }

        Kick();
    }

    public void Dispose()
    {
        _stop.Cancel();
        if (_ownsHttp)
        {
            _http.Dispose();
        }
    }

    private Task<EmbeddingCall> Submit(PostKind kind, string? baseUrl, string? text, Stream? clip, TimeSpan? soft, CancellationToken caller, Func<Task<EmbeddingCall?>>? beforeSend)
    {
        if (caller.IsCancellationRequested)
        {
            clip?.Dispose();
            return Task.FromResult(Done("cancelled"));
        }

        if (!HttpEmbeddingClient.TryUri(baseUrl, "/v1/embeddings", out _))
        {
            clip?.Dispose();
            return Task.FromResult(Done("notConfigured"));
        }

        var work = new Work
        {
            Id = Interlocked.Increment(ref _sequence),
            Kind = kind,
            BaseUrl = baseUrl!,
            Text = text,
            Clip = clip,
            BeforeSend = beforeSend,
            Done = new TaskCompletionSource<EmbeddingCall>(TaskCreationOptions.RunContinuationsAsynchronously)
        };

        var start = false;
        lock (_lock)
        {
            if (_stop.IsCancellationRequested)
            {
                ReleaseClip(work);
                return Task.FromResult(Done("cancelled"));
            }

            if (!_unknown && !_postsAllowed)
            {
                ReleaseClip(work);
                return Task.FromResult(Done("incompatible"));
            }

            if (_queue.Count(item => !item.Cancelled) >= MaxPending)
            {
                ReleaseClip(work);
                return Task.FromResult(Done("queueFull", retryable: true));
            }

            _queue.Add(work);
            if (!_pumping)
            {
                _pumping = true;
                start = true;
            }
        }

        if (start)
        {
            _ = Task.Run(Drain);
        }

        return Wait(work, soft, caller);
    }

    private async Task<EmbeddingCall> Wait(Work work, TimeSpan? soft, CancellationToken caller)
    {
        try
        {
            // WaitAsync stops the caller. It does not cancel work.Done or the HTTP request.
            if (soft is TimeSpan limit)
            {
                return await work.Done.Task.WaitAsync(limit, caller).ConfigureAwait(false);
            }

            return await work.Done.Task.WaitAsync(caller).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            Abandon(work);
            return Done("timeout");
        }
        catch (OperationCanceledException) when (caller.IsCancellationRequested)
        {
            Abandon(work);
            return Done("cancelled");
        }
    }

    private void Abandon(Work work)
    {
        var release = false;
        lock (_lock)
        {
            if (work.Dispatched)
            {
                work.Abandoned = true;
                return;
            }

            work.Cancelled = true;
            _queue.Remove(work);
            release = true;
        }

        if (release)
        {
            ReleaseClip(work);
            work.Done.TrySetResult(Done("cancelled"));
        }
    }

    private async Task Drain()
    {
        while (!_stop.IsCancellationRequested)
        {
            try
            {
                if (IsUnknown())
                {
                    await RecoverAsync().ConfigureAwait(false);
                }

                if (!await ExecuteOneAsync().ConfigureAwait(false))
                {
                    lock (_lock)
                    {
                        if (_queue.Count == 0 && !_unknown && _active is null)
                        {
                            _pumping = false;
                            return;
                        }
                    }
                }
            }
            catch (OperationCanceledException) when (_stop.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex)
            {
                _logger?.LogWarning(ex, "embedding dispatch step failed");
                await SafeWait(IdleSampleGap).ConfigureAwait(false);
            }
        }

        lock (_lock)
        {
            _pumping = false;
        }
    }

    private bool IsUnknown()
    {
        lock (_lock)
        {
            return _unknown;
        }
    }

    private async Task<bool> ExecuteOneAsync()
    {
        Work? selected;
        List<Work> blocked;
        lock (_lock)
        {
            if (_unknown || _active is not null)
            {
                return false;
            }

            blocked = [];
            if (!_postsAllowed)
            {
                blocked.AddRange(_queue);
                _queue.Clear();
                selected = null;
            }
            else
            {
                selected = TakeNext();
            }
        }

        foreach (var item in blocked)
        {
            ReleaseClip(item);
            item.Done.TrySetResult(Done("incompatible"));
        }

        if (selected is null)
        {
            return blocked.Count > 0;
        }

        EmbeddingCall? refused = null;
        if (selected.BeforeSend is not null)
        {
            try
            {
                refused = await selected.BeforeSend().ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                lock (_lock)
                {
                    if (selected.Cancelled)
                    {
                        return true;
                    }
                }

                ReleaseClip(selected);
                selected.Done.TrySetException(ex);
                return true;
            }
        }

        lock (_lock)
        {
            if (selected.Cancelled)
            {
                return true;
            }

            if (selected.Kind == PostKind.Video && _queue.Exists(item => item.Kind == PostKind.Query && !item.Cancelled))
            {
                _queue.Insert(0, selected);
                return true;
            }

            if (refused is not null)
            {
                ReleaseClip(selected);
                selected.Done.TrySetResult(refused);
                return true;
            }

            selected.Dispatched = true;
            _active = selected;
        }

        var outcome = await SendAsync(selected).ConfigureAwait(false);
        lock (_lock)
        {
            if (ReferenceEquals(_active, selected))
            {
                _active = null;
            }

            if (outcome.Unknown)
            {
                _unknown = true;
                _unknownBaseUrl = selected.BaseUrl;
            }
        }

        selected.Done.TrySetResult(outcome.Call);
        return true;
    }

    private Work? TakeNext()
    {
        var next = _queue.FirstOrDefault(item => item.Kind == PostKind.Query && !item.Cancelled)
            ?? _queue.FirstOrDefault(item => item.Kind == PostKind.Video && !item.Cancelled);
        if (next is not null)
        {
            _queue.Remove(next);
        }

        _queue.RemoveAll(item => item.Cancelled);
        return next;
    }

    private async Task<(EmbeddingCall Call, bool Unknown)> SendAsync(Work work)
    {
        var budget = work.Kind == PostKind.Query ? _queryHard : _videoHard;
        using var hard = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        hard.CancelAfter(budget);
        using var request = Build(work);
        work.Clip = null;
        try
        {
            Interlocked.Increment(ref _posts);
            _logger?.LogInformation("embedding post started kind {Kind} count {Count}", work.Kind, _posts);
            using var response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, hard.Token).ConfigureAwait(false);
            var json = await response.Content.ReadAsStringAsync(hard.Token).ConfigureAwait(false);
            return Interpret(response, json);
        }
        catch (OperationCanceledException) when (hard.IsCancellationRequested && !_stop.IsCancellationRequested)
        {
            _logger?.LogInformation("embedding post hard timeout; completion unknown");
            return (Done("timeout", completionUnknown: true), true);
        }
        catch (Exception ex) when (ex is HttpRequestException or IOException)
        {
            _logger?.LogInformation(ex, "embedding post disconnected; completion unknown");
            return (Done("modelOffline", completionUnknown: true), true);
        }
    }

    private static HttpRequestMessage Build(Work work)
    {
        HttpEmbeddingClient.TryUri(work.BaseUrl, "/v1/embeddings", out var uri);
        if (work.Kind == PostKind.Query)
        {
            var payload = JsonSerializer.Serialize(new
            {
                model = ApiV1.ModelId,
                inputs = new[] { new { text = work.Text, task = "query" } }
            });
            return new HttpRequestMessage(HttpMethod.Post, uri)
            {
                Content = new StringContent(payload, Encoding.UTF8, "application/json")
            };
        }

        var videoPayload = JsonSerializer.Serialize(new
        {
            model = ApiV1.ModelId,
            inputs = new[] { new { video = "clip", video_audio = true } }
        });
        var form = new MultipartFormDataContent();
        form.Add(new StringContent(videoPayload, Encoding.UTF8), "request");
        var file = new StreamContent(work.Clip!);
        file.Headers.ContentType = new MediaTypeHeaderValue("video/mp4");
        form.Add(file, "clip", "window.mp4");
        return new HttpRequestMessage(HttpMethod.Post, uri) { Content = form };
    }

    private static (EmbeddingCall Call, bool Unknown) Interpret(HttpResponseMessage response, string json)
    {
        if (!IsJsonObject(json))
        {
            return (Done("modelOffline", completionUnknown: true), true);
        }

        if ((int)response.StatusCode == 429)
        {
            return (Done("queueFull", retryable: true), false);
        }

        if (!response.IsSuccessStatusCode)
        {
            var code = ReadCode(json) ?? "http_" + (int)response.StatusCode;
            if (code == "queue_full")
            {
                code = "queueFull";
            }

            return (Done(code, code == "queueFull"), false);
        }

        return (HttpEmbeddingClient.ParseEmbedding(json), false);
    }

    private static bool IsJsonObject(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            return document.RootElement.ValueKind == JsonValueKind.Object;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    private async Task RecoverAsync()
    {
        string? baseUrl;
        lock (_lock)
        {
            baseUrl = _unknownBaseUrl;
        }

        if (string.IsNullOrWhiteSpace(baseUrl))
        {
            lock (_lock)
            {
                _unknown = false;
            }

            return;
        }

        var streak = 0;
        while (!_stop.IsCancellationRequested)
        {
            var idle = await ProbeIdleAsync(baseUrl).ConfigureAwait(false);
            if (!idle)
            {
                streak = 0;
                await SafeWait(IdleSampleGap).ConfigureAwait(false);
                continue;
            }

            streak++;
            if (streak < IdleSamplesRequired)
            {
                await SafeWait(IdleSampleGap).ConfigureAwait(false);
                continue;
            }

            var observation = await ProbeModelsAsync(baseUrl).ConfigureAwait(false);
            if (observation is null || !observation.Reachable || !observation.Ready)
            {
                streak = 0;
                await SafeWait(IdleSampleGap).ConfigureAwait(false);
                continue;
            }

            var allow = AllowResume?.Invoke(observation) ?? true;
            lock (_lock)
            {
                _unknown = false;
                _postsAllowed = allow;
            }

            _logger?.LogInformation("embedding service reported idle; resumeAllowed {Allowed}", allow);
            return;
        }
    }

    private async Task<bool> ProbeIdleAsync(string baseUrl)
    {
        if (!HttpEmbeddingClient.TryUri(baseUrl, "/readyz", out var uri))
        {
            return false;
        }

        using var budget = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        budget.CancelAfter(ProbeTimeout);
        try
        {
            using var response = await _http.GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, budget.Token).ConfigureAwait(false);
            var json = await response.Content.ReadAsStringAsync(budget.Token).ConfigureAwait(false);
            if (response.StatusCode != System.Net.HttpStatusCode.OK)
            {
                return false;
            }

            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (!root.TryGetProperty("ready", out var ready) || ready.ValueKind != JsonValueKind.True)
            {
                return false;
            }

            if (!root.TryGetProperty("admitted_requests", out var admitted) || admitted.ValueKind != JsonValueKind.Number || !admitted.TryGetDouble(out var value) || value != 0d)
            {
                return false;
            }

            return true;
        }
        catch (Exception ex) when (ex is HttpRequestException or IOException or OperationCanceledException or JsonException)
        {
            return false;
        }
    }

    private async Task<ModelObservation?> ProbeModelsAsync(string baseUrl)
    {
        if (!HttpEmbeddingClient.TryUri(baseUrl, "/v1/models", out var uri))
        {
            return null;
        }

        using var budget = CancellationTokenSource.CreateLinkedTokenSource(_stop.Token);
        budget.CancelAfter(ProbeTimeout);
        try
        {
            using var response = await _http.GetAsync(uri, HttpCompletionOption.ResponseHeadersRead, budget.Token).ConfigureAwait(false);
            var json = await response.Content.ReadAsStringAsync(budget.Token).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode)
            {
                return null;
            }

            return HttpEmbeddingClient.ParseModels(json);
        }
        catch (Exception ex) when (ex is HttpRequestException or IOException or OperationCanceledException or JsonException)
        {
            return null;
        }
    }

    private async Task SafeWait(TimeSpan delay)
    {
        try
        {
            await _wait(delay, _stop.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (_stop.IsCancellationRequested)
        {
        }
    }

    private void Kick()
    {
        var start = false;
        lock (_lock)
        {
            if (_stop.IsCancellationRequested || _pumping)
            {
                return;
            }

            if (_queue.Count == 0 && !_unknown)
            {
                return;
            }

            _pumping = true;
            start = true;
        }

        if (start)
        {
            _ = Task.Run(Drain);
        }
    }

    private static void ReleaseClip(Work work)
    {
        var clip = work.Clip;
        work.Clip = null;
        clip?.Dispose();
    }

    private static EmbeddingCall Done(string code, bool retryable = false, bool completionUnknown = false)
        => new()
        {
            Success = false,
            ErrorCode = code,
            Retryable = retryable,
            CompletionUnknown = completionUnknown
        };

    private static string? ReadCode(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            if (document.RootElement.TryGetProperty("error", out var error) && error.TryGetProperty("code", out var code))
            {
                return code.GetString();
            }
        }
        catch (JsonException)
        {
            return null;
        }

        return null;
    }

    private enum PostKind
    {
        Query,
        Video
    }

    private sealed class Work
    {
        public int Id { get; init; }
        public PostKind Kind { get; init; }
        public string BaseUrl { get; init; } = "";
        public string? Text { get; init; }
        public Stream? Clip { get; set; }
        public Func<Task<EmbeddingCall?>>? BeforeSend { get; init; }
        public TaskCompletionSource<EmbeddingCall> Done { get; init; } = null!;
        public bool Dispatched { get; set; }
        public bool Cancelled { get; set; }
        public bool Abandoned { get; set; }
    }
}
