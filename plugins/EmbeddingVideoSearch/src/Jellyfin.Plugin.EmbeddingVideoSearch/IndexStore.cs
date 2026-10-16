using Microsoft.Data.Sqlite;

namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public sealed class StoredSignature
{
    public int Generation { get; init; }
    public string Model { get; init; } = "";
    public string Revision { get; init; } = "";
    public string ProcessingId { get; init; } = "";
    public int Dimension { get; init; }
    public bool Normalized { get; init; }
    public string InputVersion { get; init; } = "";
    public string WindowVersion { get; init; } = "";
    public string AudioVersion { get; init; } = "";
}

public sealed class StoredWindow
{
    public Guid SegmentId { get; init; }
    public Guid ItemId { get; init; }
    public string MediaSourceId { get; init; } = "";
    public Guid LibraryId { get; init; }
    public int Generation { get; init; }
    public string ContentVersion { get; init; } = "";
    public long StartTicks { get; init; }
    public long EndTicks { get; init; }
    public string MediaState { get; init; } = "";
    public float[] Vector { get; init; } = [];
}

public sealed class SegmentRow
{
    public bool Found { get; init; }
    public bool IsCurrent { get; init; }
    public Guid SegmentId { get; init; }
    public Guid ItemId { get; init; }
    public string MediaSourceId { get; init; } = "";
    public Guid LibraryId { get; init; }
    public long StartTicks { get; init; }
    public long EndTicks { get; init; }
    public string MediaState { get; init; } = "";
    public string ContentVersion { get; init; } = "";
}

public sealed partial class IndexStore : IDisposable
{
    public const int CurrentSchemaVersion = 3;

    private readonly SqliteConnection _connection;
    private readonly object _gate = new();
    private readonly FileStream? _writerLock;
    private readonly string? _writerLockPath;
    private int _disposed;

    private static readonly object ProcessLocks = new();
    private static readonly HashSet<string> HeldLocks = new(StringComparer.Ordinal);

    private IndexStore(SqliteConnection connection, FileStream? writerLock, string? writerLockPath, bool schemaReadOnly)
    {
        _connection = connection;
        _writerLock = writerLock;
        _writerLockPath = writerLockPath;
        SchemaReadOnly = schemaReadOnly;
        HasWriterLock = writerLock is not null && !schemaReadOnly;
    }

    public bool SchemaReadOnly { get; }
    public bool HasWriterLock { get; }
    public string DatabasePath { get; private set; } = "";

    public static IndexStore Open(string directory)
    {
        Directory.CreateDirectory(directory);
        try
        {
            SQLitePCL.Batteries_V2.Init();
        }
        catch (Exception)
        {
            // The official host may already have initialized the shared SQLite native library.
        }
        var databasePath = Path.Combine(directory, "index.db");
        var lockPath = Path.Combine(directory, "writer.lock");
        var writerLock = TryLock(lockPath);
        var exists = File.Exists(databasePath);
        if (writerLock is null && !exists)
        {
            throw new IOException("Index database is not initialized and this process does not hold the writer lock.");
        }

        var builder = new SqliteConnectionStringBuilder
        {
            DataSource = databasePath,
            Mode = writerLock is null ? SqliteOpenMode.ReadOnly : SqliteOpenMode.ReadWriteCreate,
            Cache = SqliteCacheMode.Shared,
            Pooling = false
        };
        var connection = new SqliteConnection(builder.ConnectionString);
        connection.Open();
        using (var pragma = connection.CreateCommand())
        {
            pragma.CommandText = "PRAGMA busy_timeout = 5000; PRAGMA foreign_keys = ON;";
            pragma.ExecuteNonQuery();
        }

        if (writerLock is not null)
        {
            using var journal = connection.CreateCommand();
            journal.CommandText = "PRAGMA journal_mode = WAL;";
            journal.ExecuteNonQuery();
        }

        var version = ReadUserVersion(connection);
        var schemaReadOnly = version > CurrentSchemaVersion;
        var store = new IndexStore(connection, writerLock, writerLock is null ? null : lockPath, schemaReadOnly) { DatabasePath = databasePath };
        if (writerLock is not null && !schemaReadOnly)
        {
            store.EnsureSchema(version);
            store.EnsureCandidateSchema();
        }

        return store;
    }

