#!/usr/bin/env python3
"""Build an empty new index for the 10-second run. It does not open or replace a 6-second index."""
import argparse
import importlib.util
import json
import sqlite3
import struct
from pathlib import Path


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load("ten_second_contract_import", "ten-second-contract.py")
importer = load("film_import_for_ten", "full-film-import.py")


def _span(row):
    if "startTicks" in row:
        return int(row["startTicks"]), int(row["endTicks"]), row["candidateId"]
    return int(row["start_ticks"]), int(row["end_ticks"]), row["candidate_id"]


def index_text_id(item_id, original_id):
    prefix = "text-" + contract.canonical_item(item_id).replace("-", "") + "-"
    if str(original_id).startswith(prefix):
        return str(original_id)
    return prefix + str(original_id)


def namespace_text_entry(source, entry):
    """Index id is namespaced. The ASR candidate id, text and ticks stay on the original entry."""
    original = entry["candidate_id"]
    namespaced = index_text_id(source["itemId"], original)
    cloned = dict(entry)
    cloned["candidate_id"] = namespaced
    return cloned, {
        "itemId": contract.canonical_item(source["itemId"]),
        "sourcePath": source["sourcePath"],
        "contentVersion": source["contentVersion"],
        "originalCandidateId": original,
        "candidateId": namespaced,
        "startTicks": int(entry["start_ticks"]),
        "endTicks": int(entry["end_ticks"]),
        "text": entry.get("text"),
        "alignmentTimePolicy": (entry.get("provenance") or {}).get("alignment_time_policy"),
        "timeBoundaryDerivation": (entry.get("provenance") or {}).get("time_boundary_derivation"),
    }


def match_frozen_film(root, film_protocol):
    contract.require_same_signature(film_protocol.get("signature"), root["signature"])
    contract.require_same_window(film_protocol.get("window"), root["window"])
    source = film_protocol["source"]
    matches = []
    for film in root.get("films") or []:
        try:
            contract.bind_source(film["source"], source)
        except contract.ContractError:
            continue
        matches.append(film)
    if len(matches) != 1:
        raise contract.ContractError("film source is not one of the three frozen films")
    return contract.canonical_item(source["itemId"])


def require_embedding_closed(run, entry, channel):
    state_path = Path(run) / channel / (entry["candidate_id"] + ".state.json")
    if not state_path.is_file():
        raise contract.ContractError("missing embedding checkpoint; failed or unknown work cannot be imported")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    contract.require_closed(state.get("status"), channel + " " + entry["candidate_id"])
    return state


