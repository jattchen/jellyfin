using Microsoft.Extensions.Logging;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public enum DispatchStep
{
    Published,
    Idle,
    Deferred,
    Paused,
    Playback,
    Blocked,
    Failed,
    Stale,
    Backoff
}

public sealed class IndexableVideo
{
    public string Container { get; init; } = "";
    public Guid ItemId { get; init; }
    public string MediaSourceId { get; init; } = "";
    public Guid LibraryId { get; init; }
    public long RuntimeTicks { get; init; }
    public string ContentVersion { get; init; } = "";
    public string SourcePath { get; init; } = "";
    public bool HasAudio { get; init; }
    public int? AudioStreamIndex { get; init; }
}

public sealed class CatalogScan
{
    public bool Trusted { get; init; }
    public IReadOnlyList<IndexableVideo> Videos { get; init; } = [];
    public IReadOnlyList<Guid> UnreadableItemIds { get; init; } = [];

    public static CatalogScan Untrusted() => new() { Trusted = false };

    public static CatalogScan Ok(IReadOnlyList<IndexableVideo> videos, IReadOnlyList<Guid> unreadable)
        => new() { Trusted = true, Videos = videos, UnreadableItemIds = unreadable };
}

public enum ItemPresence
{
    Missing,
    Unreadable,
    OutOfScope,
    Readable
}

public sealed class CatalogItem
{
    public ItemPresence Presence { get; init; }
    public IndexableVideo? Video { get; init; }
    public IReadOnlyList<IndexableVideo> Videos { get; init; } = [];
    public IReadOnlyList<string> UnreadableSourceIds { get; init; } = [];
}

public interface IMediaFacts
{
    CatalogItem ReadSource(Guid itemId, string mediaSourceId, IReadOnlyCollection<Guid> enabledLibraryIds);
}

public interface IIndexCatalog : IMediaFacts
{
    bool IsVideoPlaybackActive();

    CatalogScan ReadLibraries(IReadOnlyCollection<Guid> libraryIds);

    CatalogItem ReadItem(Guid itemId, IReadOnlyCollection<Guid> enabledLibraryIds);
}

public static class WindowAcceptance
{
    public static bool Matches(LeaseTicket lease, ClipCheck clip, EmbeddingCall call)
    {
        if (!clip.Accepted || call.Processing is null || call.Vector is null)
        {
            return false;
        }

        var expected = (lease.EndTicks - lease.StartTicks) / (double)ApiV1.TicksPerSecond;
        if (clip.SlackSeconds <= 0)
        {
            return false;
        }

        if (call.Processing.VideoDurationSeconds is not double video || Math.Abs(video - expected) > clip.SlackSeconds)
        {
            return false;
        }

        if (Math.Abs(clip.ProbedDurationSeconds - expected) > clip.SlackSeconds || !clip.HasVideo || Math.Abs(clip.StartSeconds) > clip.SlackSeconds)
        {
            return false;
        }

        if (!lease.HasAudio)
        {
            return true;
        }

        return call.Processing.AudioIncluded == true
            && call.Processing.AudioDurationSeconds is double audio
            && Math.Abs(audio - expected) <= clip.SlackSeconds
            && clip.AudioDurationSeconds is double probedAudio
            && Math.Abs(probedAudio - expected) <= clip.SlackSeconds
            && clip.HasAudio;
    }
}

public sealed class IndexCoordinator
{
    private readonly IndexStore _store;
    private readonly IEmbeddingClient _embeddings;
    private readonly IIndexCatalog _catalog;
    private readonly IWindowClipper _clipper;
    private readonly ModelDispatcher? _posts;
    private readonly string _tempDirectory;
    private readonly ILogger _logger;
    private readonly Version _hostVersion;
    private DateTimeOffset _nextDispatch = DateTimeOffset.MinValue;
    private DateTimeOffset _nextReconcile = DateTimeOffset.MaxValue;
    private int _backoffSeconds = 2;

