namespace Jellyfin.Plugin.EmbeddingVideoSearch;

internal static class MediaFileFacts
{
    // Fixture and library paths can be symlinks. FileInfo timestamps describe the link on macOS;
    // source validity must follow the actual media, as File.Exists and the player do.
    internal static FileInfo Resolve(string path)
    {
        var file = new FileInfo(path);
        return file.LinkTarget is null ? file : file.ResolveLinkTarget(true) as FileInfo
            ?? throw new IOException("Media link does not resolve to a regular file.");
    }
}
