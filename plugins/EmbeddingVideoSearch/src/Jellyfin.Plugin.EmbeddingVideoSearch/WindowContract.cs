namespace Jellyfin.Plugin.EmbeddingVideoSearch;

/// <summary>
/// The accepted 6-second contract stays on <see cref="ApiV1.WindowVersion"/>.
/// The 10-second profile is a second index version for a new host. It does not replace the 6-second planner.
/// </summary>
public static class WindowContract
{
    public static readonly int[] TenSecondSamples = [0, 2, 4, 6, 8];

    public static bool IsAcceptedIndexVersion(string? version)
        => version == ApiV1.WindowVersion || version == ApiV1.TenSecondWindowVersion;

    public static void Apply(CompatibilityBody compatibility, WindowContractBody window, string? storedVersion)
    {
        if (storedVersion == ApiV1.TenSecondWindowVersion)
        {
            compatibility.WindowVersion = ApiV1.TenSecondWindowVersion;
            window.LengthTicks = ApiV1.TenSecondWindowTicks;
            window.StepTicks = ApiV1.TenSecondStepTicks;
            return;
        }

        compatibility.WindowVersion = ApiV1.WindowVersion;
        window.LengthTicks = ApiV1.WindowTicks;
        window.StepTicks = ApiV1.StepTicks;
    }

    public static int Milliseconds(double seconds)
        => (int)Math.Round(seconds * 1000d, MidpointRounding.ToEven);

    public static bool ClipDurationAccepted(double seconds)
    {
        if (double.IsNaN(seconds) || double.IsInfinity(seconds))
        {
            return false;
        }

        var delta = Math.Abs(Milliseconds(seconds) - 10_000);
        return delta <= 150 && TenSecondSamples.All(offset => Milliseconds(seconds) > offset * 1000);
    }

    public static bool AudioCoversClip(double audioSeconds, double clipSeconds)
    {
        if (double.IsNaN(audioSeconds) || double.IsInfinity(audioSeconds) || double.IsNaN(clipSeconds) || double.IsInfinity(clipSeconds))
        {
            return false;
        }

        return Milliseconds(clipSeconds) - Milliseconds(audioSeconds) <= 150;
    }

    public static bool TenSecondJoint(WindowProcessing processing)
    {
        if (processing.FpsDeclared
            || processing.AudioIncluded != true
            || processing.HasAudio != true
            || processing.VideoDurationSeconds is not double video
            || processing.AudioDurationSeconds is not double audio
            || !ClipDurationAccepted(video)
            || !AudioCoversClip(audio, video)
            || processing.SampledSeconds is null
            || !processing.SampledSeconds.SequenceEqual(TenSecondSamples)
            || processing.FrameCount != 5
            || processing.MaxSoftTokens != 70
            || processing.AddTimestamps != false
            || processing.Profile != "jellyfin-10s-5f"
            || processing.OverflowStrategy != "reject"
            || processing.OffsetSeconds is null
            || !processing.OffsetSeconds.SequenceEqual(TenSecondSamples)
            || processing.WindowSeconds != 10
            || processing.SampleRate != 16000
            || processing.Channels != 1
            || processing.AudioTruncated != false
            || processing.AudioCoverage != "full_track")
        {
            return false;
        }

        return true;
    }
}
