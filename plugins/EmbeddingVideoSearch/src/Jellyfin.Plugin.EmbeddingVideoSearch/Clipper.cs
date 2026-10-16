using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed class ClipCheck
{
    public bool Accepted { get; init; }
    public string ErrorCode { get; init; } = "";
    public bool Retryable { get; init; }
    public double RequestedStartSeconds { get; init; }
    public double RequestedDurationSeconds { get; init; }
    public double ProbedDurationSeconds { get; init; }
    public double? AudioDurationSeconds { get; init; }
    public double StartSeconds { get; init; }

    /// <summary>One output video frame plus one AAC frame when the window has audio. Missing measurements stay at 0 and the window is refused.</summary>
    public double SlackSeconds { get; init; }
    public bool HasVideo { get; init; }
    public bool HasAudio { get; init; }
}

public interface IWindowClipper
{
    Task<ClipCheck> ClipAsync(LeaseTicket lease, string outputPath, CancellationToken cancellationToken);
}

public sealed class FfmpegWindowClipper : IWindowClipper
{
    public const string VideoEncoder = "libx264";
    public const string VideoPreset = "veryfast";
    public const string VideoCrf = "28";
    public const string PixelFormat = "yuv420p";
    public const string AudioEncoder = "aac";
    public const string AudioSampleRate = "16000";
    public const int AacFrameSamples = 1024;
    public const string InputVersion =
        "joint-x264-" + VideoPreset + "-crf" + VideoCrf + "-yuv420p-aac-" + AudioSampleRate + "-mono-v1";
    public const string AudioVersion = "source-default-audio-index-v1";
    public const int MaxCapturedChars = 8192;

    private readonly Func<(string? Ffmpeg, string? Ffprobe)> _tools;

    public FfmpegWindowClipper(Func<string?> ffmpegPath)
    {
        _tools = () =>
        {
            var ffmpeg = ffmpegPath();
            return (ffmpeg, ProbeBeside(ffmpeg));
        };
    }

    public FfmpegWindowClipper(string ffmpegPath, string ffprobePath)
    {
        _tools = () => (ffmpegPath, string.IsNullOrWhiteSpace(ffprobePath) ? ProbeBeside(ffmpegPath) : ffprobePath);
    }

    public static string? ProbeBeside(string? ffmpegPath)
    {
        if (string.IsNullOrWhiteSpace(ffmpegPath))
        {
            return null;
        }

        var probe = Path.Combine(Path.GetDirectoryName(ffmpegPath) ?? "", "ffprobe");
        return File.Exists(probe) ? probe : null;
    }

    public static string[] BuildArguments(string source, double startSeconds, double durationSeconds, string output, int? audioStreamIndex)
    {
        var start = startSeconds.ToString("0.######", CultureInfo.InvariantCulture);
        var duration = durationSeconds.ToString("0.######", CultureInfo.InvariantCulture);
        var args = new List<string>
        {
            "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats", "-y",
            "-accurate_seek", "-ss", start,
            "-i", source,
            "-t", duration,
            "-map", "0:v:0"
        };
        if (audioStreamIndex is int index)
        {
            args.Add("-map");
            args.Add("0:" + index.ToString(CultureInfo.InvariantCulture));
            args.Add("-c:v");
            args.Add(VideoEncoder);
            args.Add("-preset");
            args.Add(VideoPreset);
            args.Add("-crf");
            args.Add(VideoCrf);
            args.Add("-pix_fmt");
            args.Add(PixelFormat);
            args.Add("-c:a");
            args.Add(AudioEncoder);
            args.Add("-ac");
            args.Add("1");
            args.Add("-ar");
            args.Add(AudioSampleRate);
        }
        else
        {
            args.Add("-an");
            args.Add("-c:v");
            args.Add(VideoEncoder);
            args.Add("-preset");
            args.Add(VideoPreset);
            args.Add("-crf");
            args.Add(VideoCrf);
            args.Add("-pix_fmt");
            args.Add(PixelFormat);
        }

        args.Add("-sn");
        args.Add("-dn");
        args.Add(output);
        return args.ToArray();
    }