    public IndexCoordinator(
        IndexStore store,
        IEmbeddingClient embeddings,
        IIndexCatalog catalog,
        IWindowClipper clipper,
        string tempDirectory,
        ILogger? logger = null,
        ModelDispatcher? dispatcher = null,
        Version? hostVersion = null)
    {
        _store = store;
        _embeddings = embeddings;
        _catalog = catalog;
        _clipper = clipper;
        _posts = dispatcher;
        _tempDirectory = tempDirectory;
        _logger = logger ?? Microsoft.Extensions.Logging.Abstractions.NullLogger.Instance;
        _hostVersion = hostVersion ?? new Version(12, 2, 0);
    }

    public string? DisabledReason { get; private set; }

    public TimeSpan ReconcileInterval { get; set; } = TimeSpan.FromMinutes(1);

    public TimeSpan BackoffDelay => TimeSpan.FromSeconds(_backoffSeconds);

    public void Enqueue(string kind, Guid itemId)
    {
        if (!WorkerEnabled())
        {
            return;
        }

        _store.Enqueue(kind, itemId);
    }

    public void Start()
    {
        if (!WorkerEnabled())
        {
            DisabledReason = DisableReason();
            _logger.LogInformation("index worker disabled. reason={Reason}", DisabledReason);
            return;
        }

        DisabledReason = null;
        _store.RecoverLeases();
        _store.RequestReconcile();
        _nextReconcile = DateTimeOffset.UtcNow.Add(ReconcileInterval);
        _logger.LogInformation("index worker started");
    }

    public void ProcessEvents()
    {
        if (!WorkerEnabled())
        {
            return;
        }

        if (ReconcileInterval <= TimeSpan.Zero || DateTimeOffset.UtcNow >= _nextReconcile)
        {
            _store.RequestReconcile();
            _nextReconcile = DateTimeOffset.UtcNow.Add(ReconcileInterval <= TimeSpan.Zero ? TimeSpan.FromMinutes(1) : ReconcileInterval);
        }

        if (_store.ConsumeReconcile())
        {
            Reconcile();
        }

        foreach (var queued in _store.TakeEvents(32))
        {
            if (queued.Kind == "reconcile")
            {
                Reconcile();
            }
            else if (queued.Kind == "removed" && queued.ItemId is Guid removed)
            {
                _store.HideItem(removed);
            }
            else if (queued.ItemId is Guid itemId)
            {
                ApplyItem(itemId);
            }

            _store.CompleteEvent(queued.Id);
        }
    }

    public void Reconcile()
    {
        var libraries = _store.EnabledLibraryIds();
        var scan = _catalog.ReadLibraries(libraries);
        if (!scan.Trusted)
        {
            _logger.LogInformation("index reconcile skipped because the library read failed");
            return;
        }

        foreach (var video in scan.Videos)
        {
            _store.UpsertVideo(video.ItemId, video.MediaSourceId, video.LibraryId, video.RuntimeTicks, video.ContentVersion, video.SourcePath, video.HasAudio, video.AudioStreamIndex);
        }

        foreach (var itemId in scan.UnreadableItemIds)
        {
            _store.MarkUnknown(itemId);
        }

        _store.HideMissing(
            libraries,
            scan.Videos.Select(video => (video.ItemId, video.MediaSourceId)).ToHashSet(),
            scan.UnreadableItemIds.ToHashSet());
    }

