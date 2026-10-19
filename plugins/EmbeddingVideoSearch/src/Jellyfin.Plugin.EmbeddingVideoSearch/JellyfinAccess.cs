using Jellyfin.Data;
using Jellyfin.Database.Implementations.Enums;
using MediaBrowser.Controller.Entities;
using MediaBrowser.Controller.Library;
using MediaBrowser.Model.Entities;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed class JellyfinLibraryCatalog : ILibraryCatalog
{
    private static readonly HashSet<CollectionTypeOptions> VideoCollections =
    [
        CollectionTypeOptions.movies,
        CollectionTypeOptions.tvshows,
        CollectionTypeOptions.homevideos,
        CollectionTypeOptions.musicvideos,
        CollectionTypeOptions.mixed
    ];

    private readonly ILibraryManager _library;

    public JellyfinLibraryCatalog(ILibraryManager library)
    {
        _library = library;
    }

    public IReadOnlyList<LibraryListing> ListLibraries()
    {
        var listings = new List<LibraryListing>();
        foreach (var folder in _library.GetVirtualFolders())
        {
            if (folder.CollectionType is { } kind && !VideoCollections.Contains(kind))
            {
                continue;
            }

            if (!Guid.TryParse(folder.ItemId, out var id))
            {
                continue;
            }

            listings.Add(new LibraryListing { Id = id, Name = folder.Name ?? "" });
        }

        return listings;
    }

    public ParentBinding ResolveParent(Guid parentId)
    {
        if (ListLibraries().Any(library => library.Id == parentId))
        {
            return new ParentBinding(true, true);
        }

        BaseItem? item;
        try
        {
            item = _library.GetItemById(parentId);
        }
        catch (Exception)
        {
            return new ParentBinding(false, false);
        }

        if (item is null)
        {
            return new ParentBinding(false, false);
        }

        return new ParentBinding(true, item is CollectionFolder);
    }

    public bool ItemBelongsToParent(Guid itemId, Guid parentId)
    {
        BaseItem? item;
        try
        {
            item = _library.GetItemById(itemId);
        }
        catch (Exception)
        {
            return false;
        }

        if (item is null)
        {
            return false;
        }

        if (item.Id == parentId || item.GetParents().Any(parent => parent.Id == parentId))
        {
            return true;
        }

        try
        {
            return _library.GetCollectionFolders(item).Any(folder => folder.Id == parentId);
        }
        catch (Exception)
        {
            return false;
        }
    }
}

public sealed class JellyfinMediaAccess : IMediaAccess
{
    private readonly IUserManager _users;
    private readonly ILibraryManager _library;
    private readonly Func<IReadOnlyCollection<Guid>> _enabledLibraries;

    public JellyfinMediaAccess(IUserManager users, ILibraryManager library, Func<IReadOnlyCollection<Guid>> enabledLibraries)
    {
        _users = users;
        _library = library;
        _enabledLibraries = enabledLibraries;
    }

    public bool TryGetUser(Guid userId, out MediaPrincipal? principal)
    {
        principal = null;
        if (userId == Guid.Empty)
        {
            return false;
        }

        var user = _users.GetUserById(userId);
        if (user is null)
        {
            return false;
        }

        var disabled = user.HasPermission(PermissionKind.IsDisabled);
        var admin = user.HasPermission(PermissionKind.IsAdministrator);
        principal = new MediaPrincipal(
            admin,
            disabled,
            libraryId => CanSeeLibrary(user, libraryId),
            (itemId, mediaSourceId) => CanAccess(user, itemId, mediaSourceId));
        return true;
    }

    private bool CanSeeLibrary(Jellyfin.Database.Implementations.Entities.User user, Guid libraryId)
        => _library.GetItemById<Folder>(libraryId, user) is not null;

    private bool CanAccess(Jellyfin.Database.Implementations.Entities.User user, Guid itemId, string mediaSourceId)
    {
        if (string.IsNullOrWhiteSpace(mediaSourceId))
        {
            return false;
        }

        var item = _library.GetItemById<BaseItem>(itemId, user);
        if (item is null)
        {
            return false;
        }

        IReadOnlyList<MediaBrowser.Model.Dto.MediaSourceInfo> sources;
        try
        {
            sources = item.GetMediaSources(false);
        }
        catch (Exception)
        {
            return false;
        }

        if (!sources.Any(source => string.Equals(source.Id, mediaSourceId, StringComparison.Ordinal)))
        {
            return false;
        }

        var enabled = _enabledLibraries().ToHashSet();
        List<Folder> folders;
        try
        {
            folders = _library.GetCollectionFolders(item);
        }
        catch (Exception)
        {
            return false;
        }

        return folders.Any(folder => enabled.Contains(folder.Id) && CanSeeLibrary(user, folder.Id));
    }
}