    public async Task<ClipCheck> ClipAsync(LeaseTicket lease, string outputPath, CancellationToken cancellationToken)
    {
        var start = lease.StartTicks / (double)ApiV1.TicksPerSecond;
        var duration = (lease.EndTicks - lease.StartTicks) / (double)ApiV1.TicksPerSecond;
        if (lease.HasAudio && lease.AudioStreamIndex is null)
        {
            return Reject("audio_index_unknown", false, start, duration);
        }

        var tools = _tools();
        if (string.IsNullOrWhiteSpace(tools.Ffmpeg) || !File.Exists(tools.Ffmpeg))
        {
            return Reject("ffmpeg_unavailable", false, start, duration);
        }

        Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);
        var arguments = BuildArguments(lease.SourcePath, start, duration, outputPath, lease.HasAudio ? lease.AudioStreamIndex : null);
        var ran = await CapturedProcess.RunAsync(tools.Ffmpeg, arguments, cancellationToken).ConfigureAwait(false);
        if (ran.ExitCode != 0 || !File.Exists(outputPath))
        {
            return Reject("clip_failed", true, start, duration);
        }

        return await InspectAsync(outputPath, duration, lease.HasAudio, start, cancellationToken).ConfigureAwait(false);
    }

    public async Task<ClipCheck> InspectAsync(string path, double expectedDuration, bool requireAudio, double requestedStart, CancellationToken cancellationToken)
    {
        var tools = _tools();
        if (string.IsNullOrWhiteSpace(tools.Ffprobe) || !File.Exists(tools.Ffprobe))
        {
            return Reject("probe_unknown", false, requestedStart, expectedDuration);
        }

        var probe = await CapturedProcess.RunAsync(
            tools.Ffprobe,
            ["-v", "error", "-count_frames", "-show_entries", "stream=codec_type,start_time,duration,nb_read_frames,avg_frame_rate,r_frame_rate,sample_rate", "-of", "json", path],
            cancellationToken).ConfigureAwait(false);
        if (probe.ExitCode != 0)
        {
            return Reject("probe_unknown", false, requestedStart, expectedDuration);
        }

        return Evaluate(probe.StandardOutput, expectedDuration, requireAudio, requestedStart);
    }

    public static ClipCheck Evaluate(string ffprobeJson, double expectedDuration, bool requireAudio, double requestedStart)
    {
        try
        {
            using var document = JsonDocument.Parse(ffprobeJson);
            if (!document.RootElement.TryGetProperty("streams", out var streams) || streams.ValueKind != JsonValueKind.Array)
            {
                return Reject("probe_unknown", false, requestedStart, expectedDuration);
            }

            JsonElement? video = null;
            JsonElement? audio = null;
            foreach (var stream in streams.EnumerateArray())
            {
                if (!stream.TryGetProperty("codec_type", out var kind))
                {
                    continue;
                }

                var name = kind.GetString();
                if (name == "video" && video is null)
                {
                    video = stream;
                }
                else if (name == "audio" && audio is null)
                {
                    audio = stream;
                }
            }

            if (video is null || !TryTiming(video.Value, isAudio: false, out var videoTiming))
            {
                return Reject("probe_unknown", false, requestedStart, expectedDuration);
            }

            var audioFrame = 0d;
            Timing audioTiming = default;
            if (requireAudio)
            {
                if (audio is null || !TryTiming(audio.Value, isAudio: true, out audioTiming))
                {
                    return Reject("probe_unknown", false, requestedStart, expectedDuration);
                }

                audioFrame = audioTiming.FrameSeconds;
            }

            var slack = videoTiming.FrameSeconds + audioFrame;
            if (slack <= 0 || double.IsNaN(slack) || double.IsInfinity(slack))
            {
                return Reject("probe_unknown", false, requestedStart, expectedDuration);
            }

            var videoOk = Math.Abs(videoTiming.DurationSeconds - expectedDuration) <= slack && Math.Abs(videoTiming.StartSeconds) <= slack;
            var audioOk = !requireAudio || (Math.Abs(audioTiming.DurationSeconds - expectedDuration) <= slack && Math.Abs(audioTiming.StartSeconds) <= slack);
            var accepted = videoOk && audioOk;
            var start = Math.Max(Math.Abs(videoTiming.StartSeconds), requireAudio ? Math.Abs(audioTiming.StartSeconds) : 0);
            return new ClipCheck
            {
                Accepted = accepted,
                ErrorCode = accepted ? "" : "clip_mismatch",
                RequestedStartSeconds = requestedStart,
                RequestedDurationSeconds = expectedDuration,
                ProbedDurationSeconds = videoTiming.DurationSeconds,
                AudioDurationSeconds = requireAudio ? audioTiming.DurationSeconds : null,
                StartSeconds = start,
                SlackSeconds = slack,
                HasVideo = true,
                HasAudio = requireAudio && audioOk
            };
        }
        catch (Exception ex) when (ex is JsonException or KeyNotFoundException or FormatException)
        {
            return Reject("probe_unknown", false, requestedStart, expectedDuration);
        }
    }

    private static bool TryTiming(JsonElement stream, bool isAudio, out Timing timing)
    {
        timing = default;
        if (!TryNumber(stream, "start_time", out var start))
        {
            return false;
        }

        double? duration = TryNumber(stream, "duration", out var parsed) ? parsed : null;
        double frame;
        if (isAudio)
        {
            if (!TryNumber(stream, "sample_rate", out var sampleRate) || sampleRate <= 0)
            {
                return false;
            }

            frame = AacFrameSamples / sampleRate;
            if (duration is null && TryNumber(stream, "nb_read_frames", out var frames) && frames > 0)
            {
                duration = frames * frame;
            }
        }
        else
        {
            if (!TryRate(stream, out var rate) || rate <= 0)
            {
                return false;
            }

            frame = 1d / rate;
            if (duration is null && TryNumber(stream, "nb_read_frames", out var frames) && frames > 0)
            {
                duration = frames * frame;
            }
        }

        if (duration is not double known || known < 0 || double.IsNaN(known) || double.IsInfinity(known))
        {
            return false;
        }

        timing = new Timing(known, start, frame);
        return true;
    }

    private static bool TryRate(JsonElement stream, out double rate)
    {
        rate = 0;
        foreach (var name in new[] { "avg_frame_rate", "r_frame_rate" })
        {
            if (!stream.TryGetProperty(name, out var value))
            {
                continue;
            }

            var text = value.ValueKind == JsonValueKind.String ? value.GetString() : value.ToString();
            if (string.IsNullOrWhiteSpace(text) || text is "0/0" or "N/A")
            {
                continue;
            }

            var parts = text.Split('/');
            if (parts.Length == 2
                && double.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out var num)
                && double.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out var den)
                && den != 0)
            {
                rate = num / den;
            }
            else if (!double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out rate))
            {
                continue;
            }

            if (rate > 0 && rate < 1000)
            {
                return true;
            }
        }

        return false;
    }

    private static bool TryNumber(JsonElement stream, string name, out double number)
    {
        number = 0;
        if (!stream.TryGetProperty(name, out var value) || value.ValueKind is JsonValueKind.Null or JsonValueKind.Undefined)
        {
            return false;
        }

        if (value.ValueKind == JsonValueKind.Number)
        {
            return value.TryGetDouble(out number) && !double.IsNaN(number) && !double.IsInfinity(number);
        }

        var text = value.GetString();
        if (string.IsNullOrWhiteSpace(text) || text == "N/A")
        {
            return false;
        }

        return double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out number)
            && !double.IsNaN(number)
            && !double.IsInfinity(number);
    }

    private static ClipCheck Reject(string code, bool retryable, double start, double duration)
        => new() { ErrorCode = code, Retryable = retryable, RequestedStartSeconds = start, RequestedDurationSeconds = duration };

    private readonly record struct Timing(double DurationSeconds, double StartSeconds, double FrameSeconds);
}

