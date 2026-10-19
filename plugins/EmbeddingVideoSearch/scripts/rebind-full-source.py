#!/usr/bin/env python3
"""Rebind imported pilot candidates to three full films. No network or inference.

Dry-run is the default. --execute writes one new representation version per channel,
leaves the previous payload inactive, and inserts three partial media rows.
"""
import argparse
import hashlib
import json
import math
import pathlib
import sqlite3
import subprocess
import uuid

ORIGINAL_INDEX = pathlib.Path(
    "/Users/mac/.local/share/jellyfin-video-search-acceptance/20261014-9196a68f/"
    "stock-12.2/data/data/embedding-video-search/8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17/index.db")
UNIX_EPOCH_TICKS = 621355968000000000
MAPPING_FIELDS = (
    "sourceItemId", "itemId", "mediaSourceId", "libraryId", "contentVersion", "runtimeTicks",
    "sourcePath", "sizeBytes", "modifiedTicks", "container", "audioStreamIndex")
PROTECTED_TABLES = ("signatures", "windows", "libraries", "events")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def file_modified_ticks(path):
    return UNIX_EPOCH_TICKS + path.stat().st_mtime_ns // 100


def content_version(size_bytes, runtime_ticks, modified_ticks, container, audio_index):
    text = f"{size_bytes}|{runtime_ticks}|{modified_ticks}|{container}|{audio_index}"
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_id(value):
    return uuid.UUID(str(value))


def table_digest(connection, table):
    rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return digest([[v.hex() if isinstance(v, bytes) else v for v in row] for row in rows])


def media_rows(connection):
    return connection.execute(
        "SELECT item_id, media_source_id, generation, library_id, content_version, runtime_ticks, state, "
        "source_path, has_audio, audio_stream_index FROM media ORDER BY item_id, media_source_id, generation").fetchall()


def setting_map(connection):
    return dict(connection.execute("SELECT key, value FROM settings").fetchall())


def assert_safe_index(path):
    resolved = path.resolve()
    if resolved == ORIGINAL_INDEX.resolve():
        raise SystemExit("Refusing the historical original index")
    if not resolved.is_file():
        raise SystemExit("Index database was not found")
    if index_holders(resolved):
        raise SystemExit("Stop the copied host first")
    return resolved


def index_holders(path):
    return subprocess.run(["lsof", "-t", str(path)], capture_output=True).stdout.strip()


