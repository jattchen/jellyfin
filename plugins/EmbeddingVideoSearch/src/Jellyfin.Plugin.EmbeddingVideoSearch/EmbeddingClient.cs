using System.Text;
using System.Text.Json;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed class ModelObservation
{
    public bool Reachable { get; init; }
    public bool Ready { get; init; }
    public string Model { get; init; } = "";
    public string Revision { get; init; } = "";
    public string? ProcessingId { get; init; }
    public int Dimension { get; init; }
    public bool Normalized { get; init; }
    public string? ErrorCode { get; init; }

    public bool IsExpectedModel
        => Model == ApiV1.ModelId
           && Revision == ApiV1.Revision
           && Dimension == ApiV1.Dimension
           && Normalized
           && !string.IsNullOrWhiteSpace(ProcessingId);
}

public sealed class WindowProcessing
{
    public double? VideoDurationSeconds { get; init; }
    public bool? AudioIncluded { get; init; }
    public double? AudioDurationSeconds { get; init; }
}

public sealed class EmbeddingCall
{
    public bool Success { get; init; }
    public float[]? Vector { get; init; }
    public ModelObservation Observation { get; init; } = new();
    public string? ErrorCode { get; init; }

    /// <summary>A later worker may try again. This client does not retry.</summary>
    public bool Retryable { get; init; }

    /// <summary>The HTTP call ended without a complete response, so the slot stays occupied until the service reports idle.</summary>
    public bool CompletionUnknown { get; init; }

    public WindowProcessing? Processing { get; init; }
}

public interface IEmbeddingClient
{
    Task<ModelObservation> ObserveAsync(string? baseUrl, CancellationToken cancellationToken);
    Task<EmbeddingCall> EmbedTextAsync(string? baseUrl, string text, CancellationToken cancellationToken, Func<Task<EmbeddingCall?>>? beforeSend = null);
    Task<EmbeddingCall> EmbedVideoAsync(string? baseUrl, Stream clip, CancellationToken cancellationToken, Func<Task<EmbeddingCall?>>? beforeSend = null);

    /// <summary>Allow posts again after an administrator rebuilds onto the current model signature.</summary>
    void NoteSignatureRenewed()
    {
    }
}

public sealed class HttpEmbeddingClient : IEmbeddingClient
{
    public static readonly TimeSpan MetadataTimeout = TimeSpan.FromSeconds(5);
    public static readonly TimeSpan TextTimeout = ModelDispatcher.QuerySoftTimeout;

    /// <summary>
    /// One video attempt. A measured 6-second window took 20.65 seconds, so the hard budget stays above that sample.
    /// </summary>
    public static readonly TimeSpan VideoTimeout = ModelDispatcher.VideoHardTimeout;

    private readonly HttpClient _http;
    private readonly ModelDispatcher _dispatcher;
    private readonly TimeSpan _metadataTimeout;

    public HttpEmbeddingClient(
        HttpClient? http = null,
        TimeSpan? metadataTimeout = null,
        TimeSpan? textTimeout = null,
        TimeSpan? videoTimeout = null,
        ModelDispatcher? dispatcher = null)
    {
        _metadataTimeout = metadataTimeout ?? MetadataTimeout;
        if (dispatcher is not null)
        {
            _dispatcher = dispatcher;
            _http = dispatcher.Http;
            return;
        }

        _http = http ?? new HttpClient();
        _http.Timeout = Timeout.InfiniteTimeSpan;
        _dispatcher = new ModelDispatcher(_http, querySoft: textTimeout, videoHard: videoTimeout);
    }

    public ModelDispatcher Dispatcher => _dispatcher;

    public void NoteSignatureRenewed() => _dispatcher.NoteSignatureRenewed();

    public Task<ModelObservation> ObserveAsync(string? baseUrl, CancellationToken cancellationToken)
        => SendAsync(baseUrl, HttpMethod.Get, "/v1/models", null, _metadataTimeout, cancellationToken);

    public Task<EmbeddingCall> EmbedTextAsync(string? baseUrl, string text, CancellationToken cancellationToken, Func<Task<EmbeddingCall?>>? beforeSend = null)
        => _dispatcher.QueryAsync(baseUrl, text, cancellationToken, beforeSend);

    public Task<EmbeddingCall> EmbedVideoAsync(string? baseUrl, Stream clip, CancellationToken cancellationToken, Func<Task<EmbeddingCall?>>? beforeSend = null)
        => _dispatcher.VideoAsync(baseUrl, clip, cancellationToken, beforeSend);

