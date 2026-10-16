namespace Jellyfin.Plugin.EmbeddingVideoSearch;

public enum LeaseOutcome
{
    Published,
    Stale
}

public sealed class LeaseTicket
{
    public string Token { get; init; } = "";
    public Guid SegmentId { get; init; }
    public Guid ItemId { get; init; }
    public string MediaSourceId { get; init; } = "";
    public int Generation { get; init; }
    public string ContentVersion { get; init; } = "";
    public long StartTicks { get; init; }
    public long EndTicks { get; init; }
    public string SourcePath { get; init; } = "";
    public bool HasAudio { get; init; }
    public int? AudioStreamIndex { get; init; }
    public int Attempt { get; init; }
}

public sealed partial class IndexStore
{
    public void RequestReconcile() => WriteFlag("reconcile", true);

    public bool ConsumeReconcile()
    {
        lock (_gate)
        {
            if (ReadString("reconcile") != "1")
            {
                return false;
            }

            RequireWriter();
            using var tx = _connection.BeginTransaction();
            UpsertSetting("reconcile", "0", tx);
            tx.Commit();
            return true;
        }
    }

    public void Enqueue(string kind, Guid? itemId)
    {
        lock (_gate)
        {
            RequireWriter();
            using var command = _connection.CreateCommand();
            command.CommandText = """
                INSERT INTO events (kind, item_id, created_utc, handled)
                VALUES ($kind, $item, $created, 0)
                """;
            command.Parameters.AddWithValue("$kind", kind);
            command.Parameters.AddWithValue("$item", itemId?.ToString("D") ?? "");
            command.Parameters.AddWithValue("$created", DateTimeOffset.UtcNow.ToString("o"));
            command.ExecuteNonQuery();
        }
    }

    public IReadOnlyList<IndexEvent> TakeEvents(int max)
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = """
                SELECT id, kind, item_id FROM events
                WHERE handled = 0
                ORDER BY id
                LIMIT $max
                """;
            command.Parameters.AddWithValue("$max", max);
            using var reader = command.ExecuteReader();
            var events = new List<IndexEvent>();
            while (reader.Read())
            {
                var item = reader.GetString(2);
                events.Add(new IndexEvent(reader.GetInt64(0), reader.GetString(1), Guid.TryParse(item, out var id) ? id : null));
            }