def connect_index(path, execute):
    """Open the index. Dry-run never uses read-write: that would create WAL sidecars.

    A stopped backup can still have SQLite write-version 2 and no -wal/-shm. Plain
    mode=ro is not a portable read of that file: some builds reject the first SELECT,
    and others answer it while creating -wal and -shm. immutable=1 reads the
    checkpointed file without creating sidecars, but only after no process holds it
    and any WAL is absent or empty. A non-empty WAL is refused so its frames are not
    silently ignored.
    """
    resolved = path.resolve()
    if execute:
        return sqlite3.connect(resolved)
    if index_holders(resolved):
        raise SystemExit("Stop the copied host first")
    wal = pathlib.Path(str(resolved) + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise SystemExit("Dry-run refuses a non-empty WAL and will not ignore those frames or open the target read-write.")
    return sqlite3.connect(resolved.as_uri() + "?mode=ro&immutable=1", uri=True)


def load_mapping(path):
    rows = json.loads(path.read_text())
    if not isinstance(rows, list) or len(rows) != 3:
        raise SystemExit("Mapping must be a three-element array")
    films = {}
    for row in rows:
        if not isinstance(row, dict) or any(field not in row for field in MAPPING_FIELDS):
            raise SystemExit("Mapping entry is missing a required field")
        source = canonical_id(row["sourceItemId"])
        if source in films:
            raise SystemExit("Mapping sourceItemId values must be unique")
        media_path = pathlib.Path(row["sourcePath"])
        if not media_path.is_file():
            raise SystemExit("Mapped source file is missing")
        size = media_path.stat().st_size
        ticks = file_modified_ticks(media_path)
        if size != row["sizeBytes"] or ticks != row["modifiedTicks"]:
            raise SystemExit("Mapped file size or modification time does not match")
        expected = content_version(size, row["runtimeTicks"], ticks, row["container"], row["audioStreamIndex"])
        if expected != row["contentVersion"] or row["runtimeTicks"] <= 0 or not str(row["container"]):
            raise SystemExit("Mapped content version does not match the file facts")
        if not isinstance(row["audioStreamIndex"], int):
            raise SystemExit("audioStreamIndex must be an integer")
        canonical_id(row["itemId"])
        canonical_id(row["libraryId"])
        films[source] = row
    return films


def load_active(connection):
    rows = connection.execute(
        "SELECT channel, version, payload_json FROM candidate_representations WHERE active=1 ORDER BY channel").fetchall()
    if len(rows) != 2:
        raise SystemExit("Expected two active candidate representations")
    bundles = []
    for channel, version, payload in rows:
        bundle = json.loads(payload)
        if bundle.get("channel") != channel or bundle.get("version") != version:
            raise SystemExit("Active representation identity does not match its row")
        bundles.append((payload, bundle))
    counts = {bundle["channel"]: len(bundle["candidates"]) for _, bundle in bundles}
    if counts != {"joint": 42, "text": 25}:
        raise SystemExit("Expected 42 joint and 25 text candidates")
    return bundles


def playback_already_bound(candidate, film):
    canonical = candidate["canonical"]
    return (canonical_id(candidate["itemId"]) == canonical_id(film["itemId"])
            and candidate["mediaSourceId"] == film["mediaSourceId"]
            and candidate["startTicks"] == canonical["startTicks"]
            and candidate["endTicks"] == canonical["endTicks"]
            and candidate["runtimeTicks"] == film["runtimeTicks"]
            and candidate["contentVersion"] == film["contentVersion"])


def rebound_candidate(candidate, film):
    canonical = candidate["canonical"]
    start, end = canonical["startTicks"], canonical["endTicks"]
    if not (0 <= start < end <= film["runtimeTicks"]):
        raise SystemExit("Canonical range does not fit the full film")
    if end - start != candidate["endTicks"] - candidate["startTicks"]:
        raise SystemExit("Canonical duration differs from the stored candidate")
    anchor = f"{canonical_id(canonical['sourceItemId']).hex}:{start}:{end}"
    if candidate["anchorId"] != anchor:
        raise SystemExit("Anchor does not match the canonical range")
    vector = candidate["vector"]
    if len(vector) != 768 or any(not math.isfinite(x) for x in vector) or abs(math.fsum(x * x for x in vector) - 1) >= 0.02:
        raise SystemExit("Candidate vector is not a stored 768-d unit vector")
    rebound = dict(candidate)
    rebound.pop("segmentId", None)
    rebound.update(itemId=str(canonical_id(film["itemId"])), mediaSourceId=film["mediaSourceId"],
                   libraryId=str(canonical_id(film["libraryId"])), contentVersion=film["contentVersion"],
                   runtimeTicks=film["runtimeTicks"], startTicks=start, endTicks=end)
    return rebound


def build_bundle(bundle, films, old_segment_ids):
    used = set()
    candidates = []
    for candidate in bundle["candidates"]:
        source = canonical_id(candidate["canonical"]["sourceItemId"])
        if source not in films:
            raise SystemExit("A candidate canonical source is not one of the three films")
        used.add(source)
        candidates.append(rebound_candidate(candidate, films[source]))
    content = digest(candidates)
    version = digest({"binding": "full-source-canonical-absolute-v1", "channel": bundle["channel"],
                      "content": content, "reference": bundle["referenceVersion"]})
    if version == bundle["version"]:
        raise SystemExit("New representation version collided with the active version")
    for candidate in candidates:
        candidate["segmentId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "evs-candidate:" + version + ":" + candidate["candidateId"]))
        if candidate["segmentId"] in old_segment_ids:
            raise SystemExit("New segment id collided with an existing candidate")
        old_segment_ids.add(candidate["segmentId"])
    built = dict(bundle)
    built.update(version=version, contentVersion=content,
                 segmentationVersion=digest([[c["anchorId"], c["startTicks"], c["endTicks"]] for c in candidates]),
                 candidates=candidates)
    return built, used


def media_plan(connection, films):
    generation = connection.execute("SELECT generation FROM signatures WHERE active=1").fetchone()[0]
    planned = []
    for film in films.values():
        item = str(canonical_id(film["itemId"]))
        existing = connection.execute(
            "SELECT library_id, content_version, runtime_ticks, state, source_path, has_audio, audio_stream_index "
            "FROM media WHERE item_id=? AND media_source_id=? AND generation=?",
            (item, film["mediaSourceId"], generation)).fetchone()
        audio = film["audioStreamIndex"]
        desired = (str(canonical_id(film["libraryId"])), film["contentVersion"], film["runtimeTicks"], "partial",
                   str(pathlib.Path(film["sourcePath"])), 1 if audio >= 0 else 0, audio)
        if existing is not None and tuple(existing) != desired:
            raise SystemExit("A media row already exists for this film and does not match the mapping")
        planned.append((item, film["mediaSourceId"], generation, *desired, existing is None))
    return planned


def summarize(connection, films, built, executed, already):
    return {
        "executed": executed,
        "alreadyApplied": already,
        "candidateCounts": {bundle["channel"]: len(bundle["candidates"]) for bundle in built},
        "representations": {bundle["channel"]: bundle["version"] for bundle in built},
        "mediaPartial": [row[0] for row in media_plan(connection, films)],
        "importedCandidatesOnly": setting_map(connection).get("imported_candidates_only") == "1",
        "newModelInputs": 0,
        "rankingVersion": "reference16-logistic-tail-f64-v1",
    }


def apply_rebind(connection, films, execute):
    if setting_map(connection).get("manual_pause") != "1":
        raise SystemExit("manual_pause must be 1 before rebinding")
    protected = {table: table_digest(connection, table) for table in PROTECTED_TABLES}
    old_media = media_rows(connection)
    settings = setting_map(connection)
    loaded = load_active(connection)
    old_segments = {candidate["segmentId"] for _, bundle in loaded for candidate in bundle["candidates"]}
    already = all(playback_already_bound(candidate, films[canonical_id(candidate["canonical"]["sourceItemId"])])
                  for _, bundle in loaded for candidate in bundle["candidates"])
    built = []
    used = set()
    if not already:
        seen = set(old_segments)
        for _, bundle in loaded:
            rebound, sources = build_bundle(bundle, films, seen)
            built.append(rebound)
            used.update(sources)
        if used != set(films):
            raise SystemExit("Each mapped film must own at least one candidate")
        occupied = {row[0] for row in connection.execute("SELECT version FROM candidate_representations").fetchall()}
        if any(bundle["version"] in occupied for bundle in built):
            raise SystemExit("Refusing to overwrite an existing representation version")
    planned = media_plan(connection, films)
    if not execute:
        visible = built or [bundle for _, bundle in loaded]
        summary = summarize(connection, films, visible, False, already)
        summary["wouldWriteRepresentations"] = not already
        summary["wouldInsertMedia"] = sum(1 for row in planned if row[-1])
        summary["wouldSetImportedCandidatesOnly"] = True
        return summary, protected, old_media, settings

    with connection:
        if not already:
            for payload, bundle in loaded:
                stored = connection.execute(
                    "SELECT payload_json FROM candidate_representations WHERE channel=? AND version=?",
                    (bundle["channel"], bundle["version"])).fetchone()[0]
                if stored != payload:
                    raise SystemExit("Active payload changed before it could be deactivated")
                connection.execute("UPDATE candidate_representations SET active=0 WHERE channel=? AND version=?",
                                   (bundle["channel"], bundle["version"]))
                unchanged = connection.execute(
                    "SELECT payload_json FROM candidate_representations WHERE channel=? AND version=?",
                    (bundle["channel"], bundle["version"])).fetchone()[0]
                if unchanged != payload:
                    raise SystemExit("Deactivating a representation changed its sample payload")
            for bundle in built:
                connection.execute(
                    "INSERT INTO candidate_representations (channel, version, payload_json, active) VALUES (?,?,?,1)",
                    (bundle["channel"], bundle["version"], json.dumps(bundle, ensure_ascii=False)))
        for item, source, generation, library, version, runtime, state, path, audio, audio_index, insert in planned:
            if insert:
                connection.execute(
                    "INSERT INTO media (item_id, media_source_id, generation, library_id, content_version, "
                    "runtime_ticks, state, source_path, has_audio, audio_stream_index) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (item, source, generation, library, version, runtime, state, path, audio, audio_index))
        connection.execute(
            "INSERT INTO settings (key, value) VALUES ('imported_candidates_only', '1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'")
    if any(table_digest(connection, table) != protected[table] for table in PROTECTED_TABLES):
        raise SystemExit("Signatures, windows, libraries, or events changed")
    current_media = media_rows(connection)
    if any(row not in current_media for row in old_media):
        raise SystemExit("An existing media row changed")
    inserted = sum(1 for row in planned if row[-1])
    if len(current_media) != len(old_media) + inserted:
        raise SystemExit("Media table changed by more than the three film rows")
    current = setting_map(connection)
    for key, value in settings.items():
        if key == "imported_candidates_only":
            continue
        if current.get(key) != value:
            raise SystemExit("An existing setting changed")
    if current.get("imported_candidates_only") != "1":
        raise SystemExit("imported_candidates_only was not set")
    return summarize(connection, films, built or [bundle for _, bundle in load_active(connection)], True, already), protected, old_media, settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping", type=pathlib.Path, required=True)
    parser.add_argument("--index", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    index = assert_safe_index(args.index)
    films = load_mapping(args.mapping)
    args.output.mkdir(parents=True, exist_ok=True)
    backup = args.output / "pre-rebind-index.db"
    if args.execute:
        if backup.exists():
            raise SystemExit("Keep the previous rebind backup; choose another output directory")
        source = sqlite3.connect(index)
        with sqlite3.connect(backup) as destination:
            source.backup(destination)
        source.close()
    connection = connect_index(index, args.execute)
    try:
        summary, _, _, _ = apply_rebind(connection, films, args.execute)
    finally:
        connection.close()
    summary["backup"] = str(backup) if args.execute else None
    (args.output / "rebind-evidence.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
