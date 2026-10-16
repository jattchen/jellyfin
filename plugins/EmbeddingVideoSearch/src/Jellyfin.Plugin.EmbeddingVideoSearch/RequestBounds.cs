using System.Text;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public static class RequestBounds
{
    public const int MaxBytes = 65_536;

    public static async Task<string> ReadUtf8Async(Stream stream, CancellationToken cancellationToken)
    {
        var buffer = new byte[MaxBytes + 1];
        var read = 0;
        while (read <= MaxBytes)
        {
            var count = await stream.ReadAsync(buffer.AsMemory(read, buffer.Length - read), cancellationToken).ConfigureAwait(false);
            if (count == 0)
            {
                break;
            }

            read += count;
            if (read > MaxBytes)
            {
                throw new SearchInputException("invalidRequest", "Request body is too large.");
            }
        }

        return Encoding.UTF8.GetString(buffer, 0, read);
    }
}