    private async Task<ModelObservation> SendAsync(string? baseUrl, HttpMethod method, string path, HttpContent? body, TimeSpan budget, CancellationToken cancellationToken)
    {
        if (!TryUri(baseUrl, path, out var uri))
        {
            return Offline("notConfigured");
        }

        using var request = new HttpRequestMessage(method, uri) { Content = body };
        using var budgetSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        budgetSource.CancelAfter(budget);
        HttpResponseMessage response;
        try
        {
            response = await _http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, budgetSource.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return Offline("cancelled");
        }
        catch (OperationCanceledException)
        {
            return Offline("timeout");
        }
        catch (HttpRequestException)
        {
            return Offline("modelOffline");
        }

        using (response)
        {
        string json;
        try
        {
            json = await response.Content.ReadAsStringAsync(budgetSource.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            return Offline("cancelled");
        }
        catch (OperationCanceledException)
        {
            return Offline("timeout");
        }
        catch (HttpRequestException)
        {
            return Offline("modelOffline");
        }
        if ((int)response.StatusCode == 429)
        {
            return Offline("queueFull");
        }

        if (!response.IsSuccessStatusCode)
        {
            return Offline(NormalizeCode(ReadErrorCode(json) ?? "http_" + (int)response.StatusCode));
        }

        return ParseModels(json);
        }
    }

    private static string NormalizeCode(string code)
        => code == "queue_full" ? "queueFull" : code;

    public static bool TryUri(string? baseUrl, string path, out Uri uri)
    {
        uri = null!;
        if (string.IsNullOrWhiteSpace(baseUrl) || !Uri.TryCreate(baseUrl, UriKind.Absolute, out var parsed))
        {
            return false;
        }

        if (parsed.Scheme is not ("http" or "https") || !string.IsNullOrEmpty(parsed.UserInfo) || parsed.AbsolutePath is not "/" )
        {
            return false;
        }

        uri = new Uri(parsed, path.TrimStart('/'));
        return true;
    }

    public static EmbeddingCall ParseEmbedding(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            var observation = ReadSignature(root);
            if (!observation.IsExpectedModel)
            {
                return new EmbeddingCall { Success = false, Observation = observation, ErrorCode = "unexpectedModel" };
            }

            if (!root.TryGetProperty("data", out var data) || data.GetArrayLength() < 1)
            {
                return new EmbeddingCall { Success = false, Observation = observation, ErrorCode = "invalidOutput" };
            }

            var embedding = data[0].GetProperty("embedding");
            var vector = new float[embedding.GetArrayLength()];
            var index = 0;
            foreach (var value in embedding.EnumerateArray())
            {
                vector[index++] = value.GetSingle();
            }

            if (!VectorMath.IsUnit(vector))
            {
                return new EmbeddingCall { Success = false, Observation = observation, ErrorCode = "invalidOutput" };
            }

        return new EmbeddingCall { Success = true, Vector = vector, Observation = observation, Processing = ReadProcessing(data[0]) };
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or FormatException or InvalidOperationException)
        {
            return Failed("invalidOutput");
        }
    }

    private static WindowProcessing ReadProcessing(JsonElement item)
    {
        if (!item.TryGetProperty("processing", out var processing) || processing.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
        {
            return new WindowProcessing();
        }

        double? videoDuration = null;
        bool? audioIncluded = null;
        double? audioDuration = null;
        if (processing.TryGetProperty("video", out var video))
        {
            if (video.TryGetProperty("duration_seconds", out var duration) && duration.TryGetDouble(out var seconds))
            {
                videoDuration = seconds;
            }

            if (video.TryGetProperty("audio_included", out var included) && included.ValueKind is JsonValueKind.True or JsonValueKind.False)
            {
                audioIncluded = included.GetBoolean();
            }
        }

        if (processing.TryGetProperty("audio", out var audio) && audio.TryGetProperty("duration_seconds", out var audioSeconds) && audioSeconds.TryGetDouble(out var audioValue))
        {
            audioDuration = audioValue;
        }

        return new WindowProcessing
        {
            VideoDurationSeconds = videoDuration,
            AudioIncluded = audioIncluded,
            AudioDurationSeconds = audioDuration
        };
    }

    public static ModelObservation ParseModels(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (root.TryGetProperty("data", out var data) && data.ValueKind == JsonValueKind.Array && data.GetArrayLength() > 0)
            {
                return ReadSignature(data[0]);
            }

            return ReadSignature(root);
        }
        catch (Exception ex) when (ex is JsonException or InvalidOperationException or FormatException)
        {
            return Offline("invalidOutput");
        }
    }

    private static ModelObservation ReadSignature(JsonElement element)
    {
        var ready = !element.TryGetProperty("ready", out var readyElement) || readyElement.ValueKind != JsonValueKind.False;
        return new ModelObservation
        {
            Reachable = true,
            Ready = ready,
            Model = element.TryGetProperty("model", out var model) ? model.GetString() ?? "" : element.TryGetProperty("id", out var id) ? id.GetString() ?? "" : "",
            Revision = element.TryGetProperty("revision", out var revision) ? revision.GetString() ?? "" : "",
            ProcessingId = element.TryGetProperty("processing_id", out var processing) ? processing.GetString() : null,
            Dimension = element.TryGetProperty("dimension", out var dimension) ? dimension.GetInt32() : 0,
            Normalized = element.TryGetProperty("normalized", out var normalized) && normalized.ValueKind == JsonValueKind.True
        };
    }

    private static string? ReadErrorCode(string json)
    {
        try
        {
            using var document = JsonDocument.Parse(json);
            if (document.RootElement.ValueKind == JsonValueKind.Object
                && document.RootElement.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.Object
                && error.TryGetProperty("code", out var code) && code.ValueKind == JsonValueKind.String)
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

    private static ModelObservation Offline(string code)
        => new() { Reachable = false, ErrorCode = code };

    private static EmbeddingCall Failed(string code, bool retryable = false)
        => new() { Success = false, ErrorCode = code, Retryable = retryable, Observation = Offline(code) };
}
