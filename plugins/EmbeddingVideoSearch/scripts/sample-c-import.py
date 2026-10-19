#!/usr/bin/env python3
"""Copy an emptied host index and add only the 12-second sample films.

The template is a copy of the isolated host index after its media, windows,
signatures and candidate rows were cleared. This script does not open the
running host index, stop the host, or embed.
"""
import argparse
import fcntl
import importlib.util
import json
import sqlite3
import struct
from pathlib import Path


EMPTY_TABLES = ("media", "windows", "signatures", "candidate_representations")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load("sample_c_contract_import", "sample-c-contract.py")
importer = load("film_import_for_sample_c", "full-film-import.py")


def same_path(left, right):
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


def shared_cache_directory(protocol):
    raw = protocol.get("shared_document_cache")
    if raw in (None, False, ""):
        return None
    if not isinstance(raw, dict) or not raw.get("directory"):
        raise contract.ContractError("shared_document_cache requires an explicit directory")
    directory = Path(raw["directory"])
    if not directory.is_absolute():
        raise contract.ContractError("shared document cache requires an absolute directory")
    if any(part.startswith("asr-") or part in contract.PROTECTED_NAMES for part in directory.parts):
        raise contract.ContractError("shared document cache must not write accepted or ASR artifacts")
    return str(directory)


def require_template(path):
    path = Path(path)
    if not path.name.endswith(".template.db"):
        raise contract.ContractError("pass an explicitly named .template.db copy")
    if not path.is_file():
        raise contract.ContractError("index template is missing")
    lock = path.parent / "writer.lock"
    if lock.exists():
        with lock.open("r+b") as handle:
            try:
                fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise contract.ContractError("template directory has an active writer") from exc
            finally:
                fcntl.lockf(handle, fcntl.LOCK_UN)
    return path


def index_text_id(item_id, original_id):
    prefix = "text-" + contract.canonical_item(item_id).replace("-", "") + "-"
    if str(original_id).startswith(prefix):
        return str(original_id)
    return prefix + str(original_id)


def empty_text_bundle(signature, query_version):
    content = importer.digest([])
    reference_version = importer.digest({
        "signature": signature, "content": content, "emptyTranscription": True,
        "queryVersion": query_version, "algorithm": importer.ALGORITHM,
    })
    version = importer.digest({"channel": "text", "content": content, "reference": reference_version})
    return {
        "channel": "text", "version": version, "model": signature["id"], "revision": signature["revision"],
        "processingId": signature["processing_id"], "dimension": 768, "normalized": True,
        "inputVersion": "transcript-document-cache-v1", "embeddingTask": "document",
        "contentVersion": content, "segmentationVersion": content,
        "referenceVersion": reference_version, "referenceQueryVersion": query_version,
        "algorithm": importer.ALGORITHM, "scale": 0, "background": [], "candidates": [],
        "transcriptionChecked": True,
    }


def load_references(references, signature):
    paths = sorted((Path(references) / "responses").glob("*.json"))
    if len(paths) != 16:
        raise contract.ContractError("the sample index needs the same sixteen reference queries")
    vectors = []
    for path in paths:
        response = json.loads(path.read_text(encoding="utf-8"))
        importer.embedding.validate_signature(response, signature)
        if response["data"][0]["processing"] != {"modalities": ["text"], "task": "query"}:
            raise contract.ContractError("reference query processing does not match")
        vectors.append(importer.f32(response["data"][0]["embedding"]))
    return vectors, importer.digest({path.name: importer.sha(path) for path in paths})


