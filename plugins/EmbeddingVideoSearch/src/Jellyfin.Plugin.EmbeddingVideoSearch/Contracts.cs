using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Serialization;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed class ErrorBody
{
    [JsonPropertyName("apiVersion")]
    public string ApiVersion { get; set; } = ApiV1.Version;

    [JsonPropertyName("error")]
    public ErrorDetail Error { get; set; } = new();
}

public sealed class ErrorDetail
{
    [JsonPropertyName("code")]
    public string Code { get; set; } = "";

    [JsonPropertyName("message")]
    public string Message { get; set; } = "";
}

public sealed class AvailabilityBody
{
    [JsonPropertyName("search")]
    public string Search { get; set; } = "empty";

    [JsonPropertyName("searchReason")]
    public string SearchReason { get; set; } = "none";

    [JsonPropertyName("indexing")]
    public string Indexing { get; set; } = "idle";

    [JsonPropertyName("indexingReasons")]
    public List<string> IndexingReasons { get; set; } = [];
}

public sealed class CompatibilityBody
{
    [JsonPropertyName("hostVersion")]
    public string HostVersion { get; set; } = "";

    [JsonPropertyName("supportedHostVersions")]
    public List<string> SupportedHostVersions { get; set; } = ["12.2.0"];

    [JsonPropertyName("hostCompatible")]
    public bool HostCompatible { get; set; }

    [JsonPropertyName("model")]
    public string Model { get; set; } = ApiV1.ModelId;

    [JsonPropertyName("revision")]
    public string Revision { get; set; } = ApiV1.Revision;

    [JsonPropertyName("processingId")]
    public string? ProcessingId { get; set; }

    [JsonPropertyName("dimension")]
    public int Dimension { get; set; } = ApiV1.Dimension;

    [JsonPropertyName("normalized")]
    public bool Normalized { get; set; } = true;

    [JsonPropertyName("inputVersion")]
    public string InputVersion { get; set; } = ApiV1.InputVersion;

    [JsonPropertyName("windowVersion")]
    public string WindowVersion { get; set; } = ApiV1.WindowVersion;

    [JsonPropertyName("audioVersion")]
    public string AudioVersion { get; set; } = ApiV1.AudioVersion;

    [JsonPropertyName("indexSignatureMatches")]
    public bool IndexSignatureMatches { get; set; } = true;
}

public sealed class LibraryCoverageBody
{
    [JsonPropertyName("libraryId")]
    public Guid LibraryId { get; set; }

    [JsonPropertyName("name")]
    public string Name { get; set; } = "";
}

public sealed class CoverageBody
{
    [JsonPropertyName("candidatesSearchable")]
    public int CandidatesSearchable { get; set; }
    [JsonPropertyName("libraries")]
    public List<LibraryCoverageBody> Libraries { get; set; } = [];

    [JsonPropertyName("videosTotal")]
    public int VideosTotal { get; set; }

    [JsonPropertyName("videosComplete")]
    public int VideosComplete { get; set; }

    [JsonPropertyName("videosPartial")]
    public int VideosPartial { get; set; }

    [JsonPropertyName("videosFailed")]
    public int VideosFailed { get; set; }

    [JsonPropertyName("videosWaiting")]
    public int VideosWaiting { get; set; }

    [JsonPropertyName("windowsSearchable")]
    public int WindowsSearchable { get; set; }

    [JsonPropertyName("indexComplete")]
    public bool IndexComplete { get; set; }
}

public sealed class WindowContractBody
{
    [JsonPropertyName("lengthTicks")]
    public long LengthTicks { get; set; } = ApiV1.WindowTicks;

    [JsonPropertyName("stepTicks")]
    public long StepTicks { get; set; } = ApiV1.StepTicks;

    [JsonPropertyName("timeSource")]
    public string TimeSource { get; set; } = "recordedWindow";
}