public static class CapturedProcess
{
    public static async Task<ProcessCapture> RunAsync(string fileName, IReadOnlyList<string> arguments, CancellationToken cancellationToken)
    {
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                FileName = fileName,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false
            }
        };
        foreach (var argument in arguments)
        {
            process.StartInfo.ArgumentList.Add(argument);
        }

        process.Start();
        var stdout = DrainAsync(process.StandardOutput);
        var stderr = DrainAsync(process.StandardError);
        try
        {
            await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
            return new ProcessCapture(process.ExitCode, await stdout.ConfigureAwait(false), await stderr.ConfigureAwait(false));
        }
        catch (OperationCanceledException)
        {
            try
            {
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                }
            }
            catch (Exception)
            {
            }

            await process.WaitForExitAsync(CancellationToken.None).ConfigureAwait(false);
            try
            {
                await Task.WhenAll(stdout, stderr).ConfigureAwait(false);
            }
            catch (Exception)
            {
            }

            throw;
        }
    }

    private static async Task<string> DrainAsync(StreamReader reader)
    {
        var buffer = new char[4096];
        var kept = new StringBuilder();
        while (true)
        {
            var read = await reader.ReadAsync(buffer.AsMemory()).ConfigureAwait(false);
            if (read == 0)
            {
                return kept.ToString();
            }

            var room = FfmpegWindowClipper.MaxCapturedChars - kept.Length;
            if (room > 0)
            {
                kept.Append(buffer, 0, Math.Min(room, read));
            }
        }
    }
}

public readonly record struct ProcessCapture(int ExitCode, string StandardOutput, string StandardError);
