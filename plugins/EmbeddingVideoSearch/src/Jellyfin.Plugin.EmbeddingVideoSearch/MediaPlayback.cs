using System.Diagnostics;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Jellyfin.Data;
using Jellyfin.Database.Implementations.Enums;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaEncoding;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Http.Features;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.WebUtilities;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed record CandidateMediaBinding(SegmentDetailResponse Detail, IndexableVideo Video);

public sealed partial class SearchRuntime
{
    public CatalogItem InspectCandidateSource(Guid userId, Guid itemId, string mediaSourceId)
    {
        var principal = RequirePrincipal(userId);
        if (!principal.IsAdministrator) { throw new SearchAuthException(403, "denied", "Administrator access is required."); }
        var libraries = VisibleEnabledLibraries(principal, null);
        if (!principal.CanAccess(itemId, mediaSourceId)) { throw new SearchAuthException(404, "notFound", "Source was not found."); }
        return ReadFacts(itemId, mediaSourceId, libraries);
    }

    public CandidateMediaBinding ResolveCandidateMedia(Guid userId, Guid segmentId)
    {
        var decision = GetSegment(userId, segmentId);
        if (decision.StatusCode != 200 || decision.Body is null)
        {
            throw new SearchAuthException(decision.StatusCode, decision.Code ?? "notFound", "The media binding is no longer available.");
        }

        var detail = decision.Body;
        var facts = ReadFacts(detail.ItemId, detail.MediaSourceId, _store.EnabledLibraryIds());
        if (facts.Presence != ItemPresence.Readable || facts.Video is null
            || facts.Video.MediaSourceId != detail.MediaSourceId
            || facts.Video.ContentVersion != _store.ReadStoredContentVersion(detail.ItemId, detail.MediaSourceId)
            || string.IsNullOrEmpty(facts.Video.SourcePath) || detail.EndTicks > facts.Video.RuntimeTicks)
        {
            throw new SearchAuthException(409, "stale", "The source media has changed.");
        }

        return new CandidateMediaBinding(detail, facts.Video);
    }
}

/// <summary>Identifiers never authorize media. Every resource goes through the host's current login and binding checks.</summary>
public sealed class MediaPlaybackStore
{
    internal static Uri LocalOrigin(HttpContext context)
    {
        // ForwardedHeaders can change Request.Scheme to the proxy's external
        // HTTPS scheme. The loopback request must use this listener's transport.
        var scheme = context.Features.Get<ITlsConnectionFeature>() is null ? "http" : "https";
        return new Uri($"{scheme}://127.0.0.1:{context.Connection.LocalPort}{context.Request.PathBase}/");
    }

    private readonly object _gate = new();
    private readonly Dictionary<Guid, MediaPlaybackSession> _sessions = [];
    internal readonly SemaphoreSlim PreviewGate = new(1, 1);
    internal readonly Dictionary<string, byte[]> Previews = [];
    internal readonly Queue<string> PreviewOrder = [];
    internal static readonly HttpClient Http = new(new HttpClientHandler { AllowAutoRedirect = false }) { Timeout = TimeSpan.FromMinutes(2) };

    internal void Add(MediaPlaybackSession session)
    {
        lock (_gate)
        {
            foreach (var id in _sessions.Where(p => p.Value.Expires < DateTimeOffset.UtcNow).Select(p => p.Key).ToArray())
            {
                _sessions.Remove(id);
            }

            if (_sessions.Count >= 16)
            {
                throw new SearchAuthException(429, "busy", "Too many active inline players.");
            }

            _sessions.Add(session.Id, session);
        }
    }

    internal MediaPlaybackSession? Find(Guid id)
    {
        lock (_gate)
        {
            if (!_sessions.TryGetValue(id, out var session) || session.Expires < DateTimeOffset.UtcNow)
            {
                _sessions.Remove(id);
                return null;
            }

            session.Expires = DateTimeOffset.UtcNow.AddMinutes(30);
            return session;
        }
    }

    internal void Remove(Guid id) { lock (_gate) { _sessions.Remove(id); } }

