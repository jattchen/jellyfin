using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class MediaFileFactsTests
{
    [Fact]
    public void Symlink_binding_uses_target_facts_and_detects_same_size_target_replacement()
    {
        var directory = Path.Combine(Path.GetTempPath(), "evs-source-facts-" + Guid.NewGuid());
        Directory.CreateDirectory(directory);
        try
        {
            var target = Path.Combine(directory, "target.mp4");
            var link = Path.Combine(directory, "library.mp4");
            File.WriteAllText(target, "original");
            File.SetLastWriteTimeUtc(target, new DateTime(2025, 1, 1, 0, 0, 0, DateTimeKind.Utc));
            File.CreateSymbolicLink(link, target);
            var resolved = MediaFileFacts.Resolve(link);
            var original = ContentVersion.FromMediaFacts(resolved.Length, 100, new DateTimeOffset(resolved.LastWriteTimeUtc), "mp4", 1);
            Assert.Equal(new FileInfo(target).LastWriteTimeUtc, resolved.LastWriteTimeUtc);
            Assert.Equal(new FileInfo(target).Length, resolved.Length);
            File.WriteAllText(target, "modified");
            File.SetLastWriteTimeUtc(target, new DateTime(2025, 1, 1, 0, 0, 5, DateTimeKind.Utc));
            var replaced = MediaFileFacts.Resolve(link);
            Assert.NotEqual(original, ContentVersion.FromMediaFacts(replaced.Length, 100, new DateTimeOffset(replaced.LastWriteTimeUtc), "mp4", 1));
        }
        finally { Directory.Delete(directory, true); }
    }
}
