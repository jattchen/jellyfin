#!/usr/bin/env python3
import importlib.util
import json
import subprocess
import sys
import sqlite3
from pathlib import Path
import tempfile
import unittest
import uuid


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


embed = load("embed_for_sample_import_test", "full-film-embed.py")
importer = load("sample_c_import_test", "sample-c-import.py")
contract = importer.contract

ITEM = "11111111-1111-4111-8111-111111111111"
LIBRARY = "22222222-2222-4222-8222-222222222222"
SIGNATURE = {
    "id": contract.MODEL_ID, "revision": contract.REVISION,
    "processing_id": "eg2-7c43611ad2e20661c3d3", "dimension": 768, "normalized": True,
}
SCHEMA = """
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
"""


def unit(index):
    values = [0.0] * 768
    values[index] = 1.0
    return values


def response(processing, vector):
    return {
        "model": SIGNATURE["id"], "revision": SIGNATURE["revision"],
        "processing_id": SIGNATURE["processing_id"], "dimension": 768, "normalized": True,
        "data": [{"index": 0, "embedding": vector, "processing": processing}],
    }


def joint_processing():
    return {
        "modalities": ["video", "audio"],
        "video": {
            "duration_seconds": 12.012, "audio_included": True, "has_audio": True,
            "sampled_seconds": [0, 3, 6, 9], "frame_count": 4, "profile": contract.PROFILE,
            "offsets_seconds": [0, 3, 6, 9], "window_seconds": 12, "max_soft_tokens": 70,
            "add_timestamps": False, "overflow_strategy": "reject",
        },
        "audio": {
            "duration_seconds": 12.0, "sample_rate": 16000, "channels": 1,
            "truncated": False, "coverage": "full_track",
        },
    }


def template(path, event_item=None):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute("PRAGMA user_version = 3")
    settings = {
        "manual_pause": "1", "imported_candidates_only": "1",
        "embedding_base_url": contract.ISOLATED_BASE, "incompatible": "0",
        "related_minimum": "0.35", "threshold_calibrated": "0", "active_generation": "0",
    }
    for key, value in settings.items():
        connection.execute("INSERT INTO settings VALUES (?, ?)", (key, value))
    connection.execute("INSERT INTO libraries VALUES (?)", (LIBRARY,))
    if event_item:
        connection.execute("INSERT INTO events (kind, item_id, created_utc, handled) VALUES ('old', ?, 't', 1)", (event_item,))
    connection.commit()
    connection.close()


def write_embedding(run, channel, entry, protocol, processing, vector, reused=False):
    body = response(processing, vector)
    directory = run / channel
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (entry["candidate_id"] + ".json")
    embed.save(target, body)
    clip_sha = None
    if channel == "joint":
        clip = run / "clips" / (entry["candidate_id"] + ".mp4")
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"clip-" + entry["candidate_id"].encode())
        clip_sha = embed.sha(clip)
        embed.save(clip.with_suffix(".clip.json"), {
            "sha256": clip_sha,
            "identity": {
                "source": embed.source_facts(protocol["source"]),
                "start_ticks": entry["start_ticks"],
                "end_ticks": entry["end_ticks"],
            },
        })
    input_hash, _ = embed.embedding_input(protocol, channel, entry, clip_sha)
    state = {
        "status": "succeeded", "input_hash": input_hash, "response_sha256": embed.sha(target),
        "model_input_reused": reused,
    }
    embed.save(target.with_suffix(".state.json"), state)