    internal async Task<bool> StopAsync(MediaPlaybackSession session, string authorization, CancellationToken cancellationToken, HttpClient? transport = null)
    {
        session.Revoked = true;
        using var request = new HttpRequestMessage(HttpMethod.Delete, new Uri(session.Origin,
            "Videos/ActiveEncodings" + QueryString.Create(new Dictionary<string, string?>
            {
                ["deviceId"] = session.DeviceId, ["playSessionId"] = session.HostPlaySessionId
            })));
        request.Headers.TryAddWithoutValidation("Authorization", authorization);
        using var response = await (transport ?? Http).SendAsync(request, cancellationToken).ConfigureAwait(false);
        if (!response.IsSuccessStatusCode) { return false; }
        Remove(session.Id);
        return true;
    }

    public static string LoginKey(string authorization) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(authorization)));

    public static Uri SafeUpstream(Uri origin, Guid itemId, string reference)
    {
        if (!Uri.TryCreate(origin, reference, out var uri) || uri.Scheme != origin.Scheme
            || uri.Host != origin.Host || uri.Port != origin.Port || !string.IsNullOrEmpty(uri.UserInfo))
        {
            throw new InvalidDataException("External media reference refused.");
        }

        var path = Uri.UnescapeDataString(uri.AbsolutePath);
        var pathBase = origin.AbsolutePath.TrimEnd('/');
        if (!path.StartsWith(pathBase + $"/videos/{itemId:D}/", StringComparison.OrdinalIgnoreCase)
            && !path.StartsWith(pathBase + $"/videos/{itemId:N}/", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Media reference does not belong to this item.");
        }

        if (!new[] { ".m3u8", ".mp4", ".m4s", ".ts", ".aac" }.Any(e => path.EndsWith(e, StringComparison.OrdinalIgnoreCase)))
        {
            throw new InvalidDataException("Unsupported media resource.");
        }

        static bool Secret(string key) => key.Replace("_", "", StringComparison.Ordinal).Replace("-", "", StringComparison.Ordinal).ToLowerInvariant()
            is "apikey" or "token" or "accesstoken" or "authorization";
        var query = QueryHelpers.ParseQuery(uri.Query).Where(p => !Secret(p.Key))
            .SelectMany(p => p.Value.Select(v => new KeyValuePair<string, string?>(p.Key, v)));
        return new UriBuilder(uri) { Query = QueryString.Create(query).Value?.TrimStart('?'), Fragment = "" }.Uri;
    }
}

internal sealed class MediaPlaybackSession
{
    public Guid Id { get; } = Guid.NewGuid();
    public required Guid UserId { get; init; }
    public required string LoginKey { get; init; }
    public required Guid SegmentId { get; init; }
    public required Guid ItemId { get; init; }
    public required string MediaSourceId { get; init; }
    public required string ContentVersion { get; init; }
    public required string BindingVersion { get; init; }
    public required Uri Origin { get; init; }
    public required string HostPlaySessionId { get; init; }
    public required string DeviceId { get; init; }
    public DateTimeOffset Expires { get; set; } = DateTimeOffset.UtcNow.AddMinutes(30);
    public volatile bool Revoked;
    private readonly Dictionary<Guid, Uri> _resources = [];

    public Guid AddResource(Uri uri)
    {
        lock (_resources)
        {
            var found = _resources.FirstOrDefault(p => p.Value == uri);
            if (found.Key != Guid.Empty) { return found.Key; }
            if (_resources.Count >= 4096) { throw new InvalidDataException("Media resource limit reached."); }
            var id = Guid.NewGuid(); _resources.Add(id, uri); return id;
        }
    }

    public Uri? Resource(Guid id) { lock (_resources) { return _resources.GetValueOrDefault(id); } }
}

[ApiController]
[Authorize]
[Route(ApiV1.Route)]
public sealed class EmbeddingVideoMediaController : PluginControllerBase
{
    private readonly MediaPlaybackStore _media;
    private readonly IUserManager _users;
    private readonly IMediaEncoder _encoder;

    public EmbeddingVideoMediaController(SearchRuntime runtime, MediaPlaybackStore media, IUserManager users, IMediaEncoder encoder) : base(runtime)
    {
        _media = media; _users = users; _encoder = encoder;
    }

