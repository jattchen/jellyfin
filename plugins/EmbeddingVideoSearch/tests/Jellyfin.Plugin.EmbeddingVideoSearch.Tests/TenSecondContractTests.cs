using System.Text.Json;
using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class TenSecondContractTests
{
    [Fact]
    public void Six_second_plan_still_uses_two_windows_on_a_ten_second_film()
    {
        Assert.Equal("6s-4s-tail-v1", ApiV1.WindowVersion);
        var ten = WindowPlanner.Plan(TimeSpan.FromSeconds(10).Ticks);
        Assert.Equal(2, ten.Count);
        Assert.Equal(0, ten[0].StartTicks);
        Assert.Equal(ApiV1.WindowTicks, ten[0].EndTicks);
        Assert.Equal(ApiV1.StepTicks, ten[1].StartTicks);
        Assert.Equal(TimeSpan.FromSeconds(10).Ticks, ten[1].EndTicks);
        Assert.Equal(ApiV1.WindowTicks, ten[1].EndTicks - ten[1].StartTicks);
    }

    [Fact]
    public void Ten_second_windows_follow_runtime_ticks_and_keep_a_full_tail()
    {
        AssertCounts(12_088_100_000, 151);
        AssertCounts(11_567_786_670, 145);
        AssertCounts(17_101_752_380, 214);
        var tail = WindowPlanner.Plan(17_101_752_380, ApiV1.TenSecondWindowTicks, ApiV1.TenSecondStepTicks);
        Assert.Equal(17_101_752_380, tail[^1].EndTicks);
        Assert.Equal(ApiV1.TenSecondWindowTicks, tail[^1].EndTicks - tail[^1].StartTicks);
        Assert.Equal(17_001_752_380, tail[^1].StartTicks);
        Assert.True(tail[^1].StartTicks <= tail[^2].EndTicks);
    }

    [Fact]
    public void Unknown_window_version_does_not_match_and_ten_second_requires_the_same_processing_id()
    {
        var live = Ready("from-live-service");
        Assert.True(IndexStore.SignaturesMatch(Stored(ApiV1.WindowVersion, "from-live-service"), live));
        Assert.True(IndexStore.SignaturesMatch(Stored(ApiV1.TenSecondWindowVersion, "from-live-service"), live));
        Assert.False(IndexStore.SignaturesMatch(Stored(ApiV1.TenSecondWindowVersion, "old-6s-service"), live));
        Assert.False(IndexStore.SignaturesMatch(Stored("10s", "from-live-service"), live));
        Assert.False(IndexStore.SignaturesMatch(Stored("6s-4s-tail-v2", "from-live-service"), live));
    }

    [Fact]
    public void Capabilities_keep_the_six_second_contract_unless_the_stored_index_is_ten_second()
    {
        var compatibility = new CompatibilityBody();
        var window = new WindowContractBody();
        WindowContract.Apply(compatibility, window, null);
        Assert.Equal(ApiV1.WindowVersion, compatibility.WindowVersion);
        Assert.Equal(ApiV1.WindowTicks, window.LengthTicks);
        Assert.Equal(ApiV1.StepTicks, window.StepTicks);

        WindowContract.Apply(compatibility, window, ApiV1.TenSecondWindowVersion);
        Assert.Equal(ApiV1.TenSecondWindowVersion, compatibility.WindowVersion);
        Assert.Equal(ApiV1.TenSecondWindowTicks, window.LengthTicks);
        Assert.Equal(ApiV1.TenSecondStepTicks, window.StepTicks);
    }

    [Fact]
    public void Ten_second_joint_acceptance_uses_the_service_metadata_and_not_fps()
    {
        var accepted = HttpEmbeddingClient.ParseEmbedding(Sample(new[] { 0, 2, 4, 6, 8 }, 10, 10.05));
        Assert.True(accepted.Success);
        Assert.False(accepted.Processing!.FpsDeclared);
        Assert.True(WindowContract.TenSecondJoint(accepted.Processing));

        var longerAudio = HttpEmbeddingClient.ParseEmbedding(Sample(new[] { 0, 2, 4, 6, 8 }, 10, 10.2));
        Assert.True(WindowContract.TenSecondJoint(longerAudio.Processing!));

        var shortAudio = HttpEmbeddingClient.ParseEmbedding(Sample(new[] { 0, 2, 4, 6, 8 }, 10, 9.8));
        Assert.False(WindowContract.TenSecondJoint(shortAudio.Processing!));

        var resampled = HttpEmbeddingClient.ParseEmbedding(Sample(Enumerable.Range(0, 10).ToArray(), 10, 10));
        Assert.False(WindowContract.TenSecondJoint(resampled.Processing!));

        var withFps = HttpEmbeddingClient.ParseEmbedding(Sample(new[] { 0, 2, 4, 6, 8 }, 10, 10, fps: 1));
        Assert.True(withFps.Processing!.FpsDeclared);
        Assert.False(WindowContract.TenSecondJoint(withFps.Processing));
    }

    private static void AssertCounts(long runtimeTicks, int count)
    {
        var windows = WindowPlanner.Plan(runtimeTicks, ApiV1.TenSecondWindowTicks, ApiV1.TenSecondStepTicks);
        Assert.Equal(count, windows.Count);
        Assert.Equal(0, windows[0].StartTicks);
        Assert.Equal(runtimeTicks, windows[^1].EndTicks);
        Assert.All(windows, span => Assert.Equal(ApiV1.TenSecondWindowTicks, span.EndTicks - span.StartTicks));
        Assert.True(WindowPlanner.Covers(runtimeTicks, windows));
    }

    private static StoredSignature Stored(string windowVersion, string processingId) => new()
    {
        Generation = 1,
        Model = ApiV1.ModelId,
        Revision = ApiV1.Revision,
        ProcessingId = processingId,
        Dimension = ApiV1.Dimension,
        Normalized = true,
        InputVersion = ApiV1.InputVersion,
        WindowVersion = windowVersion,
        AudioVersion = ApiV1.AudioVersion
    };

    private static ModelObservation Ready(string processingId) => new()
    {
        Reachable = true,
        Ready = true,
        Model = ApiV1.ModelId,
        Revision = ApiV1.Revision,
        ProcessingId = processingId,
        Dimension = ApiV1.Dimension,
        Normalized = true
    };

    private static string Sample(int[] seconds, double videoSeconds, double audioSeconds, int? fps = null)
    {
        var vector = new float[ApiV1.Dimension];
        vector[0] = 1f;
        var video = new Dictionary<string, object?>
        {
            ["duration_seconds"] = videoSeconds,
            ["audio_included"] = true,
            ["has_audio"] = true,
            ["sampled_seconds"] = seconds,
            ["frame_count"] = seconds.Length,
            ["profile"] = "jellyfin-10s-5f",
            ["offsets_seconds"] = new[] { 0, 2, 4, 6, 8 },
            ["window_seconds"] = 10,
            ["max_soft_tokens"] = 70,
            ["add_timestamps"] = false,
            ["overflow_strategy"] = "reject"
        };
        if (fps is int declared)
        {
            video["fps"] = declared;
        }

        return JsonSerializer.Serialize(new
        {
            model = ApiV1.ModelId,
            revision = ApiV1.Revision,
            processing_id = "from-live-service",
            dimension = ApiV1.Dimension,
            normalized = true,
            data = new[]
            {
                new
                {
                    index = 0,
                    embedding = vector,
                    processing = new
                    {
                        video,
                        audio = new
                        {
                            duration_seconds = audioSeconds,
                            sample_rate = 16000,
                            channels = 1,
                            truncated = false,
                            coverage = "full_track"
                        }
                    }
                }
            }
        });
    }
}
