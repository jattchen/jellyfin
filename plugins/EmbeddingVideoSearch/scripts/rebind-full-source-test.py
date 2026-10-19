#!/usr/bin/env python3
"""Synthetic dry-run and execute coverage for rebind-full-source.py. Does not open a live index."""
import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import uuid

SCRIPT = pathlib.Path(__file__).with_name("rebind-full-source.py")
spec = importlib.util.spec_from_file_location("rebind_full_source", SCRIPT)
rebind = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rebind)


def unit():
    vector = [0.0] * 768
    vector[0] = 1.0
    return vector


def candidate(channel, film, start, transcript=None):
    end = start + 100
    source = str(film)
    return {
        "candidateId": f"{channel}-{film.hex[:8]}-{start}",
        "anchorId": f"{film.hex}:{start}:{end}",
        "itemId": str(uuid.uuid4()),
        "mediaSourceId": "sample-source",
        "libraryId": str(uuid.uuid4()),
        "contentVersion": "sample-version",
        "startTicks": start // 5,
        "endTicks": start // 5 + 100,
        "runtimeTicks": 120_000_000,
        "canonical": {"sourceItemId": source, "startTicks": start, "endTicks": end},
        "transcript": transcript,
        "vector": unit(),
        "segmentId": str(uuid.uuid4()),
    }


def bundle(channel, rows, version):
    return {
        "channel": channel, "version": version, "model": "google/embeddinggemma-2",
        "revision": "914f7f89142e33e77833254d9c9b90c3cef7303b", "processingId": "proc-a",
        "dimension": 768, "normalized": True, "inputVersion": "kept", "embeddingTask": "kept",
        "contentVersion": "old-content", "segmentationVersion": "old-segmentation",
        "referenceVersion": "curve-" + channel, "referenceQueryVersion": "queries",
        "algorithm": "reference16-logistic-tail-f64-v1", "scale": 0.02, "background": [0.1, 0.2],
        "candidates": rows,
    }


def mapping_entry(root, film, item, library, name):
    path = root / name
    path.write_bytes(b"film-bytes")
    ticks = 638854162990000000
    ns = (ticks - rebind.UNIX_EPOCH_TICKS) * 100
    import os
    os.utime(path, ns=(ns, ns))
    actual = rebind.file_modified_ticks(path)
    runtime = 20_000_000_000
    return {
        "sourceItemId": str(film), "itemId": str(item), "mediaSourceId": item.hex,
        "libraryId": str(library),
        "contentVersion": rebind.content_version(path.stat().st_size, runtime, actual, "mp4", 1),
        "runtimeTicks": runtime, "sourcePath": str(path), "sizeBytes": path.stat().st_size,
        "modifiedTicks": actual, "container": "mp4", "audioStreamIndex": 1,
    }