    private Guid AuthorizePlayback()
    {
        if (!TryUser(out var userId, out _)) { throw new SearchAuthException(401, "unauthorized", "Sign in to play media."); }
        var user = _users.GetUserById(userId);
        if (user is null || user.HasPermission(PermissionKind.IsDisabled) || !user.HasPermission(PermissionKind.EnableMediaPlayback)
            || !user.HasPermission(PermissionKind.EnablePlaybackRemuxing))
        {
            throw new SearchAuthException(403, "denied", "Media playback is not allowed.");
        }

        NoCache();
        return userId;
    }

    private void NoCache() { Response.Headers.CacheControl = "private, no-store"; Response.Headers["X-Content-Type-Options"] = "nosniff"; }
    private string Authorization => Request.Headers.Authorization.ToString();
    private string ResourcePath(MediaPlaybackSession s, Guid id) => $"{Request.PathBase}/{ApiV1.Route}/playback/{s.Id:D}/resources/{id:D}";

    [HttpGet("admin/source-facts/{itemId:guid}")]
    [Authorize(Policy = MediaBrowser.Common.Api.Policies.RequiresElevation)]
    public ActionResult SourceFacts(Guid itemId, [FromQuery] string mediaSourceId)
    {
        if (!TryUser(out var userId, out var error)) { return error; }
        try
        {
            NoCache();
            var facts = Runtime.InspectCandidateSource(userId, itemId, mediaSourceId);
            return JsonBody(new { presence = facts.Presence.ToString(), itemId = facts.Video?.ItemId,
                mediaSourceId = facts.Video?.MediaSourceId, libraryId = facts.Video?.LibraryId,
                contentVersion = facts.Video?.ContentVersion, runtimeTicks = facts.Video?.RuntimeTicks,
                container = facts.Video?.Container,
                audioStreamIndex = facts.Video?.AudioStreamIndex });
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException) { return FromException(ex); }
    }

    private MediaPlaybackSession RequireSession(Guid id)
    {
        var userId = AuthorizePlayback();
        var s = _media.Find(id);
        if (s is null || s.Revoked || s.UserId != userId || s.LoginKey != MediaPlaybackStore.LoginKey(Authorization))
        {
            throw new SearchAuthException(404, "notFound", "Playback was not found.");
        }

        var fresh = Runtime.ResolveCandidateMedia(userId, s.SegmentId);
        if (fresh.Detail.ItemId != s.ItemId || fresh.Detail.MediaSourceId != s.MediaSourceId || fresh.Video.ContentVersion != s.ContentVersion
            || fresh.Detail.BindingVersion != s.BindingVersion)
        {
            s.Revoked = true;
            throw new SearchAuthException(409, "stale", "The media binding changed.");
        }

        return s;
    }