def load_film(run, root, signature, cache_directory):
    run = Path(run)
    protocol_path = run / "protocol.json"
    before = protocol_path.read_bytes()
    candidates_path = run / "candidates.json"
    candidates_before = candidates_path.read_bytes()
    protocol = json.loads(before)
    if protocol.get("fullFilm") is not False:
        raise contract.ContractError("film protocol must stay a partial excerpt")
    contract.require_signature(protocol.get("signature"))
    if contract.signature_tuple(protocol["signature"]) != contract.signature_tuple(signature):
        raise contract.ContractError("film signature does not match the root protocol")
    contract.checked_window(protocol.get("window"))
    if protocol["window"] != root["window"]:
        raise contract.ContractError("film window does not match the root protocol")
    if shared_cache_directory(protocol) != cache_directory:
        raise contract.ContractError("film shared_document_cache does not match the root protocol")
    source = protocol["source"]
    matches = [film["source"] for film in root.get("films", [])
               if contract.canonical_item(film["source"]["itemId"]) == contract.canonical_item(source["itemId"])]
    if len(matches) != 1:
        raise contract.ContractError("film must match exactly one frozen root source")
    frozen_source = matches[0]
    for field in ("itemId", "mediaSourceId", "libraryId", "contentVersion", "runtimeTicks",
                  "sourcePath", "audioStreamIndex", "sourceItemId", "sizeBytes", "modifiedTicks"):
        actual, expected = source.get(field), frozen_source.get(field)
        if field in ("itemId", "libraryId", "sourceItemId") and actual is not None and expected is not None:
            actual, expected = contract.canonical_item(actual), contract.canonical_item(expected)
        if actual != expected:
            raise contract.ContractError("film source does not match frozen root: " + field)
    sample = protocol["sample"]
    planned = contract.plan_sample(sample["sampleStartTicks"], sample["sampleEndTicks"], source["runtimeTicks"])
    entries = protocol["joint_windows"]
    if [(int(entry["start_ticks"]), int(entry["end_ticks"])) for entry in entries] != planned:
        raise contract.ContractError("joint windows do not match the frozen excerpt")
    check = json.loads((run / "transcription-check.json").read_text(encoding="utf-8"))
    if check.get("status") != "succeeded" or check.get("fullFilm") is not False:
        raise contract.ContractError("transcription check did not succeed for this excerpt")
    if int(check["absoluteOffsetTicks"]) != int(sample["absoluteOffsetTicks"]):
        raise contract.ContractError("transcription offset does not match the sample")
    text_entries = json.loads(candidates_before)
    if len(text_entries) != int(check["candidateCount"]):
        raise contract.ContractError("candidate count does not match the transcription check")
    if check["contentVersion"] != source["contentVersion"] or check["sourcePath"] != source["sourcePath"]:
        raise contract.ContractError("transcription check is not bound to the original film")
    rooted = dict(protocol)
    rooted["signature"] = signature
    rooted["window"] = root["window"]
    joint_rows = []
    reused = 0
    for entry in entries:
        state = json.loads((run / "joint" / (entry["candidate_id"] + ".state.json")).read_text(encoding="utf-8"))
        if state.get("status") != "succeeded":
            raise RuntimeError("Unresolved prior dispatch; inspect before any retry: " + entry["candidate_id"])
        joint_rows.append(importer.candidate_row(entry, source, importer.verified_vector(run, entry, rooted, "joint")))
    text_rows = []
    text_map = []
    for entry in text_entries:
        start, end = int(entry["start_ticks"]), int(entry["end_ticks"])
        if not int(sample["sampleStartTicks"]) <= start < end <= int(sample["sampleEndTicks"]) <= int(source["runtimeTicks"]):
            raise contract.ContractError("text candidate is outside the original sample span")
        if entry.get("full_film") is not False:
            raise contract.ContractError("text candidate claims to be the whole film")
        state_path = run / "text" / (entry["candidate_id"] + ".state.json")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") != "succeeded":
            raise RuntimeError("Unresolved prior dispatch; inspect before any retry: " + str(state_path))
        reused += int(bool(state.get("model_input_reused")))
        vector = importer.verified_vector(run, entry, rooted, "text")
        namespaced_id = index_text_id(source["itemId"], entry["candidate_id"])
        cloned = dict(entry)
        cloned["candidate_id"] = namespaced_id
        text_rows.append(importer.candidate_row(cloned, source, vector, entry.get("text")))
        text_map.append({
            "itemId": contract.canonical_item(source["itemId"]),
            "originalCandidateId": entry["candidate_id"],
            "candidateId": namespaced_id,
            "startTicks": start,
            "endTicks": end,
            "modelInputReused": bool(state.get("model_input_reused")),
        })
    if protocol_path.read_bytes() != before or candidates_path.read_bytes() != candidates_before:
        raise contract.ContractError("embed protocol or candidates changed during import")
    return {
        "protocol": protocol, "source": source, "joint_rows": joint_rows, "text_rows": text_rows,
        "text_map": text_map, "reused_text": reused, "sample": sample,
    }