def create_index(path, films, library):
    joint = [candidate("joint", films[index % 3], 1_000_000 + index * 1000) for index in range(42)]
    text = [candidate("text", films[index % 3], 2_000_000 + index * 1000, "原文" + str(index)) for index in range(25)]
    payloads = {
        "joint": json.dumps(bundle("joint", joint, "joint-old"), ensure_ascii=False),
        "text": json.dumps(bundle("text", text, "text-old"), ensure_ascii=False),
    }
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE libraries (library_id TEXT PRIMARY KEY);
        CREATE TABLE signatures (
          generation INTEGER PRIMARY KEY, model TEXT, revision TEXT, processing_id TEXT,
          dimension INTEGER, normalized INTEGER, input_version TEXT, window_version TEXT,
          audio_version TEXT, active INTEGER);
        CREATE TABLE media (
          item_id TEXT, media_source_id TEXT, generation INTEGER, library_id TEXT,
          content_version TEXT, runtime_ticks INTEGER, state TEXT, source_path TEXT,
          has_audio INTEGER, audio_stream_index INTEGER,
          PRIMARY KEY (item_id, media_source_id, generation));
        CREATE TABLE windows (
          segment_id TEXT PRIMARY KEY, item_id TEXT, media_source_id TEXT, generation INTEGER,
          content_version TEXT, start_ticks INTEGER, end_ticks INTEGER, state TEXT, vector BLOB);
        CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT, item_id TEXT, created_utc TEXT, handled INTEGER);
        CREATE TABLE candidate_representations (
          channel TEXT, version TEXT, payload_json TEXT, active INTEGER, PRIMARY KEY(channel, version));
    """)
    connection.execute("INSERT INTO settings VALUES ('manual_pause', '1')")
    connection.execute("INSERT INTO settings VALUES ('related_minimum', '0.35')")
    connection.execute("INSERT INTO signatures VALUES (3, 'm', 'r', 'p', 768, 1, 'i', 'w', 'a', 1)")
    connection.execute(
        "INSERT INTO media VALUES (?,?,3,?,?,120000000,'complete','sample.mp4',0,NULL)",
        (str(uuid.uuid4()), "sample-source", str(library), "sample-version"))
    connection.execute(
        "INSERT INTO windows VALUES ('old-window', 'sample', 'sample-source', 3, 'sample-version', 0, 60, 'succeeded', NULL)")
    for channel, payload in payloads.items():
        connection.execute("INSERT INTO candidate_representations VALUES (?,?,?,1)", (channel, channel + "-old", payload))
    connection.commit()
    connection.close()
    return payloads


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = pathlib.Path(temporary)
        films = [uuid.uuid4() for _ in range(3)]
        library = uuid.uuid4()
        items = [uuid.uuid4() for _ in range(3)]
        index = root / "index.db"
        originals = create_index(index, films, library)
        leave_wal_header_without_sidecars(index)
        mapped = [mapping_entry(root, films[i], items[i], library, f"film-{i}.mp4") for i in range(3)]
        mapping = root / "mapping.json"
        mapping.write_text(json.dumps(mapped))
        before = index.read_bytes()
        dry = root / "dry"
        rebind.main(["--mapping", str(mapping), "--index", str(index), "--output", str(dry)])
        assert index.read_bytes() == before
        assert not pathlib.Path(str(index) + "-wal").exists()
        assert not pathlib.Path(str(index) + "-shm").exists()
        evidence = json.loads((dry / "rebind-evidence.json").read_text())
        assert evidence["executed"] is False and evidence["wouldWriteRepresentations"] is True
        assert evidence["candidateCounts"] == {"joint": 42, "text": 25}

        executed = root / "run"
        rebind.main(["--mapping", str(mapping), "--index", str(index), "--output", str(executed), "--execute"])
        assert (executed / "pre-rebind-index.db").is_file()
        connection = sqlite3.connect(index)
        active = connection.execute(
            "SELECT channel, version, payload_json, active FROM candidate_representations ORDER BY channel, active").fetchall()
        inactive = [row for row in active if row[3] == 0]
        current = [row for row in active if row[3] == 1]
        assert [row[0] for row in inactive] == ["joint", "text"]
        assert inactive[0][2] == originals["joint"] and inactive[1][2] == originals["text"]
        assert {row[0] for row in current} == {"joint", "text"}
        old_ids = set()
        new_ids = set()
        kept_curves = set()
        for channel, _, payload, _ in current:
            bundle = json.loads(payload)
            kept_curves.add(bundle["referenceVersion"])
            assert bundle["scale"] == 0.02 and bundle["background"] == [0.1, 0.2]
            for row in bundle["candidates"]:
                new_ids.add(row["segmentId"])
                assert row["startTicks"] == row["canonical"]["startTicks"]
                assert row["endTicks"] == row["canonical"]["endTicks"]
                assert row["runtimeTicks"] == 20_000_000_000
                assert uuid.UUID(row["itemId"]) in items
        for payload in originals.values():
            for row in json.loads(payload)["candidates"]:
                old_ids.add(row["segmentId"])
                assert row["mediaSourceId"] == "sample-source"
        assert old_ids.isdisjoint(new_ids)
        assert kept_curves == {"curve-joint", "curve-text"}
        media = connection.execute("SELECT state, COUNT(*) FROM media GROUP BY state").fetchall()
        assert dict(media) == {"complete": 1, "partial": 3}
        assert connection.execute("SELECT COUNT(*) FROM windows").fetchone()[0] == 1
        assert connection.execute("SELECT value FROM settings WHERE key='imported_candidates_only'").fetchone()[0] == "1"
        assert connection.execute("SELECT value FROM settings WHERE key='related_minimum'").fetchone()[0] == "0.35"
        again = rebind.apply_rebind(connection, rebind.load_mapping(mapping), True)
        assert again[0]["alreadyApplied"] is True
        assert connection.execute("SELECT COUNT(*) FROM candidate_representations WHERE active=1").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM media WHERE state='partial'").fetchone()[0] == 3
        connection.close()
        try:
            rebind.assert_safe_index(rebind.ORIGINAL_INDEX)
            raise SystemExit("original index was not refused")
        except SystemExit as error:
            assert "historical original" in str(error)
        refuse_nonempty_wal(root)
    print("rebind script checks passed")


def leave_wal_header_without_sidecars(path):
    """Leave a checkpointed WAL-header database and prove the dry-run boundary.

    Byte 18 stays 2 and both sidecars start absent. Plain mode=ro is only observed:
    an "unable to open database file" error is the older outcome, and a successful
    read is the newer one. Any other error still fails. Sidecars created by that
    probe are removed before the product open. connect_index must then read the
    pause setting, leave the database bytes unchanged, and create no sidecars.
    """
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.commit()
    connection.close()
    for suffix in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    snapshot = path.read_bytes()
    assert snapshot[18] == 2
    assert not pathlib.Path(str(path) + "-wal").exists()
    assert not pathlib.Path(str(path) + "-shm").exists()
    plain = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        try:
            row = plain.execute("SELECT value FROM settings WHERE key='manual_pause'").fetchone()
        except sqlite3.OperationalError as error:
            assert "unable to open database file" in str(error)
        else:
            assert row == ("1",)
    finally:
        plain.close()
    for suffix in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    assert path.read_bytes() == snapshot
    opened = rebind.connect_index(path, execute=False)
    try:
        assert opened.execute("SELECT value FROM settings WHERE key='manual_pause'").fetchone() == ("1",)
    finally:
        opened.close()
    assert path.read_bytes() == snapshot
    assert not pathlib.Path(str(path) + "-wal").exists()
    assert not pathlib.Path(str(path) + "-shm").exists()


def refuse_nonempty_wal(root):
    path = root / "pending-wal.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO settings VALUES ('manual_pause', '1')")
    connection.commit()
    connection.close()
    leave_wal_header_without_sidecars(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    empty = pathlib.Path(str(path) + "-wal")
    empty.write_bytes(b"")
    opened = rebind.connect_index(path, execute=False)
    assert opened.execute("SELECT value FROM settings WHERE key='manual_pause'").fetchone()[0] == "1"
    opened.close()
    assert empty.stat().st_size == 0 and hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert not pathlib.Path(str(path) + "-shm").exists()
    empty.write_bytes(b"pending-frames")
    try:
        rebind.connect_index(path, execute=False)
        raise AssertionError("non-empty WAL was opened")
    except SystemExit as error:
        assert "non-empty WAL" in str(error)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert empty.read_bytes() == b"pending-frames"


if __name__ == "__main__":
    main()
