using System.Text.Json;
using System.Text.Json.Serialization;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

/// <summary>Imported, independently versioned representations. Never replaces the worker's joint generation.</summary>
public sealed class CandidateRepresentation
{
    public string Channel { get; set; } = "";
    public string Version { get; set; } = "";
    public string Model { get; set; } = "";
    public string Revision { get; set; } = "";
    public string ProcessingId { get; set; } = "";
    public int Dimension { get; set; }
    public bool Normalized { get; set; }
    public string InputVersion { get; set; } = "";
    public string EmbeddingTask { get; set; } = "";
    public string ContentVersion { get; set; } = "";
    public string SegmentationVersion { get; set; } = "";
    public string ReferenceVersion { get; set; } = "";
    public string ReferenceQueryVersion { get; set; } = "";
    public string Algorithm { get; set; } = CandidateRanking.Version;
    public double Scale { get; set; }
    public double[] Background { get; set; } = [];
    public CandidateRow[] Candidates { get; set; } = [];

    public bool Matches(StoredSignature signature) => Model == signature.Model && Revision == signature.Revision
        && ProcessingId == signature.ProcessingId && Dimension == signature.Dimension && Normalized == signature.Normalized
        && (Channel != "joint" || InputVersion == signature.InputVersion);

    public void Validate()
    {
        if (Channel is not ("joint" or "text") || string.IsNullOrWhiteSpace(Version)
            || Model != ApiV1.ModelId || Revision != ApiV1.Revision || string.IsNullOrWhiteSpace(ProcessingId)
            || Dimension != ApiV1.Dimension || !Normalized || string.IsNullOrWhiteSpace(ContentVersion)
            || (Channel == "joint" && (InputVersion != ApiV1.InputVersion || EmbeddingTask != "video_audio"))
            || (Channel == "text" && (InputVersion != "transcript-document-cache-v1" || EmbeddingTask != "document"))
            || string.IsNullOrWhiteSpace(SegmentationVersion) || string.IsNullOrWhiteSpace(ReferenceVersion)
            || string.IsNullOrWhiteSpace(ReferenceQueryVersion) || Algorithm != CandidateRanking.Version
            || !double.IsFinite(Scale) || Scale <= 0 || Background.Length == 0
            || Background.Any(v => !double.IsFinite(v)) || Candidates.Length == 0)
        {
            throw new InvalidDataException("Invalid candidate representation or reference curve.");
        }

        var ids = new HashSet<Guid>();
        var anchors = new HashSet<string>(StringComparer.Ordinal);
        foreach (var row in Candidates)
        {
            if (row.SegmentId == Guid.Empty || !ids.Add(row.SegmentId) || !anchors.Add(row.AnchorId)
                || string.IsNullOrWhiteSpace(row.CandidateId) || row.ItemId == Guid.Empty || row.LibraryId == Guid.Empty
                || string.IsNullOrWhiteSpace(row.MediaSourceId) || string.IsNullOrWhiteSpace(row.ContentVersion)
                || row.StartTicks < 0 || row.EndTicks <= row.StartTicks || row.EndTicks > row.RuntimeTicks
                || row.Canonical.SourceItemId == Guid.Empty || row.Canonical.StartTicks < 0
                || row.Canonical.EndTicks <= row.Canonical.StartTicks
                || row.Canonical.EndTicks - row.Canonical.StartTicks != row.EndTicks - row.StartTicks
                || row.AnchorId != $"{row.Canonical.SourceItemId:N}:{row.Canonical.StartTicks}:{row.Canonical.EndTicks}"
                || row.Vector.Length != ApiV1.Dimension || row.Vector.Any(v => !double.IsFinite(v))
                || Math.Abs(CandidateRanking.Sum(row.Vector.Select(v => v * v)) - 1) > 0.02)
            {
                throw new InvalidDataException("Invalid candidate identity, timing or vector.");
            }
        }
    }
}

public sealed class CanonicalAnchor
{
    [JsonPropertyName("sourceItemId")]
    public Guid SourceItemId { get; set; }
    [JsonPropertyName("startTicks")]
    public long StartTicks { get; set; }
    [JsonPropertyName("endTicks")]
    public long EndTicks { get; set; }
}

