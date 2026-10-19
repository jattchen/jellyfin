using System.Globalization;
using Jellyfin.Data.Enums;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Controller.MediaEncoding;
using MediaBrowser.Controller.Session;
using MediaBrowser.Model.Entities;
using MediaBrowser.Model.MediaInfo;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed class JellyfinIndexCatalog : IIndexCatalog
{
    private readonly ILibraryManager _library;
    private readonly ISessionManager _sessions;
    private readonly IMediaEncoder _encoder;

    public JellyfinIndexCatalog(ILibraryManager library, ISessionManager sessions, IMediaEncoder encoder)
    {
        _library = library;
        _sessions = sessions;
        _encoder = encoder;
    }

    public string FfmpegPath => _encoder.EncoderPath ?? "";

    public bool IsVideoPlaybackActive()
    {
        foreach (var session in _sessions.Sessions)
        {
            if (session.NowPlayingItem is { MediaType: MediaType.Video })
            {
                return true;
            }
        }

        return false;
    }

    public CatalogScan ReadLibraries(IReadOnlyCollection<Guid> libraryIds)
    {
        var videos = new List<IndexableVideo>();
        var unreadable = new List<Guid>();
        try
        {
            foreach (var libraryId in libraryIds)
            {
                var items = _library.GetItemList(new InternalItemsQuery
                {
                    ParentId = libraryId,
                    Recursive = true,
                    IncludeItemTypes = [BaseItemKind.Movie, BaseItemKind.Episode, BaseItemKind.Video],
                    IsFolder = false
                });
                foreach (var item in items)
                {
                    if (item.ExtraType is not null)
                    {
                        continue;
                    }

                    ReadItemSources(item, libraryId, videos, unreadable, null);
                }
            }
        }
        catch (Exception)
        {
            return CatalogScan.Untrusted();
        }

        return CatalogScan.Ok(videos, unreadable);
    }

    public CatalogItem ReadItem(Guid itemId, IReadOnlyCollection<Guid> enabledLibraryIds)
    {
        BaseItem? item;
        try
        {
            item = _library.GetItemById(itemId);
        }
        catch (Exception)
        {
            return new CatalogItem { Presence = ItemPresence.Unreadable };
        }

        if (item is null)
        {
            return new CatalogItem { Presence = ItemPresence.Missing };
        }

        Guid libraryId;
        try
        {
            libraryId = _library.GetCollectionFolders(item).Select(folder => folder.Id).FirstOrDefault(enabledLibraryIds.Contains);
        }
        catch (Exception)
        {
            return new CatalogItem { Presence = ItemPresence.Unreadable };
        }

        if (libraryId == Guid.Empty)
        {
            return new CatalogItem { Presence = ItemPresence.OutOfScope };
        }

        var videos = new List<IndexableVideo>();
        var unreadableItems = new List<Guid>();
        var unreadableSources = new List<string>();
        ReadItemSources(item, libraryId, videos, unreadableItems, unreadableSources);
        if (videos.Count > 0)
        {
            return new CatalogItem { Presence = ItemPresence.Readable, Video = videos[0], Videos = videos, UnreadableSourceIds = unreadableSources };
        }

        return new CatalogItem
        {
            Presence = unreadableItems.Count > 0 ? ItemPresence.Unreadable : ItemPresence.Missing,
            UnreadableSourceIds = unreadableSources
        };
    }

    public CatalogItem ReadSource(Guid itemId, string mediaSourceId, IReadOnlyCollection<Guid> enabledLibraryIds)
    {
        var item = ReadItem(itemId, enabledLibraryIds);
        if (item.Presence == ItemPresence.Unreadable || item.UnreadableSourceIds.Contains(mediaSourceId))
        {
            return new CatalogItem { Presence = ItemPresence.Unreadable, UnreadableSourceIds = item.UnreadableSourceIds };
        }

        if (item.Presence != ItemPresence.Readable)
        {
            return item;
        }

        var match = item.Videos.FirstOrDefault(video => video.MediaSourceId == mediaSourceId);
        if (match is null)
        {
            return new CatalogItem { Presence = ItemPresence.Missing };
        }

        return new CatalogItem { Presence = ItemPresence.Readable, Video = match, Videos = [match] };
    }

    private static void ReadItemSources(BaseItem item, Guid libraryId, List<IndexableVideo> videos, List<Guid> unreadableItems, List<string>? unreadableSources)
    {
        IReadOnlyList<MediaBrowser.Model.Dto.MediaSourceInfo> sources;
        try
        {
            sources = item.GetMediaSources(false);
        }
        catch (Exception)
        {
            unreadableItems.Add(item.Id);
            unreadableSources?.Add(item.Id.ToString("N"));
            return;
        }

        var added = false;
        foreach (var source in sources)
        {
            if (source.Protocol != MediaProtocol.File || source.IsInfiniteStream || string.IsNullOrWhiteSpace(source.Path))
            {
                continue;
            }

            var sourceId = source.Id ?? item.Id.ToString("N");
            if (!File.Exists(source.Path))
            {
                unreadableItems.Add(item.Id);
                unreadableSources?.Add(sourceId);
                continue;
            }

            var file = MediaFileFacts.Resolve(source.Path);
            var runtime = source.RunTimeTicks ?? item.RunTimeTicks ?? 0;
            var audio = source.GetDefaultAudioStream(source.DefaultAudioStreamIndex);
            videos.Add(new IndexableVideo
            {
                ItemId = item.Id,
                MediaSourceId = sourceId,
                LibraryId = libraryId,
                RuntimeTicks = runtime,
                Container = source.Container ?? "",
                ContentVersion = ContentVersion.FromMediaFacts(file.Length, runtime, new DateTimeOffset(file.LastWriteTimeUtc, TimeSpan.Zero), source.Container ?? "", audio?.Index ?? -1),
                SourcePath = source.Path,
                HasAudio = audio is not null,
                AudioStreamIndex = audio?.Index
            });
            added = true;
        }

        if (!added && unreadableItems.Count == 0)
        {
            unreadableItems.Add(item.Id);
            unreadableSources?.Add(item.Id.ToString("N"));
        }
    }
}

public sealed class JellyfinLibrarySignals : IDisposable
{
    private readonly ILibraryManager _library;
    private readonly IndexCoordinator _coordinator;
    private bool _started;

    public JellyfinLibrarySignals(ILibraryManager library, IndexCoordinator coordinator)
    {
        _library = library;
        _coordinator = coordinator;
    }

    public void Start()
    {
        if (_started)
        {
            return;
        }

        _library.ItemAdded += OnChanged;
        _library.ItemUpdated += OnChanged;
        _library.ItemRemoved += OnRemoved;
        _started = true;
    }

    public void Dispose()
    {
        if (!_started)
        {
            return;
        }

        _library.ItemAdded -= OnChanged;
        _library.ItemUpdated -= OnChanged;
        _library.ItemRemoved -= OnRemoved;
        _started = false;
    }

    private void OnChanged(object? sender, ItemChangeEventArgs args)
    {
        if (args.Item is null)
        {
            return;
        }

        try
        {
            _coordinator.Enqueue("updated", args.Item.Id);
        }
        catch (Exception)
        {
        }
    }

    private void OnRemoved(object? sender, ItemChangeEventArgs args)
    {
        if (args.Item is null)
        {
            return;
        }

        try
        {
            _coordinator.Enqueue("removed", args.Item.Id);
        }
        catch (Exception)
        {
        }
    }
}
