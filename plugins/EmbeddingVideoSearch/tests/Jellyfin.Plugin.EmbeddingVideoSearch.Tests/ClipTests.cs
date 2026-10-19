using Xunit;

namespace Jellyfin.Plugin.EmbeddingVideoSearch.Tests;

public class ClipTests
{
    [Fact]
    public async Task Input_seek_lands_on_the_requested_picture_and_tone()
    {
        var ffmpeg = Tool("ffmpeg");
        var ffprobe = Tool("ffprobe");
        var root = Directory.CreateTempSubdirectory("evs-seek-").FullName;
        var source = Path.Combine(root, "source.mp4");
        await Run(ffmpeg, ["-y", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=10:d=3.5", "-f", "lavfi", "-i", "color=c=red:s=160x120:r=10:d=4.5", "-f", "lavfi", "-i", "sine=frequency=440:duration=3.5:sample_rate=16000", "-f", "lavfi", "-i", "sine=frequency=880:duration=4.5:sample_rate=16000", "-filter_complex", "[0:v][2:a][1:v][3:a]concat=n=2:v=1:a=1[v][a]", "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", source]);
        var args = FfmpegWindowClipper.BuildArguments(source, 4, 2, Path.Combine(root, "clip.mp4"), 1);
        var inputAt = Array.IndexOf(args, "-i");
        Assert.True(Array.IndexOf(args, "-ss") < inputAt);
        Assert.True(Array.IndexOf(args, "-accurate_seek") < inputAt);
        Assert.True(Array.IndexOf(args, "-t") > inputAt);
        Assert.Contains(FfmpegWindowClipper.VideoPreset, args);
        Assert.Contains(FfmpegWindowClipper.VideoCrf, args);
        Assert.Contains(FfmpegWindowClipper.AudioSampleRate, args);
        Assert.DoesNotContain(args, argument => argument == "copy");
        Assert.Equal(ApiV1.InputVersion, FfmpegWindowClipper.InputVersion);
        Assert.Equal("source-default-audio-index-v1", ApiV1.AudioVersion);

        var clipper = new FfmpegWindowClipper(ffmpeg, ffprobe);
        var clip = await clipper.ClipAsync(Lease(source, 4, 2, true, 1), Path.Combine(root, "window.mp4"), CancellationToken.None);
        Assert.True(clip.Accepted);
        var frame = await Dump(ffmpeg, Path.Combine(root, "window.mp4"), Path.Combine(root, "frame.rgb"), ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24"]);
        var red = 0;
        var green = 0;
        for (var i = 0; i + 2 < frame.Length; i += 3)
        {
            red += frame[i];
            green += frame[i + 1];
        }

        Assert.True(red > green);
        var pcm = await Dump(ffmpeg, Path.Combine(root, "window.mp4"), Path.Combine(root, "tone.pcm"), ["-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-t", "0.5"]);
        var samples = new short[pcm.Length / 2];
        Buffer.BlockCopy(pcm, 0, samples, 0, samples.Length * 2);
        Assert.True(Tone(samples, 880, 8000) > Tone(samples, 440, 8000));
    }

    [Fact]
    public async Task Short_offset_and_tail_windows_are_refused()
    {
        var ffmpeg = Tool("ffmpeg");
        var ffprobe = Tool("ffprobe");
        var root = Directory.CreateTempSubdirectory("evs-bad-window-").FullName;
        var clipper = new FfmpegWindowClipper(ffmpeg, ffprobe);
        await Run(ffmpeg, ["-y", "-f", "lavfi", "-i", "testsrc=duration=6:size=160x120:rate=10", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", Path.Combine(root, "short-audio.mp4")]);
        await Run(ffmpeg, ["-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", Path.Combine(root, "short-video.mp4")]);
        await Run(ffmpeg, ["-y", "-f", "lavfi", "-i", "testsrc=duration=6:size=160x120:rate=10", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", Path.Combine(root, "tail.mp4")]);
        await Run(ffmpeg, ["-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-output_ts_offset", "1.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", Path.Combine(root, "offset.mp4")]);

        var shortAudio = await clipper.ClipAsync(Lease(Path.Combine(root, "short-audio.mp4"), 0, 6, true, 1), Path.Combine(root, "out-audio.mp4"), CancellationToken.None);
        var shortVideo = await clipper.ClipAsync(Lease(Path.Combine(root, "short-video.mp4"), 0, 6, true, 1), Path.Combine(root, "out-video.mp4"), CancellationToken.None);
        var tail = await clipper.ClipAsync(Lease(Path.Combine(root, "tail.mp4"), 5.5, 6, true, 1), Path.Combine(root, "out-tail.mp4"), CancellationToken.None);
        var offset = await clipper.InspectAsync(Path.Combine(root, "offset.mp4"), 2, true, 0, CancellationToken.None);
        Assert.False(shortAudio.Accepted);
        Assert.False(shortVideo.Accepted);
        Assert.False(tail.Accepted);
        Assert.False(offset.Accepted);
        Assert.Equal("clip_mismatch", offset.ErrorCode);
    }

    [Fact]
    public void Missing_probe_fields_are_not_treated_as_zero()
    {
        var missing = FfmpegWindowClipper.Evaluate(
            """{"streams":[{"codec_type":"video","avg_frame_rate":"10/1"},{"codec_type":"audio","sample_rate":"16000"}]}""",
            0,
            true,
            0);
        Assert.False(missing.Accepted);
        Assert.Equal("probe_unknown", missing.ErrorCode);

        var early = FfmpegWindowClipper.Evaluate(
            """{"streams":[{"codec_type":"video","start_time":"-0.5","duration":"6","avg_frame_rate":"24/1"},{"codec_type":"audio","start_time":"0","duration":"6","sample_rate":"16000"}]}""",
            6,
            true,
            0);
        Assert.False(early.Accepted);
        Assert.Equal("clip_mismatch", early.ErrorCode);

        var old = new StoredSignature
        {
            Model = ApiV1.ModelId,
            Revision = ApiV1.Revision,
            ProcessingId = "proc-a",
            Dimension = ApiV1.Dimension,
            Normalized = true,
            InputVersion = "video-joint-v1",
            WindowVersion = ApiV1.WindowVersion,
            AudioVersion = "default-track-v1"
        };
        Assert.False(IndexStore.SignaturesMatch(old, Fixture.Model("proc-a")));
    }

    [Fact]
    public async Task Encoder_path_is_read_again_when_a_window_is_dispatched()
    {
        var ffmpeg = Tool("ffmpeg");
        string? hostPath = null;
        var clipper = new FfmpegWindowClipper(() => hostPath);
        var root = Directory.CreateTempSubdirectory("evs-late-ffmpeg-").FullName;
        var source = Path.Combine(root, "source.mp4");
        await Run(ffmpeg, ["-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", source]);
        var output = Path.Combine(root, "window.mp4");
        var early = await clipper.ClipAsync(Lease(source, 0, 1, true, 1), output, CancellationToken.None);
        Assert.Equal("ffmpeg_unavailable", early.ErrorCode);
        Assert.False(File.Exists(output));

        hostPath = ffmpeg;
        var late = await clipper.ClipAsync(Lease(source, 0, 1, true, 1), output, CancellationToken.None);
        Assert.True(late.Accepted);
        Assert.Equal(ffmpeg, hostPath);
        Assert.Equal(ffmpeg, Path.GetDirectoryName(ffmpeg) is { } directory && File.Exists(Path.Combine(directory, "ffprobe")) ? hostPath : null);
    }

    [Fact]
    public async Task A_full_stderr_pipe_returns_and_cancel_kills_the_process()
    {
        var filled = await CapturedProcess.RunAsync("/usr/bin/perl", ["-e", "print STDERR \"x\" x 300000"], CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(5), TestContext.Current.CancellationToken);
        Assert.Equal(0, filled.ExitCode);
        Assert.InRange(filled.StandardError.Length, 1, FfmpegWindowClipper.MaxCapturedChars);

        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        var started = DateTime.UtcNow;
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => CapturedProcess.RunAsync("/bin/sleep", ["30"], cancel.Token));
        Assert.True(DateTime.UtcNow - started < TimeSpan.FromSeconds(3));

        var ffmpeg = Tool("ffmpeg");
        var ffprobe = Tool("ffprobe");
        using var cancelEncode = new CancellationTokenSource(TimeSpan.FromMilliseconds(400));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => CapturedProcess.RunAsync(ffmpeg, ["-hide_banner", "-loglevel", "error", "-nostats", "-re", "-f", "lavfi", "-i", "testsrc=duration=30:size=32x32:rate=10", "-f", "null", "/dev/null"], cancelEncode.Token));
        var root = Directory.CreateTempSubdirectory("evs-probe-").FullName;
        var media = Path.Combine(root, "tiny.mp4");
        await Run(ffmpeg, ["-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=8", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", media]);
        var probed = await CapturedProcess.RunAsync(ffprobe, ["-v", "error", "-show_entries", "stream=codec_type", "-of", "json", media], CancellationToken.None);
        Assert.Equal(0, probed.ExitCode);
        Assert.Contains("video", probed.StandardOutput);
    }

    private static LeaseTicket Lease(string path, double start, double duration, bool audio, int? index)
        => new()
        {
            Token = "clip",
            SourcePath = path,
            StartTicks = (long)(start * ApiV1.TicksPerSecond),
            EndTicks = (long)((start + duration) * ApiV1.TicksPerSecond),
            HasAudio = audio,
            AudioStreamIndex = index
        };

    private static async Task<byte[]> Dump(string ffmpeg, string path, string output, IReadOnlyList<string> tail)
    {
        var args = new List<string> { "-y", "-v", "error", "-i", path };
        args.AddRange(tail);
        args.Add(output);
        await Run(ffmpeg, args);
        return File.ReadAllBytes(output);
    }

    private static double Tone(IReadOnlyList<short> samples, double frequency, double rate)
    {
        var coefficient = 2 * Math.Cos(2 * Math.PI * frequency / rate);
        double previous = 0;
        double older = 0;
        foreach (var sample in samples)
        {
            var next = sample + coefficient * previous - older;
            older = previous;
            previous = next;
        }

        return previous * previous + older * older - coefficient * previous * older;
    }

    private static string Tool(string name)
    {
        foreach (var candidate in new[] { "/opt/homebrew/bin/" + name, "/usr/local/bin/" + name, "/usr/bin/" + name })
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        throw new InvalidOperationException(name + " was not found.");
    }

    private static async Task Run(string fileName, IReadOnlyList<string> arguments)
    {
        var ran = await CapturedProcess.RunAsync(fileName, arguments, CancellationToken.None);
        Assert.Equal(0, ran.ExitCode);
    }
}