public sealed class CandidateRow
{
    public Guid SegmentId { get; set; }
    public string CandidateId { get; set; } = "";
    public string AnchorId { get; set; } = "";
    public Guid ItemId { get; set; }
    public string MediaSourceId { get; set; } = "";
    public Guid LibraryId { get; set; }
    public string ContentVersion { get; set; } = "";
    public long StartTicks { get; set; }
    public long EndTicks { get; set; }
    public long RuntimeTicks { get; set; }
    public CanonicalAnchor Canonical { get; set; } = new();
    public string? Transcript { get; set; }
    public double[] Vector { get; set; } = [];
}

public sealed class CandidateSnapshot
{
    public CandidateRepresentation[] Representations { get; init; } = [];
    public string Identity => string.Join("|", Representations.OrderBy(r => r.Channel, StringComparer.Ordinal).Select(r => r.Channel + ":" + r.Version));
}

public static class CandidateRanking
{
    public const string Version = "reference16-logistic-tail-f64-v1";
    public const string ScoreMeaning = "referenceTailR";

    // Neumaier compensated summation; no float32 score accumulation or renormalization.
    public static double Sum(IEnumerable<double> values)
    {
        double sum = 0, correction = 0;
        foreach (var value in values)
        {
            var next = sum + value;
            correction += Math.Abs(sum) >= Math.Abs(value) ? (sum - next) + value : (value - next) + sum;
            sum = next;
        }

        return sum + correction;
    }

    public static double Dot(float[] query, double[] document)
    {
        if (query.Length != ApiV1.Dimension || document.Length != query.Length)
        {
            throw new InvalidDataException("Candidate dimension mismatch.");
        }

        return Sum(query.Select((v, i) => (double)v * document[i]));
    }

    public static double ReferenceScore(double similarity, CandidateRepresentation representation)
    {
        if (!double.IsFinite(similarity))
        {
            throw new InvalidDataException("Nonfinite candidate similarity.");
        }

        var terms = representation.Background.Select(x =>
        {
            var z = (similarity - x) / representation.Scale;
            // log1p for x in [0,1], avoiding cancellation in the high-score tail.
            var x1 = Math.Exp(-Math.Abs(z));
            var u = 1 + x1;
            var log1p = u == 1 ? x1 : Math.Log(u) * x1 / (u - 1);
            return -(Math.Max(z, 0) + log1p);
        }).ToArray();
        var largest = terms.Max();
        var logp = largest + Math.Log(Sum(terms.Select(t => Math.Exp(t - largest)))) - Math.Log(terms.Length);
        return -Math.Min(logp, 0) / Math.Log(10);
    }

    public static List<SearchHit> Rank(float[] query, CandidateSnapshot snapshot, Func<CandidateRow, bool> allowed)
    {
        var hits = new List<SearchHit>();
        foreach (var representation in snapshot.Representations)
        {
            foreach (var row in representation.Candidates.Where(allowed))
            {
                var raw = Dot(query, row.Vector);
                hits.Add(new SearchHit
                {
                    ItemId = row.ItemId, MediaSourceId = row.MediaSourceId, SegmentId = row.SegmentId,
                    StartTicks = row.StartTicks, EndTicks = row.EndTicks, IndexComplete = true,
                    Score = ReferenceScore(raw, representation), RawScore = raw, Quality = "candidate",
                    Representation = representation.Channel == "joint" ? "jointAudioVideo" : "transcriptText",
                    CandidateId = row.CandidateId, AnchorId = row.AnchorId, Canonical = row.Canonical,
                    RepresentationVersion = representation.Version, ReferenceVersion = representation.ReferenceVersion,
                    Transcript = string.IsNullOrWhiteSpace(row.Transcript) ? null : row.Transcript,
                    BindingVersion = SegmentBinding.Version(row.SegmentId, row.ItemId, row.MediaSourceId, row.ContentVersion, row.StartTicks, row.EndTicks, representation.Channel == "joint" ? "jointAudioVideo" : "transcriptText", row.CandidateId, representation.Version)
                });
            }
        }

        // Only the exact same playable source and local range collapse, keeping the stronger R.
        // Partial overlap never folds and never borrows the other interval's transcript.
        var merged = hits.GroupBy(h => (h.ItemId, h.MediaSourceId, h.StartTicks, h.EndTicks))
            .Select(g =>
            {
                var ordered = g.OrderByDescending(h => h.Score)
                    .ThenBy(h => h.Representation == "transcriptText" ? "text" : "visual", StringComparer.Ordinal)
                    .ToList();
                var winner = ordered[0];
                if (string.IsNullOrWhiteSpace(winner.Transcript))
                {
                    winner.Transcript = ordered.Select(hit => hit.Transcript).FirstOrDefault(text => !string.IsNullOrWhiteSpace(text));
                }

                return winner;
            });
        return merged.OrderByDescending(h => h.Score)
            .ThenBy(h => h.AnchorId, StringComparer.Ordinal)
            .ThenBy(h => h.Representation == "transcriptText" ? "text" : "visual", StringComparer.Ordinal)
            .ToList();
    }