    [HttpPost("playback")]
    public async Task<ActionResult> Prepare(CancellationToken cancellationToken)
    {
        try
        {
            var userId = AuthorizePlayback();
            using var body = JsonDocument.Parse(await ReadBodyAsync(cancellationToken).ConfigureAwait(false));
            if (body.RootElement.ValueKind != JsonValueKind.Object || !body.RootElement.TryGetProperty("segmentId", out var value)
                || value.ValueKind != JsonValueKind.String || !Guid.TryParse(value.GetString(), out var segmentId))
            {
                return Error(400, "invalidRequest", "A segment identity is required.");
            }

            var binding = Runtime.ResolveCandidateMedia(userId, segmentId);
            var detail = binding.Detail;
            if (!body.RootElement.TryGetProperty("expectedBindingVersion", out var expected)
                || expected.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(detail.BindingVersion) || expected.GetString() != detail.BindingVersion)
            {
                return Error(409, "stale", "The clicked media binding has changed.");
            }
            // Internal fixed loopback only. Never trust Host or a caller-supplied upstream URL.
            var origin = MediaPlaybackStore.LocalOrigin(HttpContext);
            var deviceId = User.FindFirst("Jellyfin-DeviceId")?.Value ?? "segment-inline";
            var profile = new
            {
                Name = "Segment inline remux", MaxStreamingBitrate = 120_000_000, MaxStaticBitrate = 120_000_000,
                DirectPlayProfiles = new[] { new { Container = "mp4,m4v,mov,mkv", Type = "Video", VideoCodec = "hevc,h264", AudioCodec = "aac" } },
                TranscodingProfiles = new[] { new { Container = "mp4", Type = "Video", VideoCodec = "hevc,h264", AudioCodec = "aac", Protocol = "hls", Context = "Streaming", MinSegments = 1, SegmentLength = 3 } },
                CodecProfiles = Array.Empty<object>(), ContainerProfiles = Array.Empty<object>(), SubtitleProfiles = Array.Empty<object>()
            };
            using var request = new HttpRequestMessage(HttpMethod.Post, new Uri(origin, $"Items/{detail.ItemId:N}/PlaybackInfo"));
            request.Headers.TryAddWithoutValidation("Authorization", Authorization);
            request.Content = new StringContent(JsonSerializer.Serialize(new
            {
                UserId = userId, MediaSourceId = detail.MediaSourceId, StartTimeTicks = 0,
                IsPlayback = false, AutoOpenLiveStream = false, EnableDirectPlay = false,
                EnableDirectStream = true, AllowVideoStreamCopy = true, AllowAudioStreamCopy = true, DeviceProfile = profile
            }), Encoding.UTF8, "application/json");
            using var response = await MediaPlaybackStore.Http.SendAsync(request, cancellationToken).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode) { return Error(409, "playbackUnavailable", "The host could not prepare playback."); }
            using var info = JsonDocument.Parse(await response.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false));
            var sources = info.RootElement.GetProperty("MediaSources");
            var source = sources.EnumerateArray().FirstOrDefault(s => s.GetProperty("Id").GetString() == detail.MediaSourceId);
            if (source.ValueKind != JsonValueKind.Object || !source.TryGetProperty("TranscodingUrl", out var url) || string.IsNullOrEmpty(url.GetString()))
            {
                return Error(409, "playbackUnavailable", "No compatible HLS playback source is available.");
            }