def assert_empty_template(connection):
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != 3:
        raise contract.ContractError("index template schema version must be 3")
    names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"settings", "libraries", "signatures", "media", "windows", "events", "candidate_representations"}
    if not required <= names:
        raise contract.ContractError("index template is missing a host table")
    for table in EMPTY_TABLES:
        if connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != 0:
            raise contract.ContractError("index template still has rows in " + table)
    settings = dict(connection.execute("SELECT key, value FROM settings"))
    if settings.get("manual_pause") != "1" or settings.get("imported_candidates_only") != "1":
        raise contract.ContractError("template must keep manual pause and imported candidates only")
    if settings.get("embedding_base_url") != contract.ISOLATED_BASE:
        raise contract.ContractError("template embedding base must be http://127.0.0.1:18784")
    return settings


def write_index(template, films, references, signature, query_version, output, cache_directory):
    contract.refuse_write_target(output)
    output = Path(output)
    if output.exists():
        raise contract.ContractError("Choose a new output; never overwrite a prepared index")
    template = require_template(template)
    source_db = sqlite3.connect(f"file:{template}?mode=ro", uri=True)
    try:
        assert_empty_template(source_db)
    finally:
        source_db.close()
    output.mkdir(parents=True, mode=0o700)
    target = output / "prepared-index.db"
    source_db = sqlite3.connect(f"file:{template}?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            source_db.backup(destination)
        finally:
            destination.close()
    finally:
        source_db.close()
    connection = sqlite3.connect(target)
    try:
        libraries = {contract.canonical_item(film["source"]["libraryId"]) for film in films}
        joint_prior = {
            "channel": "joint", "candidates": [], "model": signature["id"], "revision": signature["revision"],
            "processingId": signature["processing_id"], "dimension": 768, "normalized": True,
            "inputVersion": contract.INPUT_VERSION, "embeddingTask": "video_audio",
        }
        text_prior = None
        text_rows_seen = False
        with connection:
            connection.execute("DELETE FROM events")
            connection.execute("DELETE FROM libraries")
            for library_id in sorted(libraries):
                connection.execute("INSERT INTO libraries VALUES (?)", (library_id,))
            if connection.execute("SELECT 1 FROM settings WHERE key='active_generation'").fetchone():
                connection.execute("UPDATE settings SET value='1' WHERE key='active_generation'")
            else:
                connection.execute("INSERT INTO settings VALUES ('active_generation', '1')")
            connection.execute("""INSERT INTO signatures VALUES (1, ?, ?, ?, 768, 1, ?, ?, ?, 1)""", (
                signature["id"], signature["revision"], signature["processing_id"],
                contract.INPUT_VERSION, contract.VERSION, contract.AUDIO_VERSION))
            for film in films:
                if text_rows_seen or film["text_rows"]:
                    text_rows_seen = True
                    if text_prior is None:
                        text_prior = {
                            "channel": "text", "candidates": [], "model": signature["id"],
                            "revision": signature["revision"], "processingId": signature["processing_id"],
                            "dimension": 768, "normalized": True,
                            "inputVersion": "transcript-document-cache-v1", "embeddingTask": "document",
                        }
                    if film["text_rows"]:
                        text_prior, _, _ = importer.build_bundle(
                            text_prior, film["source"], film["text_rows"], references, query_version, signature)
                joint_prior, _, _ = importer.build_bundle(
                    joint_prior, film["source"], film["joint_rows"], references, query_version, signature)
                source = film["source"]
                item = contract.canonical_item(source["itemId"])
                connection.execute("""INSERT INTO media VALUES (?, ?, 1, ?, ?, ?, 'partial', ?, 1, ?)""", (
                    item, source["mediaSourceId"], contract.canonical_item(source["libraryId"]),
                    source["contentVersion"], int(source["runtimeTicks"]), source["sourcePath"],
                    int(source["audioStreamIndex"])))
            if text_prior is None:
                text_prior = empty_text_bundle(signature, query_version)
            else:
                text_prior["transcriptionChecked"] = True
            for bundle in (joint_prior, text_prior):
                connection.execute("INSERT INTO candidate_representations VALUES (?, ?, ?, 1)", (
                    bundle["channel"], bundle["version"], json.dumps(bundle, ensure_ascii=False)))
            for film in films:
                source = film["source"]
                item = contract.canonical_item(source["itemId"])
                by_id = {row["candidateId"]: row for row in joint_prior["candidates"] if row["itemId"] == item}
                for entry in film["joint_rows"]:
                    start, end = int(entry["startTicks"]), int(entry["endTicks"])
                    row = by_id[entry["candidateId"]]
                    segment = importer.native_segment_id(item, source["mediaSourceId"], source["contentVersion"], start, end, 1)
                    blob = struct.pack("<768f", *row["vector"])
                    connection.execute("""INSERT INTO windows
                        (segment_id, item_id, media_source_id, generation, content_version,
                         start_ticks, end_ticks, state, vector, attempt)
                        VALUES (?, ?, ?, 1, ?, ?, ?, 'succeeded', ?, 1)""", (
                        segment, item, source["mediaSourceId"], source["contentVersion"], start, end, blob))
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise contract.ContractError("new index failed integrity check")
        if connection.execute("SELECT COUNT(*) FROM media WHERE state!='partial'").fetchone()[0] != 0:
            raise contract.ContractError("sample media must stay partial")
        if connection.execute("SELECT COUNT(*) FROM signatures").fetchone()[0] != 1:
            raise contract.ContractError("sample index must have one signature")
        stored_libraries = {row[0] for row in connection.execute("SELECT library_id FROM libraries")}
        if stored_libraries != libraries:
            raise contract.ContractError("sample index enabled a library outside the source facts")
        evidence = {
            "films": [{
                "label": film["protocol"]["label"],
                "itemId": contract.canonical_item(film["source"]["itemId"]),
                "contentVersion": film["source"]["contentVersion"],
                "sourcePath": film["source"]["sourcePath"],
                "runtimeTicks": int(film["source"]["runtimeTicks"]),
                "sampleStartTicks": int(film["sample"]["sampleStartTicks"]),
                "sampleEndTicks": int(film["sample"]["sampleEndTicks"]),
                "windows": len(film["joint_rows"]),
                "documents": len(film["text_rows"]),
                "state": "partial",
            } for film in films],
            "fullFilm": False,
            "coverage": "partial",
            "emptyNewIndex": False,
            "templateCopied": True,
            "priorIndexOpened": False,
            "liveHostIndexOpened": False,
            "algorithm": importer.ALGORITHM,
            "windowVersion": contract.VERSION,
            "embeddingBaseUrl": contract.ISOLATED_BASE,
            "sharedDocumentCache": cache_directory,
            "reusedTextInputs": sum(film["reused_text"] for film in films),
            "newModelInputsDuringImport": 0,
            "manualPause": True,
            "importedCandidatesOnly": True,
            "target": str(target),
        }
        contract.save_json(output / "import-evidence.json", evidence)
        return evidence
    finally:
        connection.close()


def import_films(root, film_runs, references, template, output):
    signature = contract.require_signature(root.get("signature"))
    contract.checked_window(root.get("window"))
    contract.require_isolated_base(root.get("embedding_base_url"))
    if root.get("fullFilm") is not False:
        raise contract.ContractError("root protocol must stay a partial sample")
    cache_directory = shared_cache_directory(root)
    frozen = root.get("films") or []
    if len(film_runs) != len(frozen) or not frozen:
        raise contract.ContractError("import runs must match the root film list")
    seen = []
    loaded = []
    for run in film_runs:
        film = load_film(run, root, signature, cache_directory)
        item = contract.canonical_item(film["source"]["itemId"])
        if item in seen:
            raise contract.ContractError("film appears more than once")
        seen.append(item)
        loaded.append(film)
    expected = {contract.canonical_item(film["source"]["itemId"]) for film in frozen}
    if set(seen) != expected:
        raise contract.ContractError("import must include each root film once")
    vectors, query_version = load_references(references, signature)
    return write_index(template, loaded, vectors, signature, query_version, output, cache_directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--film-run", type=Path, action="append", required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = json.loads(args.protocol.read_text(encoding="utf-8"))
    evidence = import_films(root, args.film_run, args.references, args.template, args.output)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
