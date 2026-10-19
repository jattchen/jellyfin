namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public static class HostCompatibility
{
    public static bool IsSupported(Version version)
        => version.Major == 12
           && version.Minor == 2
           && version.Build <= 0
           && version.Revision <= 0;
}

public readonly record struct WindowSpan(long StartTicks, long EndTicks);

public static class WindowPlanner
{
    public static IReadOnlyList<WindowSpan> Plan(long durationTicks)
        => Plan(durationTicks, ApiV1.WindowTicks, ApiV1.StepTicks);

    public static IReadOnlyList<WindowSpan> Plan(long durationTicks, long windowTicks, long stepTicks)
    {
        if (durationTicks <= 0)
        {
            return [];
        }

        if (durationTicks <= windowTicks)
        {
            return [new WindowSpan(0, durationTicks)];
        }

        var windows = new List<WindowSpan>();
        var start = 0L;
        while (start + windowTicks < durationTicks)
        {
            windows.Add(new WindowSpan(start, start + windowTicks));
            start += stepTicks;
        }

        var tailStart = durationTicks - windowTicks;
        if (windows.Count == 0 || windows[^1].StartTicks != tailStart)
        {
            windows.Add(new WindowSpan(tailStart, durationTicks));
        }

        return windows;
    }

    public static bool Covers(long durationTicks, IReadOnlyList<WindowSpan> windows)
    {
        if (durationTicks <= 0)
        {
            return windows.Count == 0;
        }

        if (windows.Count == 0 || windows[0].StartTicks != 0 || windows[^1].EndTicks != durationTicks)
        {
            return false;
        }

        for (var i = 1; i < windows.Count; i++)
        {
            if (windows[i].StartTicks > windows[i - 1].EndTicks)
            {
                return false;
            }
        }

        return true;
    }
}

public static class ContentVersion
{
    public static string FromMediaFacts(long sizeBytes, long runtimeTicks, DateTimeOffset modifiedUtc, string container, int audioIndex)
    {
        var text = string.Create(
            System.Globalization.CultureInfo.InvariantCulture,
            $"{sizeBytes}|{runtimeTicks}|{modifiedUtc.UtcDateTime.Ticks}|{container}|{audioIndex}");
        return Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    }
}

public static class VectorMath
{
    public const float UnitTolerance = 1e-4f;

    public static bool IsUnit(ReadOnlySpan<float> vector)
    {
        if (vector.Length != ApiV1.Dimension)
        {
            return false;
        }

        double sum = 0;
        foreach (var value in vector)
        {
            if (!float.IsFinite(value))
            {
                return false;
            }

            sum += value * (double)value;
        }

        return Math.Abs(Math.Sqrt(sum) - 1d) <= UnitTolerance;
    }

    public static float Dot(ReadOnlySpan<float> left, ReadOnlySpan<float> right)
    {
        if (left.Length != right.Length)
        {
            throw new ArgumentException("Vectors must have the same length.");
        }

        double sum = 0;
        for (var i = 0; i < left.Length; i++)
        {
            sum += left[i] * (double)right[i];
        }

        return (float)sum;
    }

    public static byte[] ToBlob(ReadOnlySpan<float> vector)
    {
        var bytes = new byte[vector.Length * sizeof(float)];
        for (var i = 0; i < vector.Length; i++)
        {
            System.Buffers.Binary.BinaryPrimitives.WriteSingleLittleEndian(bytes.AsSpan(i * sizeof(float)), vector[i]);
        }

        return bytes;
    }

    public static float[] FromBlob(byte[] blob)
    {
        if (blob.Length % sizeof(float) != 0)
        {
            throw new InvalidDataException("Vector blob length is not a multiple of 4.");
        }

        var vector = new float[blob.Length / sizeof(float)];
        for (var i = 0; i < vector.Length; i++)
        {
            vector[i] = System.Buffers.Binary.BinaryPrimitives.ReadSingleLittleEndian(blob.AsSpan(i * sizeof(float)));
        }

        return vector;
    }
}