class TemplateGuards(unittest.TestCase):
    def test_live_name_and_active_writer_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(contract.ContractError):
                importer.require_template(root / "index.db")
            path = root / "empty.template.db"
            template(path)
            lock = root / "writer.lock"
            lock.touch()
            child = subprocess.Popen([sys.executable, "-c",
                "import fcntl,sys; f=open(sys.argv[1],'r+b'); fcntl.lockf(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read()",
                str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(contract.ContractError):
                    importer.require_template(path)
            finally:
                child.communicate("")
            self.assertEqual(importer.require_template(path), path)

    def test_shared_cache_does_not_change_joint_or_text_input_hash(self):
        protocol = {"signature": SIGNATURE, "window": contract.window_template()}
        entry = {"candidate_id": "t", "text": "同一句", "start_ticks": 1, "end_ticks": 2}
        before_text, _ = embed.embedding_input(protocol, "text", entry)
        before_joint, _ = embed.embedding_input(protocol, "joint", entry, "abc")
        protocol["shared_document_cache"] = {"directory": "/tmp/shared-document-cache"}
        self.assertEqual(before_text, embed.embedding_input(protocol, "text", entry)[0])
        self.assertEqual(before_joint, embed.embedding_input(protocol, "joint", entry, "abc")[0])


class SampleImport(unittest.TestCase):
    def film(self, root, texts):
        source_path = root / "film.bin"
        source_path.write_bytes(b"original-film")
        stat = source_path.stat()
        source = {
            "itemId": ITEM, "mediaSourceId": "mediasource", "libraryId": LIBRARY,
            "contentVersion": "film-content", "runtimeTicks": 1_392_853_330,
            "container": "mp4", "audioStreamIndex": 1, "sourceItemId": ITEM,
            "sourcePath": str(source_path), "sizeBytes": stat.st_size,
            "modifiedTicks": stat.st_mtime_ns // 100 + 621355968000000000,
        }
        cache = {"directory": str(root / "shared-document-cache")}
        windows = contract.window_entries(source, 100_000_000, 1_300_000_000)
        protocol = {
            "version": "issue3-c-sample-film-v1", "fullFilm": False, "coverage": "partial",
            "label": "C1", "window": contract.window_template(),
            "embedding_base_url": contract.ISOLATED_BASE, "max_inflight_embeddings": 1,
            "signature": SIGNATURE, "source": source,
            "sample": {
                "sampleStartTicks": 100_000_000, "sampleEndTicks": 1_300_000_000,
                "absoluteOffsetTicks": 100_000_000, "excerptRuntimeTicks": contract.SAMPLE_TICKS,
            },
            "shared_document_cache": cache, "joint_windows": windows,
        }
        run = root / "embed-C1"
        run.mkdir()
        embed.save(run / "protocol.json", protocol)
        embed.save(run / "candidates.json", texts)
        embed.save(run / "transcription-check.json", {
            "status": "succeeded", "fullFilm": False, "absoluteOffsetTicks": 100_000_000,
            "candidateCount": len(texts), "contentVersion": source["contentVersion"],
            "sourcePath": source["sourcePath"],
        })
        for entry in windows:
            write_embedding(run, "joint", entry, protocol, joint_processing(), unit(0))
        for index, entry in enumerate(texts):
            write_embedding(run, "text", entry, protocol, {"modalities": ["text"], "task": "document"}, unit(index + 1), reused=index == 1)
        return run, protocol

    def test_source_drift_is_rejected_before_vectors_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, protocol = self.film(root, [])
            frozen = dict(protocol["source"])
            frozen["contentVersion"] = "changed"
            master = dict(protocol, films=[{"source": frozen}])
            with self.assertRaisesRegex(contract.ContractError, "contentVersion"):
                importer.load_film(run, master, SIGNATURE, protocol["shared_document_cache"]["directory"])

    def test_successful_empty_transcription_keeps_joint_search_and_unknown_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, protocol = self.film(root, [])
            master = dict(protocol, films=[{"source": protocol["source"]}])
            references = root / "responses"
            references.mkdir()
            for index in range(16):
                embed.save(references / f"q{index:02d}.json", response(
                    {"modalities": ["text"], "task": "query"}, unit(index)))
            template_path = root / "empty.template.db"
            template(template_path)
            output = root / "empty-text-index"
            importer.import_films(master, [run], root, template_path, output)
            connection = sqlite3.connect(output / "prepared-index.db")
            bundles = {row[0]: json.loads(row[1]) for row in connection.execute(
                "SELECT channel,payload_json FROM candidate_representations")}
            connection.close()
            self.assertEqual(len(bundles["joint"]["candidates"]), 12)
            self.assertEqual(bundles["text"]["candidates"], [])
            self.assertTrue(bundles["text"]["transcriptionChecked"])
            check_path = run / "transcription-check.json"
            check = json.loads(check_path.read_text())
            check["status"] = "unknown"
            embed.save(check_path, check)
            unknown_output = root / "unknown-index"
            with self.assertRaises(contract.ContractError):
                importer.import_films(master, [run], root, template_path, unknown_output)
            self.assertFalse(unknown_output.exists())

    def test_partial_sample_replaces_only_the_template_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            texts = [
                {"candidate_id": "c0000", "text": "同一句", "start_ticks": 100_000_000, "end_ticks": 150_000_000, "full_film": False},
                {"candidate_id": "c0001", "text": "同一句", "start_ticks": 200_000_000, "end_ticks": 260_000_000, "full_film": False},
            ]
            run, protocol = self.film(root, texts)
            before = (run / "protocol.json").read_bytes()
            template_path = root / "empty.template.db"
            template(template_path)
            connection = sqlite3.connect(template_path)
            connection.execute("INSERT INTO libraries VALUES (?)", (str(uuid.uuid4()),))
            connection.commit()
            connection.close()
            references = root / "responses"
            references.mkdir()
            for index in range(16):
                embed.save(references / f"q{index:02d}.json", response(
                    {"modalities": ["text"], "task": "query"}, unit(index)))
            output = root / "new-index"
            evidence = importer.import_films(
                {"fullFilm": False, "window": protocol["window"], "signature": SIGNATURE,
                 "embedding_base_url": contract.ISOLATED_BASE, "shared_document_cache": protocol["shared_document_cache"],
                 "films": [{"source": protocol["source"]}]},
                [run], references.parent, template_path, output)
            self.assertEqual((run / "protocol.json").read_bytes(), before)
            self.assertFalse(evidence["fullFilm"])
            self.assertEqual(evidence["coverage"], "partial")
            self.assertEqual(evidence["films"][0]["state"], "partial")
            self.assertEqual(evidence["films"][0]["runtimeTicks"], 1_392_853_330)
            self.assertEqual(evidence["films"][0]["windows"], 12)
            self.assertEqual(evidence["films"][0]["documents"], 2)
            self.assertEqual(evidence["reusedTextInputs"], 1)
            self.assertEqual(evidence["newModelInputsDuringImport"], 0)
            self.assertEqual(evidence["sharedDocumentCache"], protocol["shared_document_cache"]["directory"])
            self.assertFalse(evidence["liveHostIndexOpened"])
            connection = sqlite3.connect(output / "prepared-index.db")
            media = connection.execute("SELECT state, runtime_ticks, source_path, content_version FROM media").fetchone()
            self.assertEqual(media, ("partial", 1_392_853_330, protocol["source"]["sourcePath"], "film-content"))
            self.assertEqual(connection.execute("SELECT library_id FROM libraries").fetchall(), [(LIBRARY,)])
            self.assertEqual(connection.execute("SELECT window_version, processing_id FROM signatures").fetchone(),
                             (contract.VERSION, SIGNATURE["processing_id"]))
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='embedding_base_url'").fetchone()[0], contract.ISOLATED_BASE)
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='manual_pause'").fetchone()[0], "1")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM windows").fetchone()[0], 12)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media").fetchone()[0], 1)
            connection.close()


if __name__ == "__main__":
    unittest.main()