    public async Task<DispatchStep> DispatchOneAsync(CancellationToken cancellationToken)
    {
        if (!WorkerEnabled())
        {
            return DispatchStep.Blocked;
        }

        if (DateTimeOffset.UtcNow < _nextDispatch)
        {
            return DispatchStep.Backoff;
        }

        if (_store.ManualPause)
        {
            return DispatchStep.Paused;
        }

        if (_catalog.IsVideoPlaybackActive())
        {
            return DispatchStep.Playback;
        }

        if (_store.Incompatible || _store.SchemaReadOnly || !_store.HasWriterLock || string.IsNullOrWhiteSpace(_store.EmbeddingBaseUrl))
        {
            return DispatchStep.Blocked;
        }

        if (_posts is { BlocksNewVideo: true })
        {
            return DispatchStep.Deferred;
        }

        var lease = _store.TryLease();
        if (lease is null)
        {
            return DispatchStep.Idle;
        }

        Directory.CreateDirectory(_tempDirectory);
        var output = Path.Combine(_tempDirectory, lease.Token + ".mp4");
        try
        {
            var clip = await _clipper.ClipAsync(lease, output, cancellationToken).ConfigureAwait(false);
            if (!clip.Accepted)
            {
                _store.FailLease(lease.Token, clip.ErrorCode, clip.Retryable);
                NoteBackoff(clip.Retryable);
                return DispatchStep.Failed;
            }

            await using var stream = File.OpenRead(output);
            // None: a caller stop must not cancel this post. The current window is allowed to finish.
            var embedded = await _embeddings.EmbedVideoAsync(_store.EmbeddingBaseUrl, stream, CancellationToken.None, () => Task.FromResult(RefuseVideo(lease))).ConfigureAwait(false);
            if (embedded.ErrorCode is "paused" or "playback" or "stale" or "incompatible")
            {
                _store.ReleaseLease(lease.Token);
                return embedded.ErrorCode switch
                {
                    "paused" => DispatchStep.Paused,
                    "playback" => DispatchStep.Playback,
                    "incompatible" => DispatchStep.Blocked,
                    _ => DispatchStep.Stale
                };
            }

            if (!embedded.Success || embedded.Vector is null)
            {
                _store.FailLease(lease.Token, embedded.ErrorCode ?? "modelOffline", embedded.Retryable || embedded.CompletionUnknown);
                NoteBackoff(embedded.Retryable && !embedded.CompletionUnknown);
                return DispatchStep.Failed;
            }

            if (!WindowAcceptance.Matches(lease, clip, embedded))
            {
                _store.FailLease(lease.Token, "processing_mismatch", false);
                _logger.LogInformation("index window rejected item {ItemId} segment {SegmentId} reason processing_mismatch", lease.ItemId, lease.SegmentId);
                return DispatchStep.Failed;
            }

            ProcessEvents();
            var active = _store.ActiveSignature();
            if (active is null || active.Generation != lease.Generation || !IndexStore.SignaturesMatch(active, embedded.Observation))
            {
                if (active?.Generation == lease.Generation)
                {
                    _store.SetIncompatible(true);
                }

                _store.ReleaseLease(lease.Token);
                return DispatchStep.Stale;
            }

            if (!LeaseStillMatchesFacts(lease))
            {
                _logger.LogInformation("index window not published item {ItemId} segment {SegmentId} because the media changed", lease.ItemId, lease.SegmentId);
                return DispatchStep.Stale;
            }

            var outcome = _store.CompleteLease(lease.Token, embedded.Vector);
            if (outcome == LeaseOutcome.Published)
            {
                _backoffSeconds = 2;
                _nextDispatch = DateTimeOffset.MinValue;
                _logger.LogInformation("index window published item {ItemId} segment {SegmentId} generation {Generation}", lease.ItemId, lease.SegmentId, lease.Generation);
                return DispatchStep.Published;
            }

            _logger.LogInformation("index window stale item {ItemId} segment {SegmentId} generation {Generation}", lease.ItemId, lease.SegmentId, lease.Generation);
            return DispatchStep.Stale;
        }
        catch (OperationCanceledException)
        {
            _store.ReleaseLease(lease.Token);
            throw;
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "index window failed item {ItemId} segment {SegmentId}", lease.ItemId, lease.SegmentId);
            _store.FailLease(lease.Token, "worker_error", true);
            NoteBackoff(true);
            return DispatchStep.Failed;
        }
        finally
        {
            if (File.Exists(output))
            {
                File.Delete(output);
            }
        }
    }

    private void ApplyItem(Guid itemId)
    {
        var item = _catalog.ReadItem(itemId, _store.EnabledLibraryIds());
        if (item.Presence is ItemPresence.Missing or ItemPresence.OutOfScope)
        {
            _store.HideItem(itemId);
            return;
        }

        if (item.Presence == ItemPresence.Unreadable)
        {
            _store.MarkUnknown(itemId);
            return;
        }

        var videos = item.Videos.Count > 0 ? item.Videos : item.Video is null ? [] : new[] { item.Video };
        foreach (var video in videos)
        {
            _store.UpsertVideo(video.ItemId, video.MediaSourceId, video.LibraryId, video.RuntimeTicks, video.ContentVersion, video.SourcePath, video.HasAudio, video.AudioStreamIndex);
        }
    }

    /// <summary>
    /// Manual pause is checked only before a video post. Reconcile used to run first and plan
    /// full-film windows. Imported-candidate mode stops scan, enqueue, and dispatch here.
    /// </summary>
    private bool WorkerEnabled()
        => HostCompatibility.IsSupported(_hostVersion) && !_store.SchemaReadOnly && _store.HasWriterLock && !_store.ImportedCandidatesOnly;

    private string DisableReason()
    {
        if (!HostCompatibility.IsSupported(_hostVersion))
        {
            return "hostIncompatible";
        }

        if (_store.SchemaReadOnly)
        {
            return "schemaNewer";
        }

        if (!_store.HasWriterLock)
        {
            return "writerLocked";
        }

        return "importedCandidatesOnly";
    }

    private bool LeaseStillMatchesFacts(LeaseTicket lease)
    {
        CatalogItem facts;
        try
        {
            facts = _catalog.ReadSource(lease.ItemId, lease.MediaSourceId, _store.EnabledLibraryIds());
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "index window fact check failed item {ItemId}", lease.ItemId);
            _store.ReleaseLease(lease.Token);
            return false;
        }

        if (facts.Presence == ItemPresence.Unreadable)
        {
            _store.MarkUnknown(lease.ItemId);
            _store.ReleaseLease(lease.Token);
            return false;
        }

        if (facts.Presence is ItemPresence.Missing or ItemPresence.OutOfScope || facts.Video is null || facts.Video.MediaSourceId != lease.MediaSourceId)
        {
            _store.HideItem(lease.ItemId, lease.MediaSourceId);
            return false;
        }

        if (facts.Video.ContentVersion != lease.ContentVersion || !_store.EnabledLibraryIds().Contains(facts.Video.LibraryId))
        {
            _store.UpsertVideo(facts.Video.ItemId, facts.Video.MediaSourceId, facts.Video.LibraryId, facts.Video.RuntimeTicks, facts.Video.ContentVersion, facts.Video.SourcePath, facts.Video.HasAudio, facts.Video.AudioStreamIndex);
            return false;
        }

        return _store.ActiveGeneration == lease.Generation;
    }

    private EmbeddingCall? RefuseVideo(LeaseTicket lease)
    {
        if (_posts is { PostsAllowed: false } || _store.Incompatible)
        {
            return new EmbeddingCall { ErrorCode = "incompatible" };
        }

        if (_store.ManualPause)
        {
            return new EmbeddingCall { ErrorCode = "paused" };
        }

        if (_catalog.IsVideoPlaybackActive())
        {
            return new EmbeddingCall { ErrorCode = "playback" };
        }

        if (_store.ActiveGeneration != lease.Generation)
        {
            return new EmbeddingCall { ErrorCode = "stale" };
        }

        return null;
    }

    private void NoteBackoff(bool retryable)
    {
        if (!retryable)
        {
            return;
        }

        _nextDispatch = DateTimeOffset.UtcNow.AddSeconds(_backoffSeconds);
        _backoffSeconds = Math.Min(_backoffSeconds * 2, 30);
    }
}
