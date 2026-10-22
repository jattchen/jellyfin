namespace Jellyfin.Plugin.EmbeddingVideoSearch;

/// <summary>Stable values shared by the HTTP contract and the index.</summary>
public static class ApiV1
{
    public const string Version = "1";
    public const string Route = "EmbeddingVideoSearch/v1";
    public const string AdminRoute = "EmbeddingVideoSearch/v1/admin";
    public const long TicksPerSecond = 10_000_000L;
    public const long WindowTicks = 6 * TicksPerSecond;
    public const long StepTicks = 4 * TicksPerSecond;
    public const long TenSecondWindowTicks = 10 * TicksPerSecond;
    public const long TenSecondStepTicks = 8 * TicksPerSecond;
    public const long TwelveSecondWindowTicks = 12 * TicksPerSecond;
    public const long TwelveSecondStepTicks = 10 * TicksPerSecond;
    public const int Dimension = 768;
    public const string ModelId = "google/embeddinggemma-2";
    public const string Revision = "914f7f89142e33e77833254d9c9b90c3cef7303b";
    public const string InputVersion = FfmpegWindowClipper.InputVersion;
    public const string WindowVersion = "6s-4s-tail-v1";
    public const string TenSecondWindowVersion = "10s-8s-5f70-tail-v1";
    public const string TwelveSecondWindowVersion = "12s-10s-4f70-tail-v1";
    public const string TwelveSecondProfile = "jellyfin-12s-4f";
    public const string AudioVersion = FfmpegWindowClipper.AudioVersion;
    public const double DefaultRelatedMinimum = 0.35;
    public const int DefaultLimit = 24;
    public const int MaxLimit = 50;
    public const int MaxGroupedVideos = 3;
    public const int GroupedHitsPerVideo = 5;
    public const string GroupedVideoFeature = "grouped-video-v1";
    public const string CollapsedRunsFeature = "collapsed-runs-v1";
    public const string SimilarSegmentsFeature = "similar-segments-v1";
    public const string DocumentCosineScoreMeaning = "documentCosine";
    public const string UserIdClaim = "Jellyfin-UserId";

    public static readonly Guid PluginId = Guid.Parse("8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17");

    public static string IndexDirectory(string dataPath)
        => Path.Combine(dataPath, "embedding-video-search", PluginId.ToString("D"));
}