    /// <summary>
    /// Films follow their best hit in the existing ranking. Each selected film then keeps its own
    /// next hits, up to <paramref name="hitsPerVideo"/>. Hit count does not change film order.
    /// </summary>
    public static List<SearchHit> TakeByVideo(IReadOnlyList<SearchHit> ranked, int videoLimit, int hitsPerVideo)
    {
        var order = new List<Guid>();
        var groups = new Dictionary<Guid, List<SearchHit>>();
        foreach (var hit in ranked)
        {
            if (!groups.TryGetValue(hit.ItemId, out var bucket))
            {
                if (order.Count >= videoLimit)
                {
                    continue;
                }

                bucket = [];
                groups[hit.ItemId] = bucket;
                order.Add(hit.ItemId);
            }

            if (bucket.Count < hitsPerVideo)
            {
                bucket.Add(hit);
            }
        }

        return order.SelectMany(id => groups[id]).ToList();
    }
}

public sealed partial class IndexStore
{
    private static readonly JsonSerializerOptions CandidateJson = new() { PropertyNameCaseInsensitive = true };

    private void EnsureCandidateSchema()
    {
        using var command = _connection.CreateCommand();
        command.CommandText = """
            CREATE TABLE IF NOT EXISTS candidate_representations (
              channel TEXT NOT NULL, version TEXT NOT NULL, payload_json TEXT NOT NULL,
              active INTEGER NOT NULL, PRIMARY KEY(channel,version));
            CREATE UNIQUE INDEX IF NOT EXISTS active_candidate_representation
              ON candidate_representations(channel) WHERE active=1;
            """;
        command.ExecuteNonQuery();
    }

    public CandidateSnapshot ReadCandidateSnapshot()
    {
        lock (_gate)
        {
            using var exists = _connection.CreateCommand();
            exists.CommandText = "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='candidate_representations'";
            if (Convert.ToInt32(exists.ExecuteScalar()) == 0)
            {
                return new CandidateSnapshot();
            }

            using var command = _connection.CreateCommand();
            command.CommandText = "SELECT channel,version,payload_json FROM candidate_representations WHERE active=1 ORDER BY channel";
            using var reader = command.ExecuteReader();
            var representations = new List<CandidateRepresentation>();
            while (reader.Read())
            {
                var representation = JsonSerializer.Deserialize<CandidateRepresentation>(reader.GetString(2), CandidateJson)
                    ?? throw new InvalidDataException("Missing candidate payload.");
                representation.Validate();
                if (representation.Channel != reader.GetString(0) || representation.Version != reader.GetString(1))
                {
                    throw new InvalidDataException("Candidate version identity mismatch.");
                }

                representations.Add(representation);
            }

            if (representations.Count != 0 && (representations.Count != 2 || representations.Select(r => r.Channel).Distinct().Count() != 2
                || representations.SelectMany(r => r.Candidates).Select(c => c.SegmentId).Distinct().Count() != representations.Sum(r => r.Candidates.Length)))
            {
                throw new InvalidDataException("Both independent candidate representations are required.");
            }

            return new CandidateSnapshot { Representations = representations.ToArray() };
        }
    }
}