            return events;
        }
    }

    public void CompleteEvent(long id)
    {
        lock (_gate)
        {
            RequireWriter();
            using var command = _connection.CreateCommand();
            command.CommandText = "UPDATE events SET handled = 1 WHERE id = $id";
            command.Parameters.AddWithValue("$id", id);
            command.ExecuteNonQuery();
        }
    }

    public void UpsertVideo(Guid itemId, string mediaSourceId, Guid libraryId, long runtimeTicks, string contentVersion, string sourcePath, bool hasAudio, int? audioStreamIndex = null)
    {
        lock (_gate)
        {
            RequireWriter();
            var signature = ActiveSignature();
            if (signature is null)
            {
                return;
            }

            var planned = WindowPlanner.Plan(runtimeTicks);
            using var tx = _connection.BeginTransaction();
            var existing = ReadMedia(tx, itemId, mediaSourceId, signature.Generation);
            if (runtimeTicks <= 0 || !WindowPlanner.Covers(runtimeTicks, planned))
            {
                WriteMedia(tx, itemId, mediaSourceId, signature.Generation, libraryId, contentVersion, runtimeTicks, "failed", sourcePath, hasAudio, audioStreamIndex);
                tx.Commit();
                return;
            }

            if (existing is null || existing.Value.Version != contentVersion || existing.Value.State == "hidden")
            {
                WriteMedia(tx, itemId, mediaSourceId, signature.Generation, libraryId, contentVersion, runtimeTicks, "waiting", sourcePath, hasAudio, audioStreamIndex);
                SupersedeOtherVersions(tx, itemId, mediaSourceId, signature.Generation, contentVersion);
                InsertPending(tx, itemId, mediaSourceId, signature.Generation, contentVersion, planned);
            }
            else
            {
                WriteMedia(tx, itemId, mediaSourceId, signature.Generation, libraryId, contentVersion, runtimeTicks, existing.Value.State, sourcePath, hasAudio, audioStreamIndex);
                InsertPending(tx, itemId, mediaSourceId, signature.Generation, contentVersion, planned);
                Rollup(tx, itemId, mediaSourceId, signature.Generation, contentVersion);
            }

            tx.Commit();
        }
    }

    public void HideItem(Guid itemId, string? mediaSourceId = null)
    {
        lock (_gate)
        {
            RequireWriter();
            var signature = ActiveSignature();
            if (signature is null)
            {
                return;
            }

            using var tx = _connection.BeginTransaction();
            using var media = _connection.CreateCommand();
            media.Transaction = tx;
            media.CommandText = """
                UPDATE media SET state = 'hidden'
                WHERE item_id = $item AND generation = $generation
                  AND ($source = '' OR media_source_id = $source)
                """;
            media.Parameters.AddWithValue("$item", itemId.ToString("D"));
            media.Parameters.AddWithValue("$generation", signature.Generation);
            media.Parameters.AddWithValue("$source", mediaSourceId ?? "");
            media.ExecuteNonQuery();
            using var windows = _connection.CreateCommand();
            windows.Transaction = tx;
            windows.CommandText = """
                UPDATE windows SET state = 'superseded', lease_token = NULL
                WHERE item_id = $item AND generation = $generation
                  AND ($source = '' OR media_source_id = $source)
                  AND state != 'superseded'
                """;
            windows.Parameters.AddWithValue("$item", itemId.ToString("D"));
            windows.Parameters.AddWithValue("$generation", signature.Generation);
            windows.Parameters.AddWithValue("$source", mediaSourceId ?? "");
            windows.ExecuteNonQuery();
            tx.Commit();
        }
    }

    public void MarkUnknown(Guid itemId)
    {
        lock (_gate)
        {
            RequireWriter();
            var signature = ActiveSignature();
            if (signature is null)
            {
                return;
            }

            using var command = _connection.CreateCommand();
            command.CommandText = """
                UPDATE media SET state = 'unknown'
                WHERE item_id = $item AND generation = $generation AND state != 'hidden'
                """;
            command.Parameters.AddWithValue("$item", itemId.ToString("D"));
            command.Parameters.AddWithValue("$generation", signature.Generation);
            command.ExecuteNonQuery();
        }
    }

    public void HideMissing(IReadOnlyCollection<Guid> libraries, IReadOnlySet<(Guid ItemId, string MediaSourceId)> seen, IReadOnlySet<Guid> unreadable)
    {
        lock (_gate)
        {
            RequireWriter();
            var signature = ActiveSignature();
            if (signature is null || libraries.Count == 0)
            {
                return;
            }

            using var command = _connection.CreateCommand();
            var names = new List<string>();
            var index = 0;
            foreach (var library in libraries)
            {
                var name = "$h" + index++;
                names.Add(name);
                command.Parameters.AddWithValue(name, library.ToString("D"));
            }

            command.CommandText = $"""
                SELECT item_id, media_source_id FROM media
                WHERE generation = $generation AND state != 'hidden'
                  AND library_id IN ({string.Join(", ", names)})
                """;
            command.Parameters.AddWithValue("$generation", signature.Generation);
            using var reader = command.ExecuteReader();
            var hide = new List<(string Item, string Source)>();
            while (reader.Read())
            {
                var item = Guid.Parse(reader.GetString(0));
                var source = reader.GetString(1);
                if (unreadable.Contains(item) || seen.Contains((item, source)))
                {
                    continue;
                }

                hide.Add((reader.GetString(0), source));
            }

            reader.Close();
            foreach (var row in hide)
            {
                HideItem(Guid.Parse(row.Item), row.Source);
            }
        }
    }

    public void RecoverLeases()
    {
        lock (_gate)
        {
            RequireWriter();
            using var command = _connection.CreateCommand();
            command.CommandText = """
                UPDATE windows SET state = 'pending', lease_token = NULL, lease_until = NULL
                WHERE state = 'leased'
                """;
            command.ExecuteNonQuery();
        }
    }

    public LeaseTicket? TryLease()
    {
        lock (_gate)
        {
            RequireWriter();
            if (Incompatible || ManualPause)
            {
                return null;
            }

            var signature = ActiveSignature();
            if (signature is null)
            {
                return null;
            }

            RecoverExpired(signature.Generation);
            using var busy = _connection.CreateCommand();
            busy.CommandText = "SELECT COUNT(*) FROM windows WHERE state = 'leased' AND generation = $generation";
            busy.Parameters.AddWithValue("$generation", signature.Generation);
            if (Convert.ToInt32(busy.ExecuteScalar(), System.Globalization.CultureInfo.InvariantCulture) > 0)
            {
                return null;
            }

            using var select = _connection.CreateCommand();
            select.CommandText = """
                SELECT w.segment_id, w.item_id, w.media_source_id, w.content_version, w.start_ticks, w.end_ticks,
                       m.source_path, m.has_audio, w.attempt, m.audio_stream_index
                FROM windows w
                JOIN media m
                  ON m.item_id = w.item_id AND m.media_source_id = w.media_source_id AND m.generation = w.generation
                WHERE w.generation = $generation AND w.state = 'pending'
                  AND w.content_version = m.content_version
                  AND m.state NOT IN ('hidden', 'unknown')
                ORDER BY w.start_ticks
                LIMIT 1
                """;
            select.Parameters.AddWithValue("$generation", signature.Generation);
            using var reader = select.ExecuteReader();
            if (!reader.Read())
            {
                return null;
            }

            var ticket = new LeaseTicket
            {
                Token = Guid.NewGuid().ToString("N"),
                SegmentId = Guid.Parse(reader.GetString(0)),
                ItemId = Guid.Parse(reader.GetString(1)),
                MediaSourceId = reader.GetString(2),
                Generation = signature.Generation,
                ContentVersion = reader.GetString(3),
                StartTicks = reader.GetInt64(4),
                EndTicks = reader.GetInt64(5),
                SourcePath = reader.GetString(6),
                HasAudio = reader.GetInt32(7) == 1,
                Attempt = reader.GetInt32(8),
                AudioStreamIndex = reader.IsDBNull(9) ? null : reader.GetInt32(9)
            };
            reader.Close();
            using var update = _connection.CreateCommand();
            update.CommandText = """
                UPDATE windows
                SET state = 'leased', attempt = $attempt, lease_token = $token, lease_until = $until, failure_reason = NULL
                WHERE segment_id = $id AND state = 'pending'
                """;
            update.Parameters.AddWithValue("$attempt", ticket.Attempt);
            update.Parameters.AddWithValue("$token", ticket.Token);
            update.Parameters.AddWithValue("$until", DateTimeOffset.UtcNow.AddMinutes(10).ToString("o"));
            update.Parameters.AddWithValue("$id", ticket.SegmentId.ToString("D"));
            return update.ExecuteNonQuery() == 1 ? ticket : null;
        }
    }

    public void ExpireLease(string token)
    {
        lock (_gate)
        {
            RequireWriter();
            using var command = _connection.CreateCommand();
            command.CommandText = "UPDATE windows SET lease_until = $until WHERE lease_token = $token";
            command.Parameters.AddWithValue("$until", DateTimeOffset.UnixEpoch.ToString("o"));
            command.Parameters.AddWithValue("$token", token);
            command.ExecuteNonQuery();
        }
    }

    public LeaseOutcome CompleteLease(string token, float[] vector)
    {
        if (!VectorMath.IsUnit(vector))
        {
            throw new InvalidDataException("Refusing to store a vector that is not a 768-d unit vector.");
        }

        lock (_gate)
        {
            RequireWriter();
            using var tx = _connection.BeginTransaction();
            using var select = _connection.CreateCommand();
            select.Transaction = tx;
            select.CommandText = """
                SELECT w.item_id, w.media_source_id, w.generation, w.content_version, m.content_version, m.state, s.active
                FROM windows w
                JOIN media m ON m.item_id = w.item_id AND m.media_source_id = w.media_source_id AND m.generation = w.generation
                LEFT JOIN signatures s ON s.generation = w.generation
                WHERE w.lease_token = $token AND w.state = 'leased'
                """;
            select.Parameters.AddWithValue("$token", token);
            using var reader = select.ExecuteReader();
            if (!reader.Read())
            {
                return LeaseOutcome.Stale;
            }

            var item = reader.GetString(0);
            var source = reader.GetString(1);
            var generation = reader.GetInt32(2);
            var windowVersion = reader.GetString(3);
            var mediaVersion = reader.GetString(4);
            var mediaState = reader.GetString(5);
            var active = !reader.IsDBNull(6) && reader.GetInt32(6) == 1;
            reader.Close();
            if (!active || windowVersion != mediaVersion || mediaState is "hidden" or "unknown")
            {
                using var drop = _connection.CreateCommand();
                drop.Transaction = tx;
                drop.CommandText = """
                    UPDATE windows SET state = 'superseded', lease_token = NULL, lease_until = NULL
                    WHERE lease_token = $token
                    """;
                drop.Parameters.AddWithValue("$token", token);
                drop.ExecuteNonQuery();
                tx.Commit();
                return LeaseOutcome.Stale;
            }

            using var update = _connection.CreateCommand();
            update.Transaction = tx;
            update.CommandText = """
                UPDATE windows SET state = 'succeeded', vector = $vector, lease_token = NULL, lease_until = NULL
                WHERE lease_token = $token AND state = 'leased'
                """;
            update.Parameters.AddWithValue("$vector", VectorMath.ToBlob(vector));
            update.Parameters.AddWithValue("$token", token);
            if (update.ExecuteNonQuery() != 1)
            {
                return LeaseOutcome.Stale;
            }

            Rollup(tx, Guid.Parse(item), source, generation, mediaVersion);
            tx.Commit();
            return LeaseOutcome.Published;
        }
    }

    public void FailLease(string token, string reason, bool retryable)
    {
        lock (_gate)
        {
            RequireWriter();
            using var tx = _connection.BeginTransaction();
            using var select = _connection.CreateCommand();
            select.Transaction = tx;
            select.CommandText = """
                SELECT item_id, media_source_id, generation, content_version, attempt
                FROM windows WHERE lease_token = $token AND state = 'leased'
                """;
            select.Parameters.AddWithValue("$token", token);
            using var reader = select.ExecuteReader();
            if (!reader.Read())
            {
                return;
            }

            var item = reader.GetString(0);
            var source = reader.GetString(1);
            var generation = reader.GetInt32(2);
            var version = reader.GetString(3);
            var attempt = reader.GetInt32(4);
            reader.Close();
            var retry = retryable && attempt < 5;
            using var update = _connection.CreateCommand();
            update.Transaction = tx;
            update.CommandText = """
                UPDATE windows
                SET state = $state, failure_reason = $reason, lease_token = NULL, lease_until = NULL
                WHERE lease_token = $token
                """;
            update.Parameters.AddWithValue("$state", retry ? "pending" : "failed");
            update.Parameters.AddWithValue("$reason", reason);
            update.Parameters.AddWithValue("$token", token);
            update.ExecuteNonQuery();
            Rollup(tx, Guid.Parse(item), source, generation, version);
            tx.Commit();
        }
    }

    public void ReleaseLease(string token)
    {
        lock (_gate)
        {
            RequireWriter();
            using var command = _connection.CreateCommand();
            command.CommandText = """
                UPDATE windows SET state = 'pending', lease_token = NULL, lease_until = NULL
                WHERE lease_token = $token AND state = 'leased'
                """;
            command.Parameters.AddWithValue("$token", token);
            command.ExecuteNonQuery();
        }
    }

    public int CountWindows(string state)
    {
        lock (_gate)
        {
            using var command = _connection.CreateCommand();
            command.CommandText = "SELECT COUNT(*) FROM windows WHERE state = $state";
            command.Parameters.AddWithValue("$state", state);
            return Convert.ToInt32(command.ExecuteScalar(), System.Globalization.CultureInfo.InvariantCulture);
        }
    }

    private void RecoverExpired(int generation)
    {
        using var select = _connection.CreateCommand();
        select.CommandText = "SELECT segment_id, lease_until FROM windows WHERE state = 'leased' AND generation = $generation";
        select.Parameters.AddWithValue("$generation", generation);
        using var reader = select.ExecuteReader();
        var expired = new List<string>();
        while (reader.Read())
        {
            var until = reader.IsDBNull(1) ? "" : reader.GetString(1);
            if (!DateTimeOffset.TryParse(until, System.Globalization.CultureInfo.InvariantCulture, System.Globalization.DateTimeStyles.RoundtripKind, out var parsed) || parsed <= DateTimeOffset.UtcNow)
            {
                expired.Add(reader.GetString(0));
            }
        }

        reader.Close();
        foreach (var id in expired)
        {
            using var update = _connection.CreateCommand();
            update.CommandText = """
                UPDATE windows SET state = 'pending', lease_token = NULL, lease_until = NULL
                WHERE segment_id = $id AND state = 'leased'
                """;
            update.Parameters.AddWithValue("$id", id);
            update.ExecuteNonQuery();
        }
    }

    private (string Version, string State)? ReadMedia(Microsoft.Data.Sqlite.SqliteTransaction tx, Guid itemId, string mediaSourceId, int generation)
    {
        using var command = _connection.CreateCommand();
        command.Transaction = tx;
        command.CommandText = """
            SELECT content_version, state FROM media
            WHERE item_id = $item AND media_source_id = $source AND generation = $generation
            """;
        command.Parameters.AddWithValue("$item", itemId.ToString("D"));
        command.Parameters.AddWithValue("$source", mediaSourceId);
        command.Parameters.AddWithValue("$generation", generation);
        using var reader = command.ExecuteReader();
        return reader.Read() ? (reader.GetString(0), reader.GetString(1)) : null;
    }

    private void WriteMedia(Microsoft.Data.Sqlite.SqliteTransaction tx, Guid itemId, string mediaSourceId, int generation, Guid libraryId, string contentVersion, long runtimeTicks, string state, string sourcePath, bool hasAudio, int? audioStreamIndex)
    {
        using var command = _connection.CreateCommand();
        command.Transaction = tx;
        command.CommandText = """
            INSERT INTO media (item_id, media_source_id, generation, library_id, content_version, runtime_ticks, state, source_path, has_audio, audio_stream_index)
            VALUES ($item, $source, $generation, $library, $version, $runtime, $state, $path, $audio, $index)
            ON CONFLICT (item_id, media_source_id, generation) DO UPDATE SET
              library_id = excluded.library_id,
              content_version = excluded.content_version,
              runtime_ticks = excluded.runtime_ticks,
              state = excluded.state,
              source_path = excluded.source_path,
              has_audio = excluded.has_audio,
              audio_stream_index = excluded.audio_stream_index
            """;
        command.Parameters.AddWithValue("$item", itemId.ToString("D"));
        command.Parameters.AddWithValue("$source", mediaSourceId);
        command.Parameters.AddWithValue("$generation", generation);
        command.Parameters.AddWithValue("$library", libraryId.ToString("D"));
        command.Parameters.AddWithValue("$version", contentVersion);
        command.Parameters.AddWithValue("$runtime", runtimeTicks);
        command.Parameters.AddWithValue("$state", state);
        command.Parameters.AddWithValue("$path", sourcePath);
        command.Parameters.AddWithValue("$audio", hasAudio ? 1 : 0);
        command.Parameters.AddWithValue("$index", audioStreamIndex.HasValue ? audioStreamIndex.Value : DBNull.Value);
        command.ExecuteNonQuery();
    }

    private void SupersedeOtherVersions(Microsoft.Data.Sqlite.SqliteTransaction tx, Guid itemId, string mediaSourceId, int generation, string contentVersion)
    {
        using var command = _connection.CreateCommand();
        command.Transaction = tx;
        command.CommandText = """
            UPDATE windows SET state = 'superseded', lease_token = NULL, lease_until = NULL
            WHERE item_id = $item AND media_source_id = $source AND generation = $generation
              AND content_version != $version AND state != 'superseded'
            """;
        command.Parameters.AddWithValue("$item", itemId.ToString("D"));
        command.Parameters.AddWithValue("$source", mediaSourceId);
        command.Parameters.AddWithValue("$generation", generation);
        command.Parameters.AddWithValue("$version", contentVersion);
        command.ExecuteNonQuery();
    }

    private void InsertPending(Microsoft.Data.Sqlite.SqliteTransaction tx, Guid itemId, string mediaSourceId, int generation, string contentVersion, IReadOnlyList<WindowSpan> windows)
    {
        foreach (var window in windows)
        {
            var segment = SegmentId(itemId, mediaSourceId, contentVersion, window.StartTicks, window.EndTicks, generation);
            using var command = _connection.CreateCommand();
            command.Transaction = tx;
            command.CommandText = """
                INSERT OR IGNORE INTO windows (
                  segment_id, item_id, media_source_id, generation, content_version, start_ticks, end_ticks, state, attempt)
                VALUES ($segment, $item, $source, $generation, $version, $start, $end, 'pending', 0)
                """;
            command.Parameters.AddWithValue("$segment", segment.ToString("D"));
            command.Parameters.AddWithValue("$item", itemId.ToString("D"));
            command.Parameters.AddWithValue("$source", mediaSourceId);
            command.Parameters.AddWithValue("$generation", generation);
            command.Parameters.AddWithValue("$version", contentVersion);
            command.Parameters.AddWithValue("$start", window.StartTicks);
            command.Parameters.AddWithValue("$end", window.EndTicks);
            command.ExecuteNonQuery();
        }
    }

    public IReadOnlyList<FailedWindowRef> ListFailedForRetry()
    {
        lock (_gate)
        {
            RequireWriter();
            var libraries = EnabledLibraryIds();
            if (libraries.Count == 0)
            {
                return [];
            }

            using var command = _connection.CreateCommand();
            var names = new List<string>();
            var index = 0;
            foreach (var library in libraries)
            {
                var name = "$lib" + index++;
                names.Add(name);
                command.Parameters.AddWithValue(name, library.ToString("D"));
            }

            command.CommandText = $"""
                SELECT DISTINCT w.item_id, w.media_source_id, m.library_id, w.content_version
                FROM windows w
                JOIN media m
                  ON m.item_id = w.item_id AND m.media_source_id = w.media_source_id AND m.generation = w.generation
                WHERE w.generation = (SELECT generation FROM signatures WHERE active = 1)
                  AND w.state = 'failed'
                  AND w.content_version = m.content_version
                  AND m.state NOT IN ('hidden', 'unknown')
                  AND m.library_id IN ({string.Join(", ", names)})
                """;
            using var reader = command.ExecuteReader();
            var rows = new List<FailedWindowRef>();
            while (reader.Read())
            {
                rows.Add(new FailedWindowRef(
                    Guid.Parse(reader.GetString(0)),
                    reader.GetString(1),
                    Guid.Parse(reader.GetString(2)),
                    reader.GetString(3)));
            }

            return rows;
        }
    }

    public int ResetFailed(Guid itemId, string mediaSourceId, string contentVersion)
    {
        lock (_gate)
        {
            RequireWriter();
            var generation = ActiveGeneration;
            using var tx = _connection.BeginTransaction();
            using var update = _connection.CreateCommand();
            update.Transaction = tx;
            update.CommandText = """
                UPDATE windows
                SET state = 'pending', attempt = 0, lease_token = NULL, lease_until = NULL, failure_reason = NULL
                WHERE item_id = $item AND media_source_id = $source
                  AND generation = (SELECT generation FROM signatures WHERE active = 1)
                  AND content_version = $version AND state = 'failed'
                """;
            update.Parameters.AddWithValue("$item", itemId.ToString("D"));
            update.Parameters.AddWithValue("$source", mediaSourceId);
            update.Parameters.AddWithValue("$version", contentVersion);
            var reset = update.ExecuteNonQuery();
            if (reset > 0)
            {
                Rollup(tx, itemId, mediaSourceId, generation, contentVersion);
            }

            tx.Commit();
            return reset;
        }
    }

    private void Rollup(Microsoft.Data.Sqlite.SqliteTransaction tx, Guid itemId, string mediaSourceId, int generation, string contentVersion)
    {
        using var count = _connection.CreateCommand();
        count.Transaction = tx;
        count.CommandText = """
            SELECT state, COUNT(*) FROM windows
            WHERE item_id = $item AND media_source_id = $source AND generation = $generation
              AND content_version = $version AND state != 'superseded'
            GROUP BY state
            """;
        count.Parameters.AddWithValue("$item", itemId.ToString("D"));
        count.Parameters.AddWithValue("$source", mediaSourceId);
        count.Parameters.AddWithValue("$generation", generation);
        count.Parameters.AddWithValue("$version", contentVersion);
        var counts = new Dictionary<string, int>(StringComparer.Ordinal);
        using (var reader = count.ExecuteReader())
        {
            while (reader.Read())
            {
                counts[reader.GetString(0)] = reader.GetInt32(1);
            }
        }

        var succeeded = counts.GetValueOrDefault("succeeded");
        var pending = counts.GetValueOrDefault("pending");
        var leased = counts.GetValueOrDefault("leased");
        var failed = counts.GetValueOrDefault("failed");
        var total = succeeded + pending + leased + failed;
        var state = total > 0 && succeeded == total ? "complete"
            : failed > 0 && pending == 0 && leased == 0 ? "failed"
            : succeeded > 0 ? "partial"
            : "waiting";
        using var update = _connection.CreateCommand();
        update.Transaction = tx;
        update.CommandText = """
            UPDATE media SET state = $state
            WHERE item_id = $item AND media_source_id = $source AND generation = $generation
              AND state NOT IN ('hidden', 'unknown')
            """;
        update.Parameters.AddWithValue("$state", state);
        update.Parameters.AddWithValue("$item", itemId.ToString("D"));
        update.Parameters.AddWithValue("$source", mediaSourceId);
        update.Parameters.AddWithValue("$generation", generation);
        update.ExecuteNonQuery();
    }
}

public readonly record struct FailedWindowRef(Guid ItemId, string MediaSourceId, Guid LibraryId, string ContentVersion);

public readonly record struct IndexEvent(long Id, string Kind, Guid? ItemId);