    public bool ManualPause => ReadFlag("manual_pause");
    public bool ImportedCandidatesOnly => ReadFlag("imported_candidates_only");
    public bool Incompatible => ReadFlag("incompatible");
    public double RelatedMinimum => ReadDouble("related_minimum", ApiV1.DefaultRelatedMinimum);
    public bool ThresholdCalibrated => ReadFlag("threshold_calibrated");
    public string EmbeddingBaseUrl => ReadString("embedding_base_url");

    public string? ReadStoredContentVersion(Guid itemId, string mediaSourceId)
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = """
                SELECT content_version FROM media
                WHERE item_id = $item AND media_source_id = $source
                  AND generation = (SELECT generation FROM signatures WHERE active = 1)
                  AND state NOT IN ('hidden', 'unknown')
                """;
            command.Parameters.AddWithValue("$item", itemId.ToString("D"));
            command.Parameters.AddWithValue("$source", mediaSourceId);
            var value = command.ExecuteScalar();
            return value is string version ? version : null;
        }
    }

    public string? ReadMediaState(Guid itemId, string mediaSourceId)
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = """
                SELECT state FROM media
                WHERE item_id = $item AND media_source_id = $source
                  AND generation = (SELECT generation FROM signatures WHERE active = 1)
                """;
            command.Parameters.AddWithValue("$item", itemId.ToString("D"));
            command.Parameters.AddWithValue("$source", mediaSourceId);
            return command.ExecuteScalar() as string;
        }
    }

    public int ActiveGeneration => ReadInt("active_generation");

    public void SetManualPause(bool paused) => WriteFlag("manual_pause", paused);

    public void SetImportedCandidatesOnly(bool enabled) => WriteFlag("imported_candidates_only", enabled);

    public void SetIncompatible(bool incompatible) => WriteFlag("incompatible", incompatible);

    public void SetQuality(double relatedMinimum, bool calibrated)
    {
        if (relatedMinimum is < -1 or > 1 || !double.IsFinite(relatedMinimum))
        {
            throw new ArgumentOutOfRangeException(nameof(relatedMinimum));
        }

        lock (_gate)
        {
            RequireWriter();
            using var tx = _connection.BeginTransaction();
            UpsertSetting("related_minimum", relatedMinimum.ToString(System.Globalization.CultureInfo.InvariantCulture), tx);
            UpsertSetting("threshold_calibrated", calibrated ? "1" : "0", tx);
            tx.Commit();
        }
    }

    public void SetEmbeddingBaseUrl(string url)
    {
        lock (_gate)
        {
            RequireWriter();
            using var tx = _connection.BeginTransaction();
            UpsertSetting("embedding_base_url", url, tx);
            tx.Commit();
        }
    }

    public void ReplaceLibraries(IReadOnlyList<Guid> libraryIds)
    {
        lock (_gate)
        {
            RequireWriter();
            using var tx = _connection.BeginTransaction();
            using (var delete = _connection.CreateCommand())
            {
                delete.Transaction = tx;
                delete.CommandText = "DELETE FROM libraries";
                delete.ExecuteNonQuery();
            }

            foreach (var id in libraryIds.Distinct())
            {
                using var insert = _connection.CreateCommand();
                insert.Transaction = tx;
                insert.CommandText = "INSERT INTO libraries (library_id) VALUES ($id)";
                insert.Parameters.AddWithValue("$id", id.ToString("D"));
                insert.ExecuteNonQuery();
            }

            tx.Commit();
        }
    }

    public IReadOnlyList<Guid> EnabledLibraryIds()
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = "SELECT library_id FROM libraries ORDER BY library_id";
            using var reader = command.ExecuteReader();
            var ids = new List<Guid>();
            while (reader.Read())
            {
                ids.Add(Guid.Parse(reader.GetString(0)));
            }

            return ids;
        }
    }

    public StoredSignature? ActiveSignature()
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = """
                SELECT generation, model, revision, processing_id, dimension, normalized,
                       input_version, window_version, audio_version
                FROM signatures WHERE active = 1
                """;
            using var reader = command.ExecuteReader();
            return reader.Read() ? ReadSignature(reader) : null;
        }
    }

    public int GenerationCount()
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = "SELECT COUNT(*) FROM signatures";
            return Convert.ToInt32(command.ExecuteScalar(), System.Globalization.CultureInfo.InvariantCulture);
        }
    }

    public int WindowCount()
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = "SELECT COUNT(*) FROM windows";
            return Convert.ToInt32(command.ExecuteScalar(), System.Globalization.CultureInfo.InvariantCulture);
        }
    }

    public void SaveSucceededWindow(
        Guid itemId,
        string mediaSourceId,
        Guid libraryId,
        string contentVersion,
        long runtimeTicks,
        long startTicks,
        long endTicks,
        float[] vector,
        string? privatePath)
    {
        if (!VectorMath.IsUnit(vector))
        {
            throw new InvalidDataException("Refusing to store a vector that is not a 768-d unit vector.");
        }

        var signature = ActiveSignature() ?? throw new InvalidOperationException("No active signature.");
        var segmentId = SegmentId(itemId, mediaSourceId, contentVersion, startTicks, endTicks, signature.Generation);
        lock (_gate)
        {
            RequireWriter();
            if (Incompatible)
            {
                throw new InvalidOperationException("Search is paused for an incompatible signature. Existing rows were kept.");
            }

            using var tx = _connection.BeginTransaction();
            using (var media = _connection.CreateCommand())
            {
                media.Transaction = tx;
                media.CommandText = """
                    INSERT INTO media (item_id, media_source_id, generation, library_id, content_version, runtime_ticks, state, source_path)
                    VALUES ($item, $source, $generation, $library, $version, $runtime, 'complete', $path)
                    ON CONFLICT (item_id, media_source_id, generation) DO UPDATE SET
                      library_id = excluded.library_id,
                      content_version = excluded.content_version,
                      runtime_ticks = excluded.runtime_ticks,
                      state = 'complete',
                      source_path = excluded.source_path
                    """;
                media.Parameters.AddWithValue("$item", itemId.ToString("D"));
                media.Parameters.AddWithValue("$source", mediaSourceId);
                media.Parameters.AddWithValue("$generation", signature.Generation);
                media.Parameters.AddWithValue("$library", libraryId.ToString("D"));
                media.Parameters.AddWithValue("$version", contentVersion);
                media.Parameters.AddWithValue("$runtime", runtimeTicks);
                media.Parameters.AddWithValue("$path", privatePath ?? "");
                media.ExecuteNonQuery();
            }

            using (var window = _connection.CreateCommand())
            {
                window.Transaction = tx;
                window.CommandText = """
                    INSERT INTO windows (segment_id, item_id, media_source_id, generation, content_version, start_ticks, end_ticks, state, vector)
                    VALUES ($segment, $item, $source, $generation, $version, $start, $end, 'succeeded', $vector)
                    ON CONFLICT (segment_id) DO UPDATE SET state = 'succeeded', vector = excluded.vector
                    """;
                window.Parameters.AddWithValue("$segment", segmentId.ToString("D"));
                window.Parameters.AddWithValue("$item", itemId.ToString("D"));
                window.Parameters.AddWithValue("$source", mediaSourceId);
                window.Parameters.AddWithValue("$generation", signature.Generation);
                window.Parameters.AddWithValue("$version", contentVersion);
                window.Parameters.AddWithValue("$start", startTicks);
                window.Parameters.AddWithValue("$end", endTicks);
                window.Parameters.AddWithValue("$vector", VectorMath.ToBlob(vector));
                window.ExecuteNonQuery();
            }

            tx.Commit();
        }
    }

    public void EnsureActiveSignature(ModelObservation observation)
    {
        if (!observation.IsExpectedModel || observation.ProcessingId is null)
        {
            throw new InvalidOperationException("Refusing to activate a signature the plugin does not expect.");
        }

        lock (_gate)
        {
            RequireWriter();
            var current = ActiveSignature();
            if (current is not null && SignaturesMatch(current, observation))
            {
                return;
            }

            if (current is not null)
            {
                throw new InvalidOperationException("An active signature already exists. Rebuild retains it instead of replacing it in place.");
            }

            using var tx = _connection.BeginTransaction();
            InsertSignature(tx, generation: 1, observation, active: true);
            UpsertSetting("active_generation", "1", tx);
            UpsertSetting("incompatible", "0", tx);
            tx.Commit();
        }
    }

    public int BeginRebuild(ModelObservation observation)
    {
        if (!observation.IsExpectedModel || string.IsNullOrWhiteSpace(observation.ProcessingId))
        {
            throw new InvalidOperationException("Refusing to rebuild onto an unexpected model.");
        }

        lock (_gate)
        {
            RequireWriter();
            var previousWindows = WindowCount();
            using var tx = _connection.BeginTransaction();
            using (var deactivate = _connection.CreateCommand())
            {
                deactivate.Transaction = tx;
                deactivate.CommandText = "UPDATE signatures SET active = 0";
                deactivate.ExecuteNonQuery();
            }

            var next = ReadInt("active_generation", tx) + 1;
            if (next < 1)
            {
                next = 1;
            }

            InsertSignature(tx, next, observation, active: true);
            UpsertSetting("active_generation", next.ToString(System.Globalization.CultureInfo.InvariantCulture), tx);
            UpsertSetting("incompatible", "0", tx);
            tx.Commit();
            if (WindowCount() < previousWindows)
            {
                throw new InvalidOperationException("Rebuild removed stored windows.");
            }

            return next;
        }
    }

    public IReadOnlyList<StoredWindow> LoadSearchable(IReadOnlyCollection<Guid> libraryIds, int? generation = null)
    {
        lock (_gate)
        {
            if (libraryIds.Count == 0 || Incompatible || SchemaReadOnly)
            {
                return [];
            }

            var parameters = new List<string>();
            using var command = _connection.CreateCommand();
            var index = 0;
            foreach (var id in libraryIds)
            {
                var name = "$lib" + index++;
                parameters.Add(name);
                command.Parameters.AddWithValue(name, id.ToString("D"));
            }

            command.Parameters.AddWithValue("$generation", generation ?? -1);
            command.CommandText = $"""
                SELECT w.segment_id, w.item_id, w.media_source_id, m.library_id, w.generation,
                       w.content_version, w.start_ticks, w.end_ticks, m.state, w.vector
                FROM windows w
                JOIN media m
                  ON m.item_id = w.item_id
                 AND m.media_source_id = w.media_source_id
                 AND m.generation = w.generation
                JOIN signatures s ON s.generation = w.generation
                WHERE w.state = 'succeeded'
                  AND w.content_version = m.content_version
                  AND m.state != 'hidden'
                  AND m.library_id IN ({string.Join(", ", parameters)})
                  AND (
                    ($generation >= 0 AND w.generation = $generation)
                    OR ($generation < 0 AND s.active = 1)
                  )
                """;
            using var reader = command.ExecuteReader();
            var rows = new List<StoredWindow>();
            while (reader.Read())
            {
                var blob = (byte[])reader.GetValue(9);
                rows.Add(new StoredWindow
                {
                    SegmentId = Guid.Parse(reader.GetString(0)),
                    ItemId = Guid.Parse(reader.GetString(1)),
                    MediaSourceId = reader.GetString(2),
                    LibraryId = Guid.Parse(reader.GetString(3)),
                    Generation = reader.GetInt32(4),
                    ContentVersion = reader.GetString(5),
                    StartTicks = reader.GetInt64(6),
                    EndTicks = reader.GetInt64(7),
                    MediaState = reader.GetString(8),
                    Vector = VectorMath.FromBlob(blob)
                });
            }

            return rows;
        }
    }

    public SegmentRow ReadSegment(Guid segmentId)
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = """
                SELECT w.segment_id, w.item_id, w.media_source_id, m.library_id, w.generation,
                       w.start_ticks, w.end_ticks, m.state, w.state, w.content_version, m.content_version,
                       CASE WHEN s.active = 1 THEN 1 ELSE 0 END
                FROM windows w
                JOIN media m
                  ON m.item_id = w.item_id
                 AND m.media_source_id = w.media_source_id
                 AND m.generation = w.generation
                LEFT JOIN signatures s ON s.generation = w.generation
                WHERE w.segment_id = $id
                """;
            command.Parameters.AddWithValue("$id", segmentId.ToString("D"));
            using var reader = command.ExecuteReader();
            if (!reader.Read())
            {
                return new SegmentRow();
            }

            var windowState = reader.GetString(8);
            var windowVersion = reader.GetString(9);
            var mediaVersion = reader.GetString(10);
            var active = reader.GetInt32(11) == 1;
            var mediaState = reader.GetString(7);
            var current = active
                && windowState == "succeeded"
                && mediaState != "hidden"
                && windowVersion == mediaVersion;
            return new SegmentRow
            {
                Found = true,
                IsCurrent = current,
                SegmentId = Guid.Parse(reader.GetString(0)),
                ItemId = Guid.Parse(reader.GetString(1)),
                MediaSourceId = reader.GetString(2),
                LibraryId = Guid.Parse(reader.GetString(3)),
                StartTicks = reader.GetInt64(5),
                EndTicks = reader.GetInt64(6),
                MediaState = mediaState,
                ContentVersion = windowVersion
            };
        }
    }

    public (int Videos, int Complete, int Partial, int Failed, int Waiting, int Searchable, bool IndexComplete) Coverage(IReadOnlyCollection<Guid> libraryIds)
    {
        var rows = LoadSearchable(libraryIds);
        lock (_gate)
        {
            if (libraryIds.Count == 0)
            {
                return (0, 0, 0, 0, 0, 0, false);
            }

            using var command = _connection.CreateCommand();
            var names = new List<string>();
            var index = 0;
            foreach (var id in libraryIds)
            {
                var name = "$c" + index++;
                names.Add(name);
                command.Parameters.AddWithValue(name, id.ToString("D"));
            }

            command.CommandText = $"""
                SELECT state, COUNT(*)
                FROM media
                WHERE generation = (SELECT generation FROM signatures WHERE active = 1)
                  AND library_id IN ({string.Join(", ", names)})
                GROUP BY state
                """;
            var counts = new Dictionary<string, int>(StringComparer.Ordinal);
            using (var reader = command.ExecuteReader())
            {
                while (reader.Read())
                {
                    counts[reader.GetString(0)] = reader.GetInt32(1);
                }
            }

            var complete = counts.GetValueOrDefault("complete");
            var partial = counts.GetValueOrDefault("partial");
            var failed = counts.GetValueOrDefault("failed");
            var waiting = counts.GetValueOrDefault("waiting");
            var hidden = counts.GetValueOrDefault("hidden");
            var total = complete + partial + failed + waiting + counts.GetValueOrDefault("unknown");
            var searchableVideos = rows.Select(row => row.ItemId).Distinct().Count();
            var indexComplete = total > 0 && complete == total && hidden == 0 && partial == 0 && failed == 0 && waiting == 0;
            return (total, complete, partial, failed, waiting, rows.Count, indexComplete && searchableVideos == total);
        }
    }

    public string CreateConsistentBackup()
    {
        lock (_gate)
        {
            var backupPath = Path.Combine(Path.GetDirectoryName(DatabasePath)!, "backups", "index-" + DateTime.UtcNow.ToString("yyyyMMddTHHmmssfffZ", System.Globalization.CultureInfo.InvariantCulture) + ".db");
            Directory.CreateDirectory(Path.GetDirectoryName(backupPath)!);
            using var destination = new SqliteConnection(new SqliteConnectionStringBuilder
            {
                DataSource = backupPath,
                Mode = SqliteOpenMode.ReadWriteCreate,
                Pooling = false
            }.ConnectionString);
            destination.Open();
            _connection.BackupDatabase(destination);
            return backupPath;
        }
    }

    public static Guid SegmentId(Guid itemId, string mediaSourceId, string contentVersion, long startTicks, long endTicks, int generation)
    {
        var text = $"{itemId:N}|{mediaSourceId}|{contentVersion}|{startTicks}|{endTicks}|{generation}";
        var hash = System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(text));
        hash[7] = (byte)((hash[7] & 0x0F) | 0x50);
        hash[8] = (byte)((hash[8] & 0x3F) | 0x80);
        return new Guid(hash.AsSpan(0, 16));
    }

    public static bool SignaturesMatch(StoredSignature stored, ModelObservation observation)
        => observation.Ready
           && observation.IsExpectedModel
           && stored.Model == observation.Model
           && stored.Revision == observation.Revision
           && stored.ProcessingId == observation.ProcessingId
           && stored.Dimension == observation.Dimension
           && stored.Normalized
           && observation.Normalized
           && stored.InputVersion == ApiV1.InputVersion
           && stored.WindowVersion == ApiV1.WindowVersion
           && stored.AudioVersion == ApiV1.AudioVersion;

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        _connection.Dispose();
        _writerLock?.Dispose();
        if (_writerLockPath is not null)
        {
            lock (ProcessLocks)
            {
                HeldLocks.Remove(Path.GetFullPath(_writerLockPath));
            }
        }
    }

    private void EnsureSchema(int version)
    {
        if (version == CurrentSchemaVersion)
        {
            return;
        }

        if (version == 1)
        {
            CreateConsistentBackup();
            using var upgrade = _connection.BeginTransaction();
            using var alter = _connection.CreateCommand();
            alter.Transaction = upgrade;
            alter.CommandText = """
                ALTER TABLE windows ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE windows ADD COLUMN lease_token TEXT;
                ALTER TABLE windows ADD COLUMN lease_until TEXT;
                ALTER TABLE windows ADD COLUMN failure_reason TEXT;
                ALTER TABLE media ADD COLUMN has_audio INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE media ADD COLUMN audio_stream_index INTEGER;
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  kind TEXT NOT NULL,
                  item_id TEXT,
                  created_utc TEXT NOT NULL,
                  handled INTEGER NOT NULL
                );
                PRAGMA user_version = 3;
                """;
            alter.ExecuteNonQuery();
            upgrade.Commit();
            return;
        }

        if (version == 2)
        {
            CreateConsistentBackup();
            using var upgrade = _connection.BeginTransaction();
            using var alter = _connection.CreateCommand();
            alter.Transaction = upgrade;
            alter.CommandText = """
                ALTER TABLE media ADD COLUMN audio_stream_index INTEGER;
                PRAGMA user_version = 3;
                """;
            alter.ExecuteNonQuery();
            upgrade.Commit();
            return;
        }

        if (version != 0)
        {
            throw new InvalidDataException("Refusing to migrate an unrecognized older index.");
        }

        using var tx = _connection.BeginTransaction();
        using var command = _connection.CreateCommand();
        command.Transaction = tx;
        command.CommandText = """
            CREATE TABLE settings (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE libraries (
              library_id TEXT PRIMARY KEY
            );
            CREATE TABLE signatures (
              generation INTEGER PRIMARY KEY,
              model TEXT NOT NULL,
              revision TEXT NOT NULL,
              processing_id TEXT NOT NULL,
              dimension INTEGER NOT NULL,
              normalized INTEGER NOT NULL,
              input_version TEXT NOT NULL,
              window_version TEXT NOT NULL,
              audio_version TEXT NOT NULL,
              active INTEGER NOT NULL
            );
            CREATE TABLE media (
              item_id TEXT NOT NULL,
              media_source_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              library_id TEXT NOT NULL,
              content_version TEXT NOT NULL,
              runtime_ticks INTEGER NOT NULL,
              state TEXT NOT NULL,
              source_path TEXT NOT NULL DEFAULT '',
              has_audio INTEGER NOT NULL DEFAULT 0,
              audio_stream_index INTEGER,
              PRIMARY KEY (item_id, media_source_id, generation)
            );
            CREATE TABLE windows (
              segment_id TEXT PRIMARY KEY,
              item_id TEXT NOT NULL,
              media_source_id TEXT NOT NULL,
              generation INTEGER NOT NULL,
              content_version TEXT NOT NULL,
              start_ticks INTEGER NOT NULL,
              end_ticks INTEGER NOT NULL,
              state TEXT NOT NULL,
              vector BLOB,
              attempt INTEGER NOT NULL DEFAULT 0,
              lease_token TEXT,
              lease_until TEXT,
              failure_reason TEXT
            );
            CREATE TABLE events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kind TEXT NOT NULL,
              item_id TEXT,
              created_utc TEXT NOT NULL,
              handled INTEGER NOT NULL
            );
            """;
        command.ExecuteNonQuery();
        UpsertSetting("manual_pause", "0", tx);
        UpsertSetting("incompatible", "0", tx);
        UpsertSetting("related_minimum", ApiV1.DefaultRelatedMinimum.ToString(System.Globalization.CultureInfo.InvariantCulture), tx);
        UpsertSetting("threshold_calibrated", "0", tx);
        UpsertSetting("embedding_base_url", "", tx);
        UpsertSetting("active_generation", "0", tx);
        using var versionCommand = _connection.CreateCommand();
        versionCommand.Transaction = tx;
        versionCommand.CommandText = "PRAGMA user_version = 3";
        versionCommand.ExecuteNonQuery();
        tx.Commit();
    }

    private void InsertSignature(SqliteTransaction tx, int generation, ModelObservation observation, bool active)
    {
        using var command = _connection.CreateCommand();
        command.Transaction = tx;
        command.CommandText = """
            INSERT INTO signatures (
              generation, model, revision, processing_id, dimension, normalized,
              input_version, window_version, audio_version, active)
            VALUES ($generation, $model, $revision, $processing, $dimension, $normalized,
                    $input, $window, $audio, $active)
            """;
        command.Parameters.AddWithValue("$generation", generation);
        command.Parameters.AddWithValue("$model", observation.Model);
        command.Parameters.AddWithValue("$revision", observation.Revision);
        command.Parameters.AddWithValue("$processing", observation.ProcessingId);
        command.Parameters.AddWithValue("$dimension", observation.Dimension);
        command.Parameters.AddWithValue("$normalized", observation.Normalized ? 1 : 0);
        command.Parameters.AddWithValue("$input", ApiV1.InputVersion);
        command.Parameters.AddWithValue("$window", ApiV1.WindowVersion);
        command.Parameters.AddWithValue("$audio", ApiV1.AudioVersion);
        command.Parameters.AddWithValue("$active", active ? 1 : 0);
        command.ExecuteNonQuery();
    }

    private static StoredSignature ReadSignature(SqliteDataReader reader)
        => new()
        {
            Generation = reader.GetInt32(0),
            Model = reader.GetString(1),
            Revision = reader.GetString(2),
            ProcessingId = reader.GetString(3),
            Dimension = reader.GetInt32(4),
            Normalized = reader.GetInt32(5) == 1,
            InputVersion = reader.GetString(6),
            WindowVersion = reader.GetString(7),
            AudioVersion = reader.GetString(8)
        };

    private void RequireWriter()
    {
        if (SchemaReadOnly)
        {
            throw new InvalidOperationException("Index schema is newer than this plugin. Writes are refused.");
        }

        if (!HasWriterLock)
        {
            throw new InvalidOperationException("This process does not hold the index writer lock.");
        }
    }

    private void WriteFlag(string key, bool value)
    {
        lock (_gate)
        {
            RequireWriter();
            using var tx = _connection.BeginTransaction();
            UpsertSetting(key, value ? "1" : "0", tx);
            tx.Commit();
        }
    }

    private bool ReadFlag(string key)
    {
        lock (_gate)
        {
            return ReadString(key) == "1";
        }
    }

    private int ReadInt(string key, SqliteTransaction? tx = null)
    {
        var text = ReadString(key, tx);
        return int.TryParse(text, System.Globalization.NumberStyles.Integer, System.Globalization.CultureInfo.InvariantCulture, out var value) ? value : 0;
    }

    private double ReadDouble(string key, double fallback)
    {
        lock (_gate)
        {
            var text = ReadString(key);
            return double.TryParse(text, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var value) ? value : fallback;
        }
    }

    private string ReadString(string key, SqliteTransaction? tx = null)
    {
        using var command = _connection.CreateCommand();
        command.Transaction = tx;
        command.CommandText = "SELECT value FROM settings WHERE key = $key";
        command.Parameters.AddWithValue("$key", key);
        return command.ExecuteScalar() as string ?? "";
    }

    private void UpsertSetting(string key, string value, SqliteTransaction tx)
    {
        using var command = _connection.CreateCommand();
        command.Transaction = tx;
        command.CommandText = """
            INSERT INTO settings (key, value) VALUES ($key, $value)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """;
        command.Parameters.AddWithValue("$key", key);
        command.Parameters.AddWithValue("$value", value);
        command.ExecuteNonQuery();
    }

    private static int ReadUserVersion(SqliteConnection connection)
    {
        using var command = connection.CreateCommand();
        command.CommandText = "PRAGMA user_version";
        return Convert.ToInt32(command.ExecuteScalar(), System.Globalization.CultureInfo.InvariantCulture);
    }

    private static FileStream? TryLock(string path)
    {
        var full = Path.GetFullPath(path);
        lock (ProcessLocks)
        {
            if (!HeldLocks.Add(full))
            {
                return null;
            }
        }

        try
        {
            return new FileStream(full, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        }
        catch (IOException)
        {
            lock (ProcessLocks)
            {
                HeldLocks.Remove(full);
            }

            return null;
        }
    }
}