public sealed class QualityPolicyBody
{
    [JsonPropertyName("relatedMinimum")]
    public double? RelatedMinimum { get; set; } = ApiV1.DefaultRelatedMinimum;

    [JsonPropertyName("calibrated")]
    public bool Calibrated { get; set; }

    [JsonPropertyName("scoreMeaning")]
    public string ScoreMeaning { get; set; } = "cosine";

    [JsonPropertyName("rankingVersion")]
    public string? RankingVersion { get; set; }

    [JsonPropertyName("representations")]
    public Dictionary<string, string> Representations { get; set; } = [];
}

public sealed class CapabilitiesResponse
{
    [JsonPropertyName("apiVersion")]
    public string ApiVersion { get; set; } = ApiV1.Version;

    [JsonPropertyName("ticksPerSecond")]
    public long TicksPerSecond { get; set; } = ApiV1.TicksPerSecond;

    [JsonPropertyName("availability")]
    public AvailabilityBody Availability { get; set; } = new();

    [JsonPropertyName("compatibility")]
    public CompatibilityBody Compatibility { get; set; } = new();

    [JsonPropertyName("coverage")]
    public CoverageBody Coverage { get; set; } = new();

    [JsonPropertyName("window")]
    public WindowContractBody Window { get; set; } = new();

    [JsonPropertyName("qualityPolicy")]
    public QualityPolicyBody QualityPolicy { get; set; } = new();

    [JsonPropertyName("features")]
    public List<string> Features { get; set; } = [ApiV1.GroupedVideoFeature];
}

public sealed class SearchRequest
{
    [JsonPropertyName("diagnostics")]
    public bool Diagnostics { get; set; }

    [JsonPropertyName("groupByVideo")]
    public bool GroupByVideo { get; set; }
    [JsonPropertyName("query")]
    public string? Query { get; set; }

    [JsonPropertyName("libraryIds")]
    public Guid[]? LibraryIds { get; set; }

    [JsonPropertyName("parentId")]
    public Guid? ParentId { get; set; }

    [JsonPropertyName("limit")]
    public int? Limit { get; set; }

    [JsonPropertyName("clientQueryId")]
    public string? ClientQueryId { get; set; }
}

public sealed class SearchHit
{
    [JsonPropertyName("itemId")]
    public Guid ItemId { get; set; }

    [JsonPropertyName("mediaSourceId")]
    public string MediaSourceId { get; set; } = "";

    [JsonPropertyName("segmentId")]
    public Guid SegmentId { get; set; }

    [JsonPropertyName("startTicks")]
    public long StartTicks { get; set; }

    [JsonPropertyName("endTicks")]
    public long EndTicks { get; set; }

    [JsonPropertyName("score")]
    public double Score { get; set; }

    [JsonPropertyName("rawScore")]
    public double? RawScore { get; set; }

    [JsonPropertyName("representation")]
    public string? Representation { get; set; }

    [JsonPropertyName("candidateId")]
    public string? CandidateId { get; set; }

    [JsonPropertyName("anchorId")]
    public string? AnchorId { get; set; }

    [JsonPropertyName("canonical")]
    public CanonicalAnchor? Canonical { get; set; }

    [JsonPropertyName("representationVersion")]
    public string? RepresentationVersion { get; set; }

    [JsonPropertyName("referenceVersion")]
    public string? ReferenceVersion { get; set; }

    [JsonPropertyName("quality")]
    public string Quality { get; set; } = "weak";

    [JsonPropertyName("indexComplete")]
    public bool IndexComplete { get; set; }

