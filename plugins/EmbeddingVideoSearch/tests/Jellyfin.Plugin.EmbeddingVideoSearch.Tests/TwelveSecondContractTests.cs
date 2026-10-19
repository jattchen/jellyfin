using System.Text.Json;
using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class TwelveSecondContractTests
{
    [Fact]
    public void Twelve_second_plan_has_twelve_windows_on_a_two_minute_span()
    {
        var local = WindowPlanner.Plan(1_200_000_000, ApiV1.TwelveSecondWindowTicks, ApiV1.TwelveSecondStepTicks);
        Assert.Equal(12, local.Count);
        Assert.Equal(0, local[0].StartTicks);
        Assert.Equal(ApiV1.TwelveSecondWindowTicks, local[0].EndTicks);
        Assert.Equal(1_080_000_000, local[^1].StartTicks);
        Assert.Equal(1_200_000_000, local[^1].EndTicks);
        Assert.True(local[^1].StartTicks <= local[^2].EndTicks);
    }

    [Fact]
    public void Stored_twelve_second_index_reports_its_own_window_and_processing_id()
    {
        var live = Ready("eg2-7c43611ad2e20661c3d3");
        Assert.True(IndexStore.SignaturesMatch(Stored(ApiV1.TwelveSecondWindowVersion, "eg2-7c43611ad2e20661c3d3"), live));
        Assert.False(IndexStore.SignaturesMatch(Stored(ApiV1.TwelveSecondWindowVersion, "from-10s-service"), live));
        Assert.False(IndexStore.SignaturesMatch(Stored(ApiV1.TenSecondWindowVersion, "eg2-0b01d27c8c20b7d920e5"), live));

        var compatibility = new CompatibilityBody();
        var window = new WindowContractBody();
        WindowContract.Apply(compatibility, window, ApiV1.TwelveSecondWindowVersion);
        Assert.Equal(ApiV1.TwelveSecondWindowVersion, compatibility.WindowVersion);
        Assert.Equal(ApiV1.TwelveSecondWindowTicks, window.LengthTicks);
        Assert.Equal(ApiV1.TwelveSecondStepTicks, window.StepTicks);
    }

    [Fact]
    public void Twelve_second_joint_accepts_the_observed_service_metadata()
    {
        var accepted = HttpEmbeddingClient.ParseEmbedding(Sample(12.012, 12.0));
        Assert.True(accepted.Success);
        Assert.False(accepted.Processing!.FpsDeclared);
        Assert.True(WindowContract.TwelveSecondJoint(accepted.Processing));

        var shortAudio = HttpEmbeddingClient.ParseEmbedding(Sample(12.012, 11.8));
        Assert.False(WindowContract.TwelveSecondJoint(shortAudio.Processing!));

        var withFps = HttpEmbeddingClient.ParseEmbedding(Sample(12.012, 12.0, fps: 1));
        Assert.False(WindowContract.TwelveSecondJoint(withFps.Processing!));
    }

    [Fact]
    public void Checked_empty_text_is_valid_and_an_unchecked_empty_channel_is_not()
    {
        var checkedEmpty = Representation("text", transcriptionChecked: true);
        checkedEmpty.Validate();

        var uncheckedEmpty = Representation("text", transcriptionChecked: false);
        Assert.Throws<InvalidDataException>(() => uncheckedEmpty.Validate());

        var emptyJoint = Representation("joint", transcriptionChecked: true);
        Assert.Throws<InvalidDataException>(() => emptyJoint.Validate());
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

    private static CandidateRepresentation Representation(string channel, bool transcriptionChecked) => new()
    {
        Channel = channel,
        Version = "v",
        Model = ApiV1.ModelId,
        Revision = ApiV1.Revision,
        ProcessingId = "eg2-7c43611ad2e20661c3d3",
        Dimension = ApiV1.Dimension,
        Normalized = true,
        InputVersion = channel == "joint" ? ApiV1.InputVersion : "transcript-document-cache-v1",
        EmbeddingTask = channel == "joint" ? "video_audio" : "document",
        ContentVersion = "content",
        SegmentationVersion = "segmentation",
        ReferenceVersion = "reference",
        ReferenceQueryVersion = "queries",
        Algorithm = CandidateRanking.Version,
        Scale = 0,
        Background = [],
        Candidates = [],
        TranscriptionChecked = transcriptionChecked
    };

    private static string Sample(double videoSeconds, double audioSeconds, int? fps = null)
    {
        var vector = new float[ApiV1.Dimension];
        vector[0] = 1f;
        var video = new Dictionary<string, object?>
        {
            ["duration_seconds"] = videoSeconds,
            ["audio_included"] = true,
            ["has_audio"] = true,
            ["sampled_seconds"] = new[] { 0, 3, 6, 9 },
            ["frame_count"] = 4,
            ["profile"] = ApiV1.TwelveSecondProfile,
            ["offsets_seconds"] = new[] { 0, 3, 6, 9 },
            ["window_seconds"] = 12,
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
            processing_id = "eg2-7c43611ad2e20661c3d3",
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
                        modalities = new[] { "video", "audio" },
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