            var manifest = MediaPlaybackStore.SafeUpstream(origin, detail.ItemId, url.GetString()!);
            // This bounded pilot reuses the original codecs. Do not silently start a video encoder.
            var q = QueryHelpers.ParseQuery(manifest.Query).ToDictionary(p => p.Key, p => p.Value.ToString(), StringComparer.OrdinalIgnoreCase);
            var reasons = q.GetValueOrDefault("TranscodeReasons", "");
            if (reasons.Split(',').Any(reason => reason.Contains("Bitrate", StringComparison.OrdinalIgnoreCase)))
            {
                return Error(409, "playbackUnavailable", "The host requires bitrate conversion; inline playback only remuxes existing codecs.");
            }
            q["VideoCodec"] = "copy"; q["AudioCodec"] = "copy";
            manifest = new UriBuilder(manifest) { Query = QueryString.Create(q.Select(p => new KeyValuePair<string, string?>(p.Key, p.Value))).Value?.TrimStart('?') }.Uri;
            var s = new MediaPlaybackSession
            {
                UserId = userId, LoginKey = MediaPlaybackStore.LoginKey(Authorization), SegmentId = segmentId,
                ItemId = detail.ItemId, MediaSourceId = detail.MediaSourceId, ContentVersion = binding.Video.ContentVersion,
                BindingVersion = detail.BindingVersion,
                Origin = origin, HostPlaySessionId = info.RootElement.TryGetProperty("PlaySessionId", out var ps) ? ps.GetString() ?? "" : "", DeviceId = deviceId
            };
            var resource = s.AddResource(manifest);
            _media.Add(s);
            try { _ = RequireSession(s.Id); } // Refuse revocation/content changes during negotiation.
            catch { _media.Remove(s.Id); throw; }
            return JsonBody(new { playbackId = s.Id, manifestUrl = ResourcePath(s, resource), itemId = detail.ItemId,
                mediaSourceId = detail.MediaSourceId, bindingVersion = detail.BindingVersion,
                startTicks = detail.StartTicks, durationTicks = binding.Video.RuntimeTicks, timelineOriginTicks = 0 });
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException or JsonException or HttpRequestException or InvalidDataException)
        {
            return ex is InvalidDataException ? Error(409, "playbackUnavailable", "The host returned an unsupported media reference.") : FromException(ex);
        }
    }

    [HttpGet("playback/{playbackId:guid}/resources/{resourceId:guid}")]
    public async Task<ActionResult> Resource(Guid playbackId, Guid resourceId, CancellationToken cancellationToken)
    {
        try
        {
            var s = RequireSession(playbackId);
            var uri = s.Resource(resourceId);
            if (uri is null) { return Error(404, "notFound", "Media resource was not found."); }
            using var request = new HttpRequestMessage(HttpMethod.Get, uri);
            request.Headers.TryAddWithoutValidation("Authorization", Authorization);
            if (!string.IsNullOrEmpty(Request.Headers.Range)) { request.Headers.TryAddWithoutValidation("Range", Request.Headers.Range.ToString()); }
            using var upstream = await MediaPlaybackStore.Http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken).ConfigureAwait(false);
            if (!upstream.IsSuccessStatusCode) { return Error((int)upstream.StatusCode, "mediaUnavailable", "The media resource is unavailable."); }
            _ = RequireSession(playbackId);
            if (uri.AbsolutePath.EndsWith(".m3u8", StringComparison.OrdinalIgnoreCase))
            {
                var text = await upstream.Content.ReadAsStringAsync(cancellationToken).ConfigureAwait(false);
                if (text.Length > 2_000_000) { throw new InvalidDataException("Manifest is too large."); }
                string Map(string reference)
                {
                    var target = MediaPlaybackStore.SafeUpstream(s.Origin, s.ItemId, new Uri(uri, reference).AbsoluteUri);
                    return ResourcePath(s, s.AddResource(target));
                }
                var lines = text.Split('\n').Select(line =>
                {
                    var stripped = line.Trim();
                    if (stripped.Length == 0) { return line; }
                    if (!stripped.StartsWith('#')) { return Map(stripped); }
                    return Regex.Replace(line, "URI=\"([^\"]+)\"", match => "URI=\"" + Map(match.Groups[1].Value) + "\"");
                });
                return Content(string.Join('\n', lines), "application/vnd.apple.mpegurl");
            }

            Response.StatusCode = (int)upstream.StatusCode;
            Response.ContentType = upstream.Content.Headers.ContentType?.ToString() ?? "video/mp4";
            if (upstream.Content.Headers.ContentLength is long length) { Response.ContentLength = length; }
            if (upstream.Content.Headers.ContentRange is { } range) { Response.Headers.ContentRange = range.ToString(); }
            await using var stream = await upstream.Content.ReadAsStreamAsync(cancellationToken).ConfigureAwait(false);
            var buffer = new byte[65536];
            var nextCheck = DateTimeOffset.UtcNow.AddSeconds(1);
            int count;
            while ((count = await stream.ReadAsync(buffer, cancellationToken).ConfigureAwait(false)) > 0)
            {
                if (DateTimeOffset.UtcNow >= nextCheck) { _ = RequireSession(playbackId); nextCheck = DateTimeOffset.UtcNow.AddSeconds(1); }
                await Response.Body.WriteAsync(buffer.AsMemory(0, count), cancellationToken).ConfigureAwait(false);
            }

            return new EmptyResult();
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException or HttpRequestException or InvalidDataException)
        {
            if (Response.HasStarted) { HttpContext.Abort(); return new EmptyResult(); }
            return ex is InvalidDataException ? Error(409, "mediaUnavailable", "An unsupported media reference was refused.") : FromException(ex);
        }
    }

    [HttpDelete("playback/{playbackId:guid}")]
    public async Task<ActionResult> Stop(Guid playbackId, CancellationToken cancellationToken)
    {
        try
        {
            // Cleanup must still work after the content or playback permission changes.
            // Ownership and the original host login remain mandatory.
            if (!TryUser(out var userId, out var error)) { return error; }
            var s = _media.Find(playbackId);
            if (s is null || s.UserId != userId || s.LoginKey != MediaPlaybackStore.LoginKey(Authorization))
            {
                return Error(404, "notFound", "Playback was not found.");
            }
            NoCache();
            // The official host owns HLS resources and encoder lifetime.
            if (!await _media.StopAsync(s, Authorization, cancellationToken).ConfigureAwait(false))
            {
                return Error(503, "cleanupPending", "Playback access is revoked; host resource cleanup is pending.");
            }
            return JsonBody(new { stopped = true });
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException) { return FromException(ex); }
        catch (HttpRequestException) { return Error(503, "cleanupPending", "Playback access is revoked; host resource cleanup is pending."); }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested) { return Error(503, "cleanupPending", "Playback access is revoked; host resource cleanup timed out."); }
    }

    [HttpGet("segments/{segmentId:guid}/preview")]
    public async Task<ActionResult> Preview(Guid segmentId, [FromQuery] string? expectedBindingVersion, CancellationToken cancellationToken)
    {
        try
        {
            var userId = AuthorizePlayback();
            var binding = Runtime.ResolveCandidateMedia(userId, segmentId);
            if (string.IsNullOrEmpty(binding.Detail.BindingVersion) || binding.Detail.BindingVersion != expectedBindingVersion)
            {
                return Error(409, "stale", "The preview binding has changed.");
            }
            var key = $"{binding.Detail.ItemId:N}|{binding.Video.MediaSourceId}|{binding.Video.ContentVersion}|{binding.Detail.StartTicks}";
            byte[]? image;
            lock (_media.Previews) { _media.Previews.TryGetValue(key, out image); }
            if (image is null)
            {
                if (!await _media.PreviewGate.WaitAsync(0, cancellationToken).ConfigureAwait(false)) { return Error(429, "busy", "Another preview is being prepared."); }
                try
                {
                    var start = new ProcessStartInfo(_encoder.EncoderPath ?? "") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, CreateNoWindow = true };
                    foreach (var arg in new[] { "-v", "error", "-threads", "1", "-ss", (binding.Detail.StartTicks / 10_000_000d).ToString("R", System.Globalization.CultureInfo.InvariantCulture), "-i", binding.Video.SourcePath, "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", "-f", "image2pipe", "pipe:1" }) { start.ArgumentList.Add(arg); }
                    using var process = Process.Start(start);
                    if (process is null) { return Error(404, "previewUnavailable", "No preview is available."); }
                    using var budget = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken); budget.CancelAfter(TimeSpan.FromSeconds(15));
                    using var imageBytes = new MemoryStream();
                    var stderr = process.StandardError.ReadToEndAsync(budget.Token);
                    try
                    {
                        await process.StandardOutput.BaseStream.CopyToAsync(imageBytes, budget.Token).ConfigureAwait(false);
                        await process.WaitForExitAsync(budget.Token).ConfigureAwait(false);
                        _ = await stderr.ConfigureAwait(false);
                        if (process.ExitCode != 0 || imageBytes.Length is < 4 or > 1_000_000) { return Error(404, "previewUnavailable", "No preview is available."); }
                        image = imageBytes.ToArray();
                    }
                    finally { if (!process.HasExited) { process.Kill(true); } }
                    var fresh = Runtime.ResolveCandidateMedia(userId, segmentId);
                    if (fresh.Detail.BindingVersion != binding.Detail.BindingVersion) { return Error(409, "stale", "The source media changed."); }
                    lock (_media.Previews)
                    {
                        if (!_media.Previews.ContainsKey(key))
                        {
                            while (_media.Previews.Count >= 128) { _media.Previews.Remove(_media.PreviewOrder.Dequeue()); }
                            _media.Previews.Add(key, image); _media.PreviewOrder.Enqueue(key);
                        }
                    }
                }
                finally { _media.PreviewGate.Release(); }
            }

            _ = AuthorizePlayback();
            if (Runtime.ResolveCandidateMedia(userId, segmentId).Detail.BindingVersion != expectedBindingVersion)
            {
                return Error(409, "stale", "The preview binding has changed.");
            }
            return File(image, "image/jpeg");
        }
        catch (Exception ex) when (ex is SearchAuthException or SearchInputException) { return FromException(ex); }
        catch (Exception ex) when (ex is IOException or System.ComponentModel.Win32Exception) { return Error(404, "previewUnavailable", "No preview is available."); }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested) { return Error(404, "previewUnavailable", "Preview preparation timed out."); }
    }
}