    [JsonPropertyName("transcript")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Transcript { get; set; }

    [JsonPropertyName("bindingVersion")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? BindingVersion { get; set; }
}

public static class SegmentBinding
{
    public static string Version(Guid segmentId, Guid itemId, string mediaSourceId, string contentVersion, long startTicks, long endTicks, string representation, string? candidateId = null, string? representationVersion = null)
    {
        var text = string.Create(CultureInfo.InvariantCulture, $"{segmentId:N}|{itemId:N}|{mediaSourceId}|{contentVersion}|{startTicks}|{endTicks}|{representation}|{candidateId}|{representationVersion}");
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();
    }
}

public sealed class SegmentDetailResponse
{
    [JsonPropertyName("candidateId")]
    public string? CandidateId { get; set; }

    [JsonPropertyName("canonical")]
    public CanonicalAnchor? Canonical { get; set; }
    [JsonPropertyName("itemId")]
    public Guid ItemId { get; set; }

    [JsonPropertyName("mediaSourceId")]
    public string MediaSourceId { get; set; } = "";

    [JsonPropertyName("segmentId")]
    public Guid SegmentId { get; set; }

    [JsonPropertyName("startTicks")]
    public long StartTicks { get; set; }

    [JsonPropertyName("endTicks")]
    public long EndTicks { get; set; }

    [JsonPropertyName("score")]
    public float Score { get; set; }

    [JsonPropertyName("quality")]
    public string Quality { get; set; } = "related";

    [JsonPropertyName("indexComplete")]
    public bool IndexComplete { get; set; }

    [JsonPropertyName("representation")]
    public string Representation { get; set; } = "jointAudioVideo";

    [JsonPropertyName("transcript")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? Transcript { get; set; }

    [JsonPropertyName("bindingVersion")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public string? BindingVersion { get; set; }
}

public sealed class SearchResponse
{
    [JsonPropertyName("rawResults")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public List<SearchHit>? RawResults { get; set; }
    [JsonPropertyName("apiVersion")]
    public string ApiVersion { get; set; } = ApiV1.Version;

    [JsonPropertyName("queryId")]
    public Guid QueryId { get; set; }

    [JsonPropertyName("clientQueryId")]
    public string? ClientQueryId { get; set; }

    [JsonPropertyName("availability")]
    public AvailabilityBody Availability { get; set; } = new();

    [JsonPropertyName("indexComplete")]
    public bool IndexComplete { get; set; }

    [JsonPropertyName("qualityPolicy")]
    public QualityPolicyBody QualityPolicy { get; set; } = new();

    [JsonPropertyName("results")]
    public List<SearchHit> Results { get; set; } = [];
}

public sealed class LibrarySelectionRequest
{
    [JsonPropertyName("libraryIds")]
    public Guid[]? LibraryIds { get; set; }
}

public sealed class SettingsRequest
{
    [JsonPropertyName("relatedMinimum")]
    public double? RelatedMinimum { get; set; }

    [JsonPropertyName("calibrated")]
    public bool? Calibrated { get; set; }

    [JsonPropertyName("embeddingServiceBaseUrl")]
    public string? EmbeddingServiceBaseUrl { get; set; }
}

public sealed class AdminLibraryBody
{
    [JsonPropertyName("libraryId")]
    public Guid LibraryId { get; set; }

    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("enabled")]
    public bool Enabled { get; set; }
}

public sealed class PauseBody
{
    [JsonPropertyName("manual")]
    public bool Manual { get; set; }

    [JsonPropertyName("playback")]
    public bool Playback { get; set; }

    [JsonPropertyName("incompatible")]
    public bool Incompatible { get; set; }

    [JsonPropertyName("schemaReadOnly")]
    public bool SchemaReadOnly { get; set; }

    [JsonPropertyName("writerLocked")]
    public bool WriterLocked { get; set; }

    [JsonPropertyName("reasons")]
    public List<string> Reasons { get; set; } = [];
}

public sealed class ModelStatusBody
{
    [JsonPropertyName("configured")]
    public bool Configured { get; set; }

    [JsonPropertyName("reachable")]
    public bool Reachable { get; set; }

    [JsonPropertyName("ready")]
    public bool Ready { get; set; }

    [JsonPropertyName("model")]
    public string Model { get; set; } = ApiV1.ModelId;

    [JsonPropertyName("revision")]
    public string Revision { get; set; } = ApiV1.Revision;

    [JsonPropertyName("processingId")]
    public string? ProcessingId { get; set; }

    [JsonPropertyName("dimension")]
    public int Dimension { get; set; } = ApiV1.Dimension;

    [JsonPropertyName("normalized")]
    public bool Normalized { get; set; } = true;

    [JsonPropertyName("errorCode")]
    public string? ErrorCode { get; set; }
}

public sealed class SignatureStatusBody
{
    [JsonPropertyName("activeGeneration")]
    public int? ActiveGeneration { get; set; }

    [JsonPropertyName("processingId")]
    public string? ProcessingId { get; set; }

    [JsonPropertyName("serviceMatchesActive")]
    public bool ServiceMatchesActive { get; set; }
}

public sealed class ProgressBody
{
    [JsonPropertyName("videosComplete")]
    public int VideosComplete { get; set; }

    [JsonPropertyName("videosWaiting")]
    public int VideosWaiting { get; set; }

    [JsonPropertyName("videosPartial")]
    public int VideosPartial { get; set; }

    [JsonPropertyName("videosFailed")]
    public int VideosFailed { get; set; }

    [JsonPropertyName("windowsRetained")]
    public int WindowsRetained { get; set; }

    [JsonPropertyName("generationsRetained")]
    public int GenerationsRetained { get; set; }
}

public sealed class AdminStatusResponse
{
    [JsonPropertyName("apiVersion")]
    public string ApiVersion { get; set; } = ApiV1.Version;

    [JsonPropertyName("pause")]
    public PauseBody Pause { get; set; } = new();

    [JsonPropertyName("model")]
    public ModelStatusBody Model { get; set; } = new();

    [JsonPropertyName("signature")]
    public SignatureStatusBody Signature { get; set; } = new();

    [JsonPropertyName("libraries")]
    public List<AdminLibraryBody> Libraries { get; set; } = [];

    [JsonPropertyName("progress")]
    public ProgressBody Progress { get; set; } = new();

    [JsonPropertyName("thresholds")]
    public QualityPolicyBody Thresholds { get; set; } = new();

    [JsonPropertyName("activeGeneration")]
    public int ActiveGeneration { get; set; }

    [JsonPropertyName("hostCompatible")]
    public bool HostCompatible { get; set; }

    [JsonPropertyName("reindexStarted")]
    public bool ReindexStarted { get; set; }
}

public sealed class RetryFailedResponse
{
    [JsonPropertyName("apiVersion")]
    public string ApiVersion { get; set; } = ApiV1.Version;

    [JsonPropertyName("accepted")]
    public bool Accepted { get; set; }

    [JsonPropertyName("resetWindows")]
    public int ResetWindows { get; set; }

    [JsonPropertyName("activeGeneration")]
    public int ActiveGeneration { get; set; }

    [JsonPropertyName("generationUnchanged")]
    public bool GenerationUnchanged { get; set; }

    [JsonPropertyName("manualPause")]
    public bool ManualPause { get; set; }

    [JsonPropertyName("reindexStarted")]
    public bool ReindexStarted { get; set; }

    [JsonPropertyName("status")]
    public AdminStatusResponse Status { get; set; } = new();
}

public sealed class RebuildResponse
{
    [JsonPropertyName("apiVersion")]
    public string ApiVersion { get; set; } = ApiV1.Version;

    [JsonPropertyName("accepted")]
    public bool Accepted { get; set; }

    [JsonPropertyName("oldGenerationRetained")]
    public bool OldGenerationRetained { get; set; }

    [JsonPropertyName("reindexStarted")]
    public bool ReindexStarted { get; set; }

    [JsonPropertyName("activeGeneration")]
    public int ActiveGeneration { get; set; }

    [JsonPropertyName("status")]
    public AdminStatusResponse Status { get; set; } = new();
}
