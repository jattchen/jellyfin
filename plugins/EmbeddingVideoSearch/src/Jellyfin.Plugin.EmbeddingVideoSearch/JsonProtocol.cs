using System.Text.Json;
using System.Text.Json.Serialization;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public static class FlexibleGuid
{
    public static bool TryRead(string? text, out Guid id)
    {
        if (string.IsNullOrEmpty(text))
        {
            id = Guid.Empty;
            return false;
        }

        return Guid.TryParseExact(text, "D", out id) || Guid.TryParseExact(text, "N", out id);
    }
}

public sealed class FlexibleGuidConverter : JsonConverter<Guid>
{
    public override Guid Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options)
    {
        if (reader.TokenType != JsonTokenType.String || !FlexibleGuid.TryRead(reader.GetString(), out var id))
        {
            throw new JsonException("A GUID must be 32 hex digits or 8-4-4-4-12.");
        }

        return id;
    }

    public override void Write(Utf8JsonWriter writer, Guid value, JsonSerializerOptions options)
        => writer.WriteStringValue(value.ToString("D"));
}

public static class JsonProtocol
{
    public static JsonSerializerOptions Options { get; } = Create();

    public static string Serialize(object value)
        => JsonSerializer.Serialize(value, Options);

    public static T? Deserialize<T>(string json)
        => JsonSerializer.Deserialize<T>(json, Options);

    private static JsonSerializerOptions Create()
    {
        var options = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
            DictionaryKeyPolicy = JsonNamingPolicy.CamelCase,
            PropertyNameCaseInsensitive = false,
            DefaultIgnoreCondition = JsonIgnoreCondition.Never,
            WriteIndented = false
        };
        options.Converters.Add(new FlexibleGuidConverter());
        return options;
    }
}