def build_empty_index(films, references, signature, query_version, output, embedding_base_url):
    """films items carry source, joint_rows and text_rows. Vectors are already verified."""
    url = contract.require_isolated_base(embedding_base_url)
    contract.require_signature(signature)
    contract.refuse_write_target(output)
    output = Path(output)
    if output.exists():
        raise contract.ContractError("Choose a new output; never overwrite a prepared index")
    for film in films:
        if film.get("signature") is not None:
            contract.require_same_signature(film["signature"], signature)
        if film.get("window") is not None:
            contract.require_same_window(film["window"], contract.interface_template()["window"])
    seen = set()
    joint_prior = {"channel": "joint", "candidates": [], "model": signature["id"], "revision": signature["revision"],
                   "processingId": signature["processing_id"], "dimension": 768, "normalized": True,
                   "inputVersion": contract.INPUT_VERSION, "embeddingTask": "video_audio"}
    text_prior = {**joint_prior, "channel": "text", "inputVersion": "transcript-document-cache-v1", "embeddingTask": "document"}
    joint_rows = []
    for film in films:
        record = contract.film_record(film)
        if record["itemId"] in seen:
            raise contract.ContractError("duplicate itemId")
        seen.add(record["itemId"])
        planned = contract.plan_windows(film["source"]["runtimeTicks"])
        actual = [(_span(row)[0], _span(row)[1]) for row in film["joint_rows"]]
        if actual != [(start, end) for start, end in planned]:
            raise contract.ContractError("joint rows do not match this item's runtime")
        if film.get("expected_documents") is not None and len(film["text_rows"]) != int(film["expected_documents"]):
            raise contract.ContractError("document count does not match the frozen transcription")
        if not film["text_rows"]:
            raise contract.ContractError("text representation is missing")
        joint_prior, _, _ = importer.build_bundle(joint_prior, film["source"], film["joint_rows"], references, query_version, signature)
        text_prior, _, _ = importer.build_bundle(text_prior, film["source"], film["text_rows"], references, query_version, signature)
        joint_rows.append((film, record))
    output.mkdir(parents=True, mode=0o700)
    target = output / "prepared-index.db"
    connection = sqlite3.connect(target)
    connection.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE libraries (library_id TEXT PRIMARY KEY);
        CREATE TABLE signatures (
          generation INTEGER PRIMARY KEY, model TEXT NOT NULL, revision TEXT NOT NULL,
          processing_id TEXT NOT NULL, dimension INTEGER NOT NULL, normalized INTEGER NOT NULL,
          input_version TEXT NOT NULL, window_version TEXT NOT NULL, audio_version TEXT NOT NULL,
          active INTEGER NOT NULL);
        CREATE TABLE media (
          item_id TEXT NOT NULL, media_source_id TEXT NOT NULL, generation INTEGER NOT NULL,
          library_id TEXT NOT NULL, content_version TEXT NOT NULL, runtime_ticks INTEGER NOT NULL,
          state TEXT NOT NULL, source_path TEXT NOT NULL DEFAULT '', has_audio INTEGER NOT NULL DEFAULT 0,
          audio_stream_index INTEGER, PRIMARY KEY (item_id, media_source_id, generation));
        CREATE TABLE windows (
          segment_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, media_source_id TEXT NOT NULL,
          generation INTEGER NOT NULL, content_version TEXT NOT NULL, start_ticks INTEGER NOT NULL,
          end_ticks INTEGER NOT NULL, state TEXT NOT NULL, vector BLOB, attempt INTEGER NOT NULL DEFAULT 0,
          lease_token TEXT, lease_until TEXT, failure_reason TEXT);
        CREATE TABLE events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, item_id TEXT,
          created_utc TEXT NOT NULL, handled INTEGER NOT NULL);
        CREATE TABLE candidate_representations (
          channel TEXT NOT NULL, version TEXT NOT NULL, payload_json TEXT NOT NULL,
          active INTEGER NOT NULL, PRIMARY KEY(channel, version));
        CREATE UNIQUE INDEX active_candidate_representation ON candidate_representations(channel) WHERE active=1;
        """)
    connection.execute("PRAGMA user_version = 3")
    settings = {
        "manual_pause": "1", "incompatible": "0", "related_minimum": "0.35",
        "threshold_calibrated": "0", "embedding_base_url": url, "active_generation": "1",
        "imported_candidates_only": "1",
    }
    with connection:
        for key, value in settings.items():
            connection.execute("INSERT INTO settings VALUES (?, ?)", (key, value))
        connection.execute("""INSERT INTO signatures VALUES (1, ?, ?, ?, 768, 1, ?, ?, ?, 1)""", (
            signature["id"], signature["revision"], signature["processing_id"], contract.INPUT_VERSION,
            contract.VERSION, contract.AUDIO_VERSION))
        for bundle in (joint_prior, text_prior):
            connection.execute("INSERT INTO candidate_representations VALUES (?, ?, ?, 1)", (
                bundle["channel"], bundle["version"], json.dumps(bundle, ensure_ascii=False)))
        for film, record in joint_rows:
            source = film["source"]
            item = record["itemId"]
            connection.execute("INSERT OR IGNORE INTO libraries VALUES (?)", (str(source["libraryId"]),))
            connection.execute("""INSERT INTO media VALUES (?, ?, 1, ?, ?, ?, 'complete', ?, 1, ?)""", (
                item, source["mediaSourceId"], str(source["libraryId"]), source["contentVersion"],
                int(source["runtimeTicks"]), source["sourcePath"], int(source["audioStreamIndex"])))
            by_id = {row["candidateId"]: row for row in joint_prior["candidates"] if row["itemId"] == item}
            for entry in film["joint_rows"]:
                start, end, candidate_id = _span(entry)
                row = by_id[candidate_id]
                segment = importer.native_segment_id(item, source["mediaSourceId"], source["contentVersion"], start, end, 1)
                blob = struct.pack("<768f", *row["vector"])
                connection.execute("""INSERT INTO windows
                    (segment_id, item_id, media_source_id, generation, content_version, start_ticks, end_ticks, state, vector, attempt)
                    VALUES (?, ?, ?, 1, ?, ?, ?, 'succeeded', ?, 1)""", (
                    segment, item, source["mediaSourceId"], source["contentVersion"], start, end, blob))
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise contract.ContractError("new index failed integrity check")
    stored = connection.execute("SELECT processing_id, window_version FROM signatures WHERE active=1").fetchone()
    if stored != (signature["processing_id"], contract.VERSION):
        raise contract.ContractError("stored signature does not match the supplied processing id")
    evidence = {
        "films": [{**record, "windows": len(film["joint_rows"]), "documents": len(film["text_rows"])} for film, record in joint_rows],
        "runLabelIsEpisodeNumber": False,
        "emptyNewIndex": True,
        "priorIndexOpened": False,
        "algorithm": importer.ALGORITHM,
        "windowVersion": contract.VERSION,
        "processingIdSupplied": True,
        "embeddingBaseUrl": url,
        "textCandidateMap": [item for film, _record in joint_rows for item in film.get("text_id_map", [])],
    }
    connection.close()
    contract.save_json(output / "import-evidence.json", evidence)
    return evidence


def import_films(root, film_runs, references, output):
    signature = contract.require_signature(root["signature"])
    contract.checked_window(root["window"])
    url = contract.require_isolated_base(root.get("embedding_base_url"))
    frozen = root.get("films") or []
    if len(frozen) != 3:
        raise contract.ContractError("root protocol must list the three frozen films")
    contract.refuse_write_target(output)
    if Path(output).exists():
        raise contract.ContractError("Choose a new output; never overwrite a prepared index")
    seen = []
    loaded = []
    for run in film_runs:
        contract.refuse_write_target(run)
        film_protocol = json.loads(Path(run).joinpath("protocol.json").read_text(encoding="utf-8"))
        item = match_frozen_film(root, film_protocol)
        if item in seen:
            raise contract.ContractError("frozen film appears more than once")
        seen.append(item)
        loaded.append((Path(run), film_protocol))
    if set(seen) != {contract.canonical_item(film["source"]["itemId"]) for film in frozen}:
        raise contract.ContractError("import must include each frozen film once")
    films = []
    for run, film_protocol in loaded:
        contract.require_closed(json.loads((run / "documents-state.json").read_text(encoding="utf-8")).get("status"), "documents")
        for stage in ("asr", "align"):
            contract._succeeded(json.loads((run / f"{stage}-tasks.json").read_text(encoding="utf-8")),
                                len(json.loads((run / "chunks.json").read_text(encoding="utf-8"))), stage)
        source = film_protocol["source"]
        rooted = dict(film_protocol)
        rooted["signature"] = signature
        rooted["window"] = root["window"]
        joint_entries = film_protocol["joint_windows"]
        original_text = (run / "candidates.json").read_bytes()
        text_entries = json.loads(original_text)
        for entry in joint_entries:
            require_embedding_closed(run, entry, "joint")
        joint_rows = [importer.candidate_row(entry, source, importer.verified_vector(run, entry, rooted, "joint"))
                      for entry in joint_entries]
        text_rows = []
        text_map = []
        for entry in text_entries:
            require_embedding_closed(run, entry, "text")
            vector = importer.verified_vector(run, entry, rooted, "text")
            namespaced, mapping = namespace_text_entry(source, entry)
            text_rows.append(importer.candidate_row(namespaced, source, vector, entry["text"]))
            text_map.append(mapping)
        if (run / "candidates.json").read_bytes() != original_text:
            raise contract.ContractError("ASR candidates were rewritten")
        films.append({**film_protocol, "source": source, "joint_rows": joint_rows, "text_rows": text_rows,
                      "text_id_map": text_map, "signature": signature, "window": root["window"],
                      "expected_documents": film_protocol.get("expected_documents")})
    query_paths = sorted((Path(references) / "responses").glob("*.json"))
    if len(query_paths) != 16:
        raise contract.ContractError("the new index needs the same sixteen reference queries, encoded with the new signature")
    query_version = importer.digest({path.name: importer.sha(path) for path in query_paths})
    vectors = []
    for path in query_paths:
        response = json.loads(path.read_text(encoding="utf-8"))
        importer.embedding.validate_signature(response, signature)
        if response["data"][0]["processing"] != {"modalities": ["text"], "task": "query"}:
            raise contract.ContractError("reference query processing does not match")
        vectors.append(importer.f32(response["data"][0]["embedding"]))
    return build_empty_index(films, vectors, signature, query_version, output, url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--film-run", type=Path, action="append", required=True)
    parser.add_argument("--references", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    evidence = import_films(protocol, args.film_run, args.references, args.output)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
