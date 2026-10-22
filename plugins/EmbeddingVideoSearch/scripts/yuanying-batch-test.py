#!/usr/bin/env python3
"""Synthetic checks for one twelve-second directory pass. No model and no live index."""
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path
import tempfile


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch = load("yuanying_batch_test", "yuanying-batch.py")
contract = batch.contract
embed = batch.embed

LIBRARY = "22222222-2222-4222-8222-222222222222"
SIGNATURE = {
    "id": contract.MODEL_ID, "revision": contract.REVISION,
    "processing_id": batch.EXPECTED_PROCESSING_ID, "dimension": 768, "normalized": True,
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
    values[index % 768] = 1.0
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
            "duration_seconds": 12.0, "audio_included": True, "has_audio": True,
            "sampled_seconds": [0, 3, 6, 9], "frame_count": 4, "profile": contract.PROFILE,
            "offsets_seconds": [0, 3, 6, 9], "window_seconds": 12, "max_soft_tokens": 70,
            "add_timestamps": False, "overflow_strategy": "reject",
        },
        "audio": {
            "duration_seconds": 12.0, "sample_rate": 16000, "channels": 1,
            "truncated": False, "coverage": "full_track",
        },
    }


def document_processing():
    return {"modalities": ["text"], "task": "document"}


def template(path):
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
    connection.commit()
    connection.close()


def references(root):
    directory = root / "refs" / "responses"
    directory.mkdir(parents=True)
    for index in range(16):
        embed.save(directory / f"q{index:02d}.json", response(
            {"modalities": ["text"], "task": "query"}, unit(index)))
    return directory.parent


def write_facts(path, rows):
    batch.write_json(path, rows)
    return path


def source_file(directory, name, item, runtime, presence="Readable", label=None, probe_token=None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"synthetic-film")
    stat = path.stat()
    source = {
        "itemId": item, "mediaSourceId": "mediasource", "libraryId": LIBRARY,
        "contentVersion": "content-" + item[:8], "runtimeTicks": runtime,
        "container": "mp4", "audioStreamIndex": 1, "sourceItemId": item,
        "sourcePath": str(path), "sizeBytes": stat.st_size,
        "modifiedTicks": stat.st_mtime_ns // 100 + 621355968000000000,
    }
    if presence is not None:
        source["presence"] = presence
    row = {"source": source, "probe": {"format": {"marker": probe_token or "probe"}}}
    if label:
        row["label"] = label
        row["path"] = str(path)
    return row


def write_model_fixture(directory, *, config=True, vocab=True, merges=True, weights=True, index=True,
                        shard_name="model.safetensors"):
    """Tiny stand-in for a version directory. These bytes are not model weights."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if config is True:
        (directory / "config.json").write_text('{"model_type":"fixture"}\n', encoding="utf-8")
    elif isinstance(config, str):
        (directory / "config.json").write_text(config, encoding="utf-8")
    if vocab:
        (directory / "vocab.json").write_text('{"a":1}\n', encoding="utf-8")
    if merges:
        (directory / "merges.txt").write_text("a b\n", encoding="utf-8")
    if weights:
        (directory / shard_name).write_bytes(b"fixture-weight")
    if index:
        (directory / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"fixture": shard_name}}), encoding="utf-8")
    return directory


def write_parent_model(parent):
    """A directory that only contains a revision child. The parent has no config.json."""
    parent = Path(parent)
    write_model_fixture(parent / "revision")
    return parent


def config_for(root, rows, output):
    media = root / "media"
    worker = root / "worker.py"
    worker.write_text("raise SystemExit(0)\n", encoding="utf-8")
    write_model_fixture(root / "asr-model")
    write_model_fixture(root / "align-model")
    facts = write_facts(root / "facts.json", rows)
    return {
        "sourceDirectory": str(media),
        "sourceFacts": str(facts),
        "outputRoot": str(output),
        "workerPython": str(worker),
        "asrModel": str(root / "asr-model"),
        "alignerModel": str(root / "align-model"),
        "embeddingBaseUrl": contract.ISOLATED_BASE,
        "processingId": SIGNATURE["processing_id"],
        "maxInflightEmbeddings": 1,
        "transcriptionLanguagePolicy": "auto-v1",
    }


def succeed_transcription(film_dir, candidates):
    asr = film_dir / "asr"
    batch.write_json(asr / "prepare.json", {"status": "succeeded"})
    batch.write_json(asr / "chunks.json", [])
    batch.write_json(asr / "asr-tasks.json", [])
    batch.write_json(asr / "align-tasks.json", [])
    batch.write_json(asr / "documents-state.json", {"status": "succeeded"})
    batch.write_json(asr / "candidates.json", candidates)


def write_embedding(run, channel, entry, protocol, processing, vector):
    body = response(processing, vector)
    target = run / channel / (entry["candidate_id"] + ".json")
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
    digest, _window = embed.embedding_input(protocol, channel, entry, clip_sha)
    embed.save(target.with_suffix(".state.json"), {
        "status": "succeeded", "input_hash": digest, "response_sha256": embed.sha(target),
        "model_input_reused": False, "forgedInputHash": False,
    })


def finish_embeddings(film_dir, texts=None):
    succeed_transcription(film_dir, texts or [])
    batch.publish_transcription(film_dir / "asr", film_dir / "embed")
    protocol = batch.read_json(film_dir / "embed" / "protocol.json")
    for entry in protocol["joint_windows"]:
        write_embedding(film_dir / "embed", "joint", entry, protocol, joint_processing(), unit(0))
    for index, entry in enumerate(texts or []):
        write_embedding(film_dir / "embed", "text", entry, protocol, document_processing(), unit(index + 1))
    return protocol


class WindowAndReuse(unittest.TestCase):
    def test_tail_is_kept_and_short_windows_are_rejected(self):
        twenty = batch.plan_film_windows(200_000_000)
        self.assertEqual(twenty, [(0, 120_000_000), (80_000_000, 200_000_000)])
        self.assertNotEqual(twenty[-1][0] % contract.STEP_TICKS, 0)
        with self.assertRaises(batch.BatchBlocked) as caught:
            batch.plan_film_windows(contract.LENGTH_TICKS - 1)
        self.assertEqual(caught.exception.reason, "shorter-than-12s")

    def test_sample_windows_are_not_reused_when_only_the_clock_matches(self):
        spans = contract.interface_template()["confirmedSpans"]
        runtimes = {"C1": 1_392_853_330, "C2": 20_000_000_000, "C3": 100_000_000_000}
        coincident = tail = reused = other = 0
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, (start, end) in spans.items():
                item = str(uuid.uuid4())
                source = source_file(root, label + ".mp4", item, runtimes[label])["source"]
                self.assertEqual(source["presence"], "Readable")
                prior_source = {key: source[key] for key in batch.SOURCE_FIELDS}
                self.assertNotIn("presence", prior_source)
                self.assertTrue(batch.same_source(source, prior_source))
                sample_windows = contract.window_entries(prior_source, start, end)
                film_windows = batch.window_entries(source)
                planned = {(entry["start_ticks"], entry["end_ticks"]) for entry in film_windows}
                film_by_span = {(entry["start_ticks"], entry["end_ticks"]): entry for entry in film_windows}
                prior = {
                    "signature": SIGNATURE, "window": contract.window_template(),
                    "source": prior_source, "joint_windows": sample_windows,
                }
                film = {
                    "signature": SIGNATURE, "window": contract.window_template(),
                    "source": source, "joint_windows": film_windows,
                }
                states = {}
                for entry in sample_windows:
                    digest, _window = embed.embedding_input(prior, "joint", entry, "clip")
                    states[entry["candidate_id"]] = {"status": "succeeded", "input_hash": digest}
                    span = (entry["start_ticks"], entry["end_ticks"])
                    decision = batch.reuse_decision(
                        film, film_by_span.get(span, film_windows[0]), prior, entry,
                        states[entry["candidate_id"]], "clip")
                    self.assertFalse(decision["reused"])
                    self.assertFalse(decision["forgedInputHash"])
                    if span not in planned:
                        tail += 1
                        self.assertEqual(decision["reason"], "tail-boundary-mismatch")
                    else:
                        coincident += 1
                        self.assertEqual(decision["reason"], "input-hash-not-equivalent")
                        other += decision["reason"] not in {"input-hash-not-equivalent", "tail-boundary-mismatch"}
                self.assertTrue(all(entry["full_film"] is False for entry in sample_windows))
                self.assertTrue(all(entry["full_film"] is True for entry in film_windows))
        self.assertEqual((coincident, tail, reused, other), (33, 3, 0, 0))

    def test_matching_tail_still_needs_an_equal_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = 1_300_000_000
            source = source_file(root, "tail.mp4", str(uuid.uuid4()), runtime)["source"]
            sample_windows = contract.window_entries(source, 100_000_000, runtime)
            film_windows = batch.window_entries(source)
            planned = {(entry["start_ticks"], entry["end_ticks"]) for entry in film_windows}
            self.assertTrue(all((entry["start_ticks"], entry["end_ticks"]) in planned for entry in sample_windows))
            prior = {"signature": SIGNATURE, "window": contract.window_template(), "source": source, "joint_windows": sample_windows}
            film = {"signature": SIGNATURE, "window": contract.window_template(), "source": source, "joint_windows": film_windows}
            by_span = {(entry["start_ticks"], entry["end_ticks"]): entry for entry in film_windows}
            for entry in sample_windows:
                digest, _window = embed.embedding_input(prior, "joint", entry, "clip")
                decision = batch.reuse_decision(
                    film, by_span[(entry["start_ticks"], entry["end_ticks"])], prior, entry,
                    {"status": "succeeded", "input_hash": digest}, "clip")
                self.assertEqual(decision["reason"], "input-hash-not-equivalent")
                self.assertFalse(decision["reused"])

    def test_hash_changes_with_candidate_identity_and_wrong_cache_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = source_file(root, "one.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS)["source"]
            entry = batch.window_entries(source)[0]
            protocol = {"signature": SIGNATURE, "window": contract.window_template(), "source": source}
            original, _window = embed.embedding_input(protocol, "joint", entry, "clip")
            flipped = dict(entry, full_film=False)
            renamed = dict(entry, candidate_id=entry["candidate_id"] + "-other")
            self.assertNotEqual(original, embed.embedding_input(protocol, "joint", flipped, "clip")[0])
            self.assertNotEqual(original, embed.embedding_input(protocol, "joint", renamed, "clip")[0])
            prior_path = root / "prior.json"
            prior_path.write_bytes(b"prior-response")
            before = prior_path.read_bytes()
            decision = {
                "reused": False, "inputHashEquivalent": False, "forgedInputHash": False,
                "reason": "cache-hash-mismatch", "priorInputHash": "stored", "filmInputHash": original,
            }
            self.assertIsNone(batch.adopt_equivalent(decision, prior_path, root / "dest", entry))
            self.assertEqual(prior_path.read_bytes(), before)
            self.assertFalse((root / "dest").exists())

    def test_equal_hash_copies_into_the_new_run_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = source_file(root, "one.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS)["source"]
            entry = batch.window_entries(source)[0]
            protocol = {
                "signature": SIGNATURE, "window": contract.window_template(),
                "source": source, "joint_windows": [entry],
            }
            digest, _window = embed.embedding_input(protocol, "joint", entry, "clip")
            decision = batch.reuse_decision(
                protocol, entry, protocol, entry, {"status": "succeeded", "input_hash": digest}, "clip")
            self.assertEqual(decision["reason"], "equivalent-checkpoint")
            self.assertTrue(decision["inputHashEquivalent"])
            self.assertFalse(decision["reused"])
            prior = root / "sample"
            prior.mkdir()
            response_path = prior / "old.json"
            response_path.write_bytes(b"same-response")
            before = batch.snapshot_tree(prior)
            state = batch.adopt_equivalent(decision, response_path, root / "new-run", entry)
            self.assertEqual(state["input_hash"], digest)
            self.assertFalse(state["forgedInputHash"])
            self.assertEqual(batch.snapshot_tree(prior), before)
            self.assertEqual((root / "new-run" / "joint" / (entry["candidate_id"] + ".json")).read_bytes(), b"same-response")

    def test_audit_keeps_presence_out_of_identity_and_does_not_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = 1_392_853_330
            filmed = source_file(root / "media", "film.mp4", str(uuid.uuid4()), runtime, label="F001")
            source = filmed["source"]
            prior_source = {key: source[key] for key in batch.SOURCE_FIELDS}
            output = root / "output"
            config = config_for(root, [filmed], output)
            batch.preflight(config)
            protocol = batch.read_json(output / "films" / "F001" / "embed" / "protocol.json")
            self.assertEqual(protocol["source"]["presence"], "Readable")
            sample = root / "sample-embed" / "embed-C1"
            (sample / "joint").mkdir(parents=True)
            (sample / "clips").mkdir()
            sample_windows = contract.window_entries(prior_source, 100_000_000, 1_300_000_000)
            prior = {
                "signature": SIGNATURE, "window": contract.window_template(),
                "source": prior_source, "joint_windows": sample_windows,
            }
            batch.write_json(sample / "protocol.json", prior)
            for entry in sample_windows:
                digest, _window = embed.embedding_input(prior, "joint", entry, "clip")
                batch.write_json(sample / "joint" / (entry["candidate_id"] + ".state.json"), {
                    "status": "succeeded", "input_hash": digest,
                })
                batch.write_json(sample / "clips" / (entry["candidate_id"] + ".clip.json"), {"sha256": "clip"})
            before = batch.snapshot_tree(root / "sample-embed")
            summary = batch.audit_sample_root(output, root / "sample-embed")
            self.assertEqual(summary["coincidentNotEquivalent"], 11)
            self.assertEqual(summary["tailMismatch"], 1)
            self.assertEqual(summary["reused"], 0)
            self.assertTrue(summary["priorUnchanged"])
            self.assertEqual(batch.snapshot_tree(root / "sample-embed"), before)
            self.assertFalse((output / "films" / "F001" / "embed" / "joint").exists())
            audit = batch.read_json(output / "films" / "F001" / "sample-reuse-audit.json")
            reasons = {}
            for record in audit["records"]:
                reasons[record["reason"]] = reasons.get(record["reason"], 0) + 1
                self.assertFalse(record["reused"])
            self.assertEqual(reasons, {"input-hash-not-equivalent": 11, "tail-boundary-mismatch": 1})
            drifted = dict(prior_source, contentVersion="changed")
            decision = batch.reuse_decision(
                protocol, protocol["joint_windows"][1], {"source": drifted, "signature": SIGNATURE},
                sample_windows[0], {"status": "succeeded", "input_hash": "x"}, "clip")
            self.assertEqual(decision["reason"], "source-mismatch")
            other_signature = dict(SIGNATURE, processing_id="eg2-ffffffffffffffff")
            decision = batch.reuse_decision(
                protocol, protocol["joint_windows"][1],
                {"source": prior_source, "signature": other_signature},
                sample_windows[0], {"status": "succeeded", "input_hash": "x"}, "clip")
            self.assertEqual(decision["reason"], "signature-mismatch")

    def test_sample_clip_identity_stays_false_until_the_row_says_full(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            item = str(uuid.uuid4())
            source = {
                "itemId": item, "audioStreamIndex": 1, "runtimeTicks": contract.LENGTH_TICKS,
                "contentVersion": "content", "sourcePath": str(run / "film.mp4"),
            }
            probe = {"streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0", "disposition": {}},
                {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            ]}
            entry = {"candidate_id": "joint-sample-0000", "start_ticks": 0, "end_ticks": contract.LENGTH_TICKS, "full_film": False}
            facts = {"real_path": source["sourcePath"], "size": 1, "mtime_ns": 2}
            clip = run / "clips" / (entry["candidate_id"] + ".mp4")
            clip.parent.mkdir(parents=True)
            clip.write_bytes(b"cached-clip")
            identity = {
                "source": facts, "start_ticks": 0, "end_ticks": contract.LENGTH_TICKS,
                "profile": contract.INPUT_VERSION, "threads": 2, "audio_stream_index": 1,
                "window_version": contract.VERSION, "video_map": "0:v:0", "audio_map": "0:1",
                "time_origin": "container", "video_start_added": False, "video_start_time_raw": "0",
                "attached_picture_stream_indexes": [], "item_id": contract.canonical_item(item),
                "content_version": "content", "source_path": source["sourcePath"], "full_film": False,
            }
            embed.save(clip.with_suffix(".clip.json"), {"sha256": embed.sha(clip), "identity": identity})
            _path, state = contract.clip_window(
                run, entry, source, contract.window_template(), probe, embed.sha, embed.save, embed.read,
                lambda _source: facts)
            self.assertFalse(state["identity"]["full_film"])
            with self.assertRaises(contract.ContractError):
                contract.clip_window(
                    run, dict(entry, full_film=True), source, contract.window_template(), probe,
                    embed.sha, embed.save, embed.read, lambda _source: facts)


class CommandLogs(unittest.TestCase):
    def test_two_consecutive_stages_reuse_the_log_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory) / "films" / "F001" / "logs"
            codes = []
            for stage in ("prepare", "asr"):
                command = [sys.executable, "-c", "import sys; sys.stdout.write(sys.argv[1])", stage]
                codes.append(batch.execute_command(command, logs / (stage + ".log")))
            self.assertEqual(codes, [0, 0])
            self.assertEqual((logs / "prepare.log").read_text(encoding="utf-8"), "prepare")
            self.assertEqual((logs / "asr.log").read_text(encoding="utf-8"), "asr")
            self.assertTrue(logs.is_dir())


class TranscriptionBoundaries(unittest.TestCase):
    def film(self, root, runtime=contract.LENGTH_TICKS):
        item = str(uuid.uuid4())
        row = source_file(root / "media", "film.mp4", item, runtime, label="F001")
        output = root / "output"
        summary = batch.preflight(config_for(root, [row], output))
        self.assertTrue(summary["ready"])
        return output / "films" / "F001"

    def test_failed_transcription_is_not_a_successful_empty_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            film_dir = self.film(Path(directory))
            asr = film_dir / "asr"
            batch.write_json(asr / "prepare.json", {"status": "succeeded"})
            batch.write_json(asr / "chunks.json", [{"chunk_id": "c0"}])
            batch.write_json(asr / "asr-tasks.json", [{"chunk_id": "c0", "status": "failed"}])
            batch.write_json(asr / "align-tasks.json", [])
            batch.write_json(asr / "documents-state.json", {"status": "succeeded"})
            batch.write_json(asr / "candidates.json", [])
            before = batch.snapshot_tree(asr)
            assessed = batch.assess_film(film_dir)
            self.assertEqual(assessed["coverage"], "failed")
            self.assertFalse(assessed["successfulEmpty"])
            self.assertEqual(assessed["reason"], "transcription-failed")
            with self.assertRaises(batch.BatchBlocked):
                batch.publish_transcription(asr, film_dir / "embed")
            self.assertEqual(batch.snapshot_tree(asr), before)
            self.assertFalse((film_dir / "embed" / "candidates.json").exists())

    def test_unknown_checkpoint_is_not_sent_again(self):
        entry = {"candidate_id": "joint-1", "start_ticks": 0, "end_ticks": contract.LENGTH_TICKS}
        for status in ("completion-unknown", "inflight", "started", "terminal-invalid"):
            sent = []
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.dispatch_entries(
                    [entry], lambda _entry: {"status": status}, sent.append, {}, "joint", lambda _entry: None)
            self.assertEqual(sent, [])
            self.assertEqual(caught.exception.reason, "not-succeeded")
            self.assertEqual(caught.exception.status, status)

    def test_interrupted_stage_is_not_rerun_and_only_missing_window_is_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            film_dir = self.film(root, 200_000_000)
            asr = film_dir / "asr"
            batch.write_json(asr / "prepare.json", {"status": "succeeded"})
            batch.write_json(asr / "chunks.json", [{"chunk_id": "c0"}])
            calls = []
            assessed = batch.step_film(film_dir, execute=lambda stage: calls.append(stage), send=lambda _entry: None)
            self.assertEqual(calls, ["asr"])
            self.assertEqual(assessed["nextAction"]["stage"], "asr")
            batch.write_json(asr / "asr-tasks.json", [{"chunk_id": "c0", "status": "started"}])
            calls.clear()
            assessed = batch.step_film(film_dir, execute=lambda stage: calls.append(stage), send=lambda _entry: None)
            self.assertEqual(calls, [])
            self.assertEqual(assessed["reason"], "interrupted-not-retried")
            succeed_transcription(film_dir, [])
            batch.publish_transcription(asr, film_dir / "embed")
            protocol = batch.read_json(film_dir / "embed" / "protocol.json")
            first, second = protocol["joint_windows"]
            write_embedding(film_dir / "embed", "joint", first, protocol, joint_processing(), unit(0))
            sent = []
            result = batch.dispatch_entries(
                protocol["joint_windows"],
                lambda entry: batch.state_for(film_dir / "embed", "joint", entry),
                sent.append, protocol, "joint",
                lambda entry: batch.joint_clip_sha(film_dir / "embed", entry))
            self.assertEqual(sent, [second])
            self.assertEqual(result["sent"], [second["candidate_id"]])
            self.assertEqual(result["reused"], [first["candidate_id"]])
            self.assertFalse(result["resentUnknown"])

    def test_unknown_stops_the_batch_before_another_film(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = source_file(root / "media", "a.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            second = source_file(root / "media", "b.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F002")
            output = root / "output"
            config = config_for(root, [first, second], output)
            batch.preflight(config)
            asr = output / "films" / "F001" / "asr"
            batch.write_json(asr / "prepare.json", {"status": "succeeded"})
            batch.write_json(asr / "chunks.json", [{"chunk_id": "c0"}])
            batch.write_json(asr / "asr-tasks.json", [{"chunk_id": "c0", "status": "completion-unknown"}])
            calls = []
            code, summary = batch.run_batch(
                config, execute=lambda label, stage: calls.append((label, stage)) or 0,
                send=lambda label, stage, entry: calls.append(("send", label)))
            self.assertEqual(code, 3)
            self.assertEqual(summary["stopped"], "F001")
            self.assertEqual(calls, [])


class ModelLayoutAndBatchStop(unittest.TestCase):
    def film_config(self, root, output):
        row = source_file(root / "media", "film.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
        return config_for(root, [row], output)

    def two_film_config(self, root, output):
        first = source_file(root / "media", "a.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
        second = source_file(root / "media", "b.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F002")
        return config_for(root, [first, second], output)

    def assert_refused_before_output(self, config, output, reason):
        try:
            batch.preflight(config)
        except batch.BatchBlocked as exc:
            self.assertEqual(exc.reason, reason)
            self.assertFalse(output.exists())
            return
        self.fail("preflight accepted the model; output_created=" + str(output.exists()))

    def test_parent_directory_without_model_files_is_refused_before_output(self):
        for key in ("asrModel", "alignerModel"):
            with self.subTest(key=key):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    output = root / "output"
                    config = self.film_config(root, output)
                    config[key] = str(write_parent_model(root / "parent-model"))
                    self.assert_refused_before_output(config, output, "model-layout")

    def test_minimal_fixture_preflight_succeeds_without_loading_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            weight = root / "asr-model" / "model.safetensors"
            before = weight.read_bytes()
            summary = batch.preflight(config)
            self.assertTrue(summary["ready"])
            self.assertEqual(weight.read_bytes(), before)
            protocol = batch.read_json(output / "films" / "F001" / "asr" / "protocol.json")
            self.assertEqual(protocol["asr_model_path"], config["asrModel"])
            self.assertEqual(protocol["aligner_model_path"], config["alignerModel"])

    def test_missing_weight_tokenizer_or_shard_is_refused(self):
        defects = (
            {"vocab": False},
            {"merges": False},
            {"weights": False, "index": False},
            {"weights": False},
            {"config": "{"},
            {"config": "[]"},
            {"config": "{}"},
        )
        for defect in defects:
            with self.subTest(defect=sorted(defect.items())):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    output = root / "output"
                    config = self.film_config(root, output)
                    asr = root / "asr-model"
                    for child in list(asr.iterdir()):
                        child.unlink()
                    write_model_fixture(asr, **defect)
                    self.assert_refused_before_output(config, output, "model-layout")

    def test_index_shard_outside_the_model_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            outside = root / "outside.safetensors"
            outside.write_bytes(b"outside-weight")
            (root / "asr-model" / "model.safetensors.index.json").write_text(
                json.dumps({"weight_map": {"fixture": "../outside.safetensors"}}), encoding="utf-8")
            self.assert_refused_before_output(config, output, "model-layout")
            self.assertEqual(outside.read_bytes(), b"outside-weight")

    def test_existing_protocol_path_or_worker_drift_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            batch.preflight(config)
            protocol = output / "films" / "F001" / "asr" / "protocol.json"
            original = protocol.read_bytes()
            other_worker = root / "other-worker.py"
            other_worker.write_text("raise SystemExit(0)\n", encoding="utf-8")
            replacements = {
                "asrModel": str(write_model_fixture(root / "other-asr")),
                "alignerModel": str(write_model_fixture(root / "other-align")),
                "workerPython": str(other_worker),
            }
            for key, value in replacements.items():
                with self.subTest(key=key):
                    drifted = dict(config)
                    drifted[key] = value
                    try:
                        batch.preflight(drifted)
                    except batch.BatchBlocked as exc:
                        self.assertEqual(exc.reason, "protocol-drift")
                        self.assertEqual(protocol.read_bytes(), original)
                        continue
                    self.fail(key + " drift was ignored; protocol_unchanged=" + str(protocol.read_bytes() == original))

    def test_equivalent_canonical_path_keeps_the_stored_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            batch.preflight(config)
            protocol = output / "films" / "F001" / "asr" / "protocol.json"
            original = protocol.read_bytes()
            linked_worker = root / "worker-link.py"
            linked_worker.symlink_to(Path(config["workerPython"]))
            config["asrModel"] = str(Path(config["asrModel"]) / ".")
            config["alignerModel"] = str(Path(config["alignerModel"]) / ".")
            config["workerPython"] = str(linked_worker)
            summary = batch.preflight(config)
            self.assertTrue(summary["ready"])
            self.assertEqual(protocol.read_bytes(), original)
            stored = batch.read_json(protocol)
            self.assertFalse(stored["asr_model_path"].endswith("/."))
            self.assertNotEqual(stored["worker_python"], str(linked_worker))

    def test_ordinary_stage_nonzero_does_not_enter_the_next_film(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.two_film_config(root, output)
            calls = []

            def execute(label, stage):
                calls.append((label, stage))
                return 1

            code, summary = batch.run_batch(
                config, execute=execute, send=lambda label, stage, entry: calls.append(("send", label)))
            self.assertEqual(calls, [("F001", "prepare")])
            self.assertEqual(code, 2)
            self.assertEqual(summary["stopped"], "F001")
            self.assertNotEqual(summary["coverage"], "full")
            self.assertNotEqual(summary["films"][0]["coverage"], "full")

    def test_failed_checkpoint_stops_before_the_next_film(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.two_film_config(root, output)
            batch.preflight(config)
            asr = output / "films" / "F001" / "asr"
            batch.write_json(asr / "prepare.json", {"status": "succeeded"})
            batch.write_json(asr / "chunks.json", [{"chunk_id": "c0000"}])
            batch.write_json(asr / "asr-tasks.json", [{"chunk_id": "c0000", "status": "failed"}])
            before = asr.joinpath("asr-tasks.json").read_bytes()
            calls = []
            code, summary = batch.run_batch(
                config, execute=lambda label, stage: calls.append((label, stage)) or 0,
                send=lambda label, stage, entry: calls.append(("send", label)))
            self.assertEqual(calls, [])
            self.assertEqual(code, 2)
            self.assertEqual(summary["stopped"], "F001")
            self.assertNotEqual(summary["coverage"], "full")
            self.assertEqual(asr.joinpath("asr-tasks.json").read_bytes(), before)

    def test_timeout_stops_before_the_next_film(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.two_film_config(root, output)
            calls = []

            def execute(label, stage):
                calls.append((label, stage))
                asr = output / "films" / label / "asr"
                if stage == "prepare":
                    batch.write_json(asr / "prepare.json", {"status": "succeeded"})
                    batch.write_json(asr / "chunks.json", [{"chunk_id": "c0000"}])
                    return 0
                if stage == "asr":
                    batch.write_json(asr / "asr-tasks.json", [{
                        "chunk_id": "c0000", "status": "unknown", "reason": "timeout",
                    }])
                    return 1
                return 1

            code, summary = batch.run_batch(
                config, execute=execute, send=lambda label, stage, entry: calls.append(("send", label)))
            self.assertEqual(calls, [("F001", "prepare"), ("F001", "asr")])
            self.assertEqual(code, 3)
            self.assertEqual(summary["stopped"], "F001")
            self.assertNotEqual(summary["coverage"], "full")
            task = batch.read_json(output / "films" / "F001" / "asr" / "asr-tasks.json")
            self.assertEqual(task[0]["status"], "unknown")
            self.assertEqual(task[0]["reason"], "timeout")


class PrepareAndImport(unittest.TestCase):
    def test_preflight_keeps_readable_presence_and_hides_private_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            later = "ffffffff-ffff-4fff-8fff-ffffffffffff"
            earlier = "00000000-0000-4000-8000-000000000001"
            rows = [
                source_file(root / "media", "SECRET_CLIP.mp4", later, contract.LENGTH_TICKS,
                            presence="Readable", label="F002", probe_token="SECRET_PROBE_TOKEN"),
                source_file(root / "media", "other.mp4", earlier, 200_000_000,
                            presence="Readable", label="F010", probe_token="SECRET_PROBE_TOKEN"),
            ]
            (root / "media" / "notes.txt").write_text("skip", encoding="utf-8")
            (root / "media" / "nested").mkdir()
            output = root / "output"
            config = config_for(root, rows, output)
            config_path = root / "config.json"
            batch.write_json(config_path, config)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = batch.main(["preflight", "--config", str(config_path)])
            self.assertEqual(code, 0, stderr.getvalue())
            shown = stdout.getvalue()
            self.assertNotIn("SECRET_CLIP", shown)
            self.assertNotIn("SECRET_PROBE_TOKEN", shown)
            self.assertNotIn("sourcePath", shown)
            summary = json.loads(shown)
            self.assertEqual([row["label"] for row in summary["catalog"]], ["F002", "F010"])
            self.assertEqual(summary["catalog"][0]["itemId"], later)
            self.assertEqual(summary["films"], 2)
            self.assertEqual(summary["skippedFiles"], 2)
            self.assertEqual(summary["modelConcurrency"], 1)
            manifest = batch.read_json(output / "manifest.json")
            self.assertEqual(manifest["films"][0]["source"]["presence"], "Readable")
            self.assertEqual(manifest["films"][0]["probe"]["format"]["marker"], "SECRET_PROBE_TOKEN")
            protocol = batch.read_json(output / "films" / "F002" / "embed" / "protocol.json")
            self.assertEqual(protocol["source"]["presence"], "Readable")
            self.assertTrue(protocol["joint_windows"][0]["full_film"])
            command = batch.transcribe_command(config, output / "films" / "F002" / "asr", "asr")
            self.assertEqual(command[0], config["workerPython"])
            self.assertTrue(command[1].endswith("full-film-transcribe.py"))
            self.assertNotIn("18783", " ".join(command))
            self.assertNotIn("18780", " ".join(command))

    def test_wrong_port_and_preserved_output_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout, stderr = io.StringIO(), io.StringIO()
            config_path = root / "config.json"
            batch.write_json(config_path, {"embeddingBaseUrl": "http://127.0.0.1:18783"})
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = batch.main(["preflight", "--config", str(config_path)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stderr.getvalue())["reason"], "wrong-port")
            preserved = root / "multilingual-auto-v3" / "output"
            row = source_file(root / "media", "film.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            config = config_for(root, [row], preserved)
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(config)
            self.assertEqual(caught.exception.reason, "preserved-artifact")

    def test_import_refuses_bad_sources_without_creating_an_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            film_dir, output, config = self.completed(root)
            destination = root / "imported"

            def refused(mutate, reason):
                mutate()
                target = destination / reason
                with self.assertRaises(batch.BatchBlocked) as caught:
                    batch.import_films(output, config["template"], config["references"], target)
                self.assertEqual(caught.exception.reason, reason)
                self.assertFalse(target.exists())

            manifest_path = output / "manifest.json"
            original = manifest_path.read_bytes()
            protocol_path = film_dir / "embed" / "protocol.json"
            protocol_bytes = protocol_path.read_bytes()

            def bad_signature():
                manifest = batch.read_json(manifest_path)
                manifest["signature"] = dict(manifest["signature"], processing_id="eg2-ffffffffffffffff")
                batch.write_json(manifest_path, manifest)

            def restore_manifest():
                manifest_path.write_bytes(original)

            refused(bad_signature, "signature-mismatch")
            restore_manifest()

            def bad_window():
                manifest = batch.read_json(manifest_path)
                manifest["window"] = dict(manifest["window"], version="10s-8s-5f70-tail-v1")
                batch.write_json(manifest_path, manifest)

            refused(bad_window, "window-mismatch")
            restore_manifest()

            def bad_source():
                protocol = batch.read_json(protocol_path)
                protocol["source"]["contentVersion"] = "changed"
                batch.write_json(protocol_path, protocol)

            refused(bad_source, "source-mismatch")
            protocol_path.write_bytes(protocol_bytes)

            def not_full():
                protocol = batch.read_json(film_dir / "asr" / "protocol.json")
                protocol["fullFilm"] = False
                batch.write_json(film_dir / "asr" / "protocol.json", protocol)

            refused(not_full, "not-full-film")
            asr_protocol = batch.read_json(film_dir / "asr" / "protocol.json")
            asr_protocol["fullFilm"] = True
            batch.write_json(film_dir / "asr" / "protocol.json", asr_protocol)

            def missing_tail():
                protocol = batch.read_json(protocol_path)
                shortened = dict(protocol["joint_windows"][0], end_ticks=60_000_000)
                protocol["joint_windows"] = [shortened]
                batch.write_json(protocol_path, protocol)

            refused(missing_tail, "coverage-incomplete")
            protocol_path.write_bytes(protocol_bytes)

            def forged():
                batch.write_json(film_dir / "asr" / "asr-tasks.json", [{"chunk_id": "c0", "status": "failed"}])
                batch.write_json(film_dir / "asr" / "chunks.json", [{"chunk_id": "c0"}])
                check = batch.read_json(film_dir / "embed" / "transcription-check.json")
                check["successfulEmpty"] = True
                batch.write_json(film_dir / "embed" / "transcription-check.json", check)

            refused(forged, "transcription-failed")
            succeed_transcription(film_dir, [])
            batch.publish_transcription(film_dir / "asr", film_dir / "embed")

            named = root / "index.db"
            named.write_bytes(b"live")
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.import_films(output, named, config["references"], destination / "live")
            self.assertEqual(caught.exception.reason, "live-index")
            self.assertEqual(named.read_bytes(), b"live")
            self.assertFalse((destination / "live").exists())

            dirty = root / "dirty.template.db"
            template(dirty)
            connection = sqlite3.connect(dirty)
            connection.execute(
                "INSERT INTO media VALUES ('x', 'y', 1, ?, 'v', 1, 'partial', '', 0, NULL)", (LIBRARY,))
            connection.commit()
            connection.close()
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.import_films(output, dirty, config["references"], destination / "dirty")
            self.assertEqual(caught.exception.reason, "import-rejected")
            self.assertFalse((destination / "dirty").exists())

    def test_complete_film_imports_and_partial_batch_omits_the_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            film_dir, output, config = self.completed(root, text=True)
            destination = root / "new-index"
            evidence = batch.import_films(output, config["template"], config["references"], destination)
            self.assertEqual(evidence["batchCoverage"], "full")
            self.assertEqual(evidence["newModelInputsDuringImport"], 0)
            self.assertFalse((destination / "index.db").exists())
            connection = sqlite3.connect(destination / "prepared-index.db")
            media = connection.execute("SELECT state, has_audio FROM media").fetchone()
            self.assertEqual(media, ("complete", 1))
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='manual_pause'").fetchone()[0], "1")
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='imported_candidates_only'").fetchone()[0], "1")
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='embedding_base_url'").fetchone()[0], contract.ISOLATED_BASE)
            self.assertEqual(connection.execute("SELECT window_version, processing_id FROM signatures").fetchone(),
                             (contract.VERSION, SIGNATURE["processing_id"]))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM windows WHERE state='succeeded'").fetchone()[0], 1)
            bundles = {row[0]: json.loads(row[1]) for row in connection.execute(
                "SELECT channel, payload_json FROM candidate_representations")}
            connection.close()
            item = batch.read_json(film_dir / "embed" / "protocol.json")["source"]["itemId"]
            prefix = "text-" + item.replace("-", "") + "-"
            self.assertTrue(bundles["text"]["candidates"][0]["candidateId"].startswith(prefix))
            self.assertNotIn("10s-8s-5f70-tail-v1", json.dumps(bundles))

            second = source_file(root / "media", "second.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F002")
            manifest = batch.read_json(output / "manifest.json")
            manifest["films"].append({
                "label": "F002",
                "itemId": contract.canonical_item(second["source"]["itemId"]),
                "source": batch.copy_source(second["source"]),
                "windows": 1,
                "probe": second["probe"],
            })
            batch.write_json(output / "manifest.json", manifest)
            second_dir = output / "films" / "F002"
            (second_dir / "asr").mkdir(parents=True)
            (second_dir / "embed").mkdir(parents=True)
            batch.write_json(second_dir / "asr" / "protocol.json", {
                "fullFilm": True, "transcription_language_policy": "auto-v1",
                "source": batch.copy_source(second["source"]),
            })
            batch.write_json(second_dir / "embed" / "protocol.json", {
                "fullFilm": True, "label": "F002", "signature": SIGNATURE,
                "window": contract.window_template(), "joint_windows": [],
                "source": batch.copy_source(second["source"]),
            })
            partial = root / "partial-index"
            evidence = batch.import_films(
                output, config["template"], config["references"], partial, allow_partial=True)
            self.assertEqual(evidence["batchCoverage"], "partial")
            self.assertEqual(evidence["omitted"][0]["label"], "F002")
            connection = sqlite3.connect(partial / "prepared-index.db")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM media").fetchone()[0], 1)
            connection.close()
            blocked = root / "blocked-index"
            with self.assertRaises(batch.BatchBlocked):
                batch.import_films(output, config["template"], config["references"], blocked)
            self.assertFalse(blocked.exists())

    def completed(self, root, text=False):
        item = str(uuid.uuid4())
        row = source_file(root / "media", "film.mp4", item, contract.LENGTH_TICKS, label="F001")
        texts = []
        if text:
            texts = [{
                "candidate_id": "c0000-U01", "text": "合成句",
                "start_ticks": 0, "end_ticks": 1_000_000, "full_film": True,
            }]
        output = root / "output"
        config = config_for(root, [row], output)
        batch.preflight(config)
        film_dir = output / "films" / "F001"
        finish_embeddings(film_dir, texts)
        self.assertEqual(batch.assess_film(film_dir)["coverage"], "full")
        config["template"] = str(root / "empty.template.db")
        template(config["template"])
        config["references"] = str(references(root))
        return film_dir, output, config


class LanguageGapAndSelection(unittest.TestCase):
    def film_config(self, root, output, count=1):
        rows = []
        for index in range(count):
            label = f"F{index + 1:03d}"
            rows.append(source_file(root / "media", f"film-{label}.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label=label))
        return config_for(root, rows, output)

    def settle_gap(self, output):
        film = output / "films" / "F001"
        protocol = batch.read_json(film / "asr" / "protocol.json")
        protocol["unsupported_align_language_policy"] = batch.UNSUPPORTED_ALIGN_SKIP_POLICY
        batch.write_json(film / "asr" / "protocol.json", protocol)
        batch.write_json(film / "asr" / "prepare.json", {"status": "succeeded"})
        batch.write_json(film / "asr" / "chunks.json", [{"chunk_id": "c0000"}])
        batch.write_json(film / "asr" / "asr-tasks.json", [{"chunk_id": "c0000", "status": "succeeded"}])
        batch.write_json(film / "asr" / "align-tasks.json", [{
            "chunk_id": "c0000",
            "status": "skipped",
            "reason": "language-unsupported",
            "skip_policy": batch.UNSUPPORTED_ALIGN_SKIP_POLICY,
            "alignment_executed": False,
            "detected_language": "Dutch",
        }])
        batch.write_json(film / "asr" / "documents-state.json", {
            "status": "succeeded", "text_coverage": "gapped", "gap_count": 1,
        })
        batch.write_json(film / "asr" / "candidates.json", [])
        batch.write_json(film / "asr" / "align-language-gaps.json", [{
            "chunk_id": "c0000",
            "text": "Ik ah",
            "start_ticks": 0,
            "end_ticks": contract.LENGTH_TICKS,
            "reason": "language-unsupported",
            "detected_language": "Dutch",
            "skip_policy": batch.UNSUPPORTED_ALIGN_SKIP_POLICY,
            "alignment_executed": False,
            "embed": False,
            "text_retained": True,
            "input_text_sha256": "abc",
        }])
        batch.publish_transcription(film / "asr", film / "embed")
        embed_protocol = batch.read_json(film / "embed" / "protocol.json")
        for entry in embed_protocol["joint_windows"]:
            write_embedding(film / "embed", "joint", entry, embed_protocol, joint_processing(), unit(0))
        return film

    def test_unknown_skip_policy_is_refused_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            config["unsupportedAlignLanguagePolicy"] = "force-chinese"
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(config)
            self.assertEqual(caught.exception.reason, "language-policy")
            self.assertFalse(output.exists())

    def test_explicit_policy_is_stored_for_a_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            config["unsupportedAlignLanguagePolicy"] = batch.UNSUPPORTED_ALIGN_SKIP_POLICY
            summary = batch.preflight(config)
            self.assertEqual(summary["unsupportedAlignLanguagePolicy"], batch.UNSUPPORTED_ALIGN_SKIP_POLICY)
            protocol = batch.read_json(output / "films" / "F001" / "asr" / "protocol.json")
            self.assertEqual(protocol["unsupported_align_language_policy"], batch.UNSUPPORTED_ALIGN_SKIP_POLICY)
            self.assertEqual(protocol["transcription_language_policy"], "auto-v1")

    def test_gapped_film_keeps_audio_visual_and_does_not_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output, 2)
            batch.preflight(config)
            film = self.settle_gap(output)
            assessed = batch.assess_film(film)
            self.assertEqual(assessed["audioVisualCoverage"], "full")
            self.assertEqual(assessed["textCoverage"], "gapped")
            self.assertEqual(assessed["coverage"], "gapped")
            self.assertNotEqual(assessed["coverage"], "full")
            self.assertEqual(assessed["nextAction"]["action"], "done")
            self.assertFalse(assessed["successfulEmpty"])
            self.assertNotIn("text", assessed["gaps"][0])
            self.assertEqual(assessed["gaps"][0]["detected_language"], "Dutch")
            self.assertIs(assessed["gaps"][0]["alignment_executed"], False)
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.load_film_rows(film, batch.signature_for(config))
            self.assertEqual(caught.exception.reason, "coverage-incomplete")
            calls = []
            code, summary = batch.run_batch(
                config,
                execute=lambda label, stage: calls.append((label, stage)) or 1,
                send=lambda label, stage, entry: calls.append(("send", label, stage)),
            )
            self.assertEqual(calls, [("F002", "prepare")])
            self.assertEqual(code, 2)
            self.assertEqual(summary["stopped"], "F002")
            self.assertEqual(summary["films"][0]["coverage"], "gapped")
            self.assertNotIn("Ik ah", json.dumps(summary))
            limited = []
            code, summary = batch.run_batch(
                config, only_labels=["F001"],
                execute=lambda label, stage: limited.append((label, stage)) or 1,
                send=lambda label, stage, entry: limited.append(("send", label)),
            )
            self.assertEqual(limited, [])
            self.assertEqual(code, 2)
            self.assertNotIn("stopped", summary)
            self.assertEqual(summary["films"][1]["coverage"], "pending")
            with self.assertRaises(batch.BatchBlocked) as missing:
                batch.run_batch(config, only_labels=["F999"], execute=lambda *args: None, send=lambda *args: None)
            self.assertEqual(missing.exception.reason, "config")
            finish_embeddings(output / "films" / "F002")
            calls.clear()
            code, summary = batch.run_batch(
                config,
                execute=lambda label, stage: calls.append((label, stage)) or 1,
                send=lambda label, stage, entry: calls.append(("send", label, stage)),
            )
            self.assertEqual(calls, [])
            self.assertEqual(code, 2)
            self.assertNotIn("stopped", summary)
            self.assertEqual(summary["coverage"], "gapped")
            self.assertEqual(summary["textGapped"], 1)
            self.assertEqual(summary["full"], 1)

    def test_other_failures_still_stop_when_the_skip_policy_is_on(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output, 2)
            config["unsupportedAlignLanguagePolicy"] = batch.UNSUPPORTED_ALIGN_SKIP_POLICY
            batch.preflight(config)
            asr = output / "films" / "F001" / "asr"
            batch.write_json(asr / "prepare.json", {"status": "succeeded"})
            batch.write_json(asr / "chunks.json", [{"chunk_id": "c0000"}])
            batch.write_json(asr / "asr-tasks.json", [{"chunk_id": "c0000", "status": "succeeded"}])
            batch.write_json(asr / "align-tasks.json", [{
                "chunk_id": "c0000", "status": "failed", "reason": "process",
            }])
            calls = []
            code, summary = batch.run_batch(
                config, execute=lambda label, stage: calls.append((label, stage)) or 0,
                send=lambda label, stage, entry: calls.append(("send", label)))
            self.assertEqual(calls, [])
            self.assertEqual(code, 2)
            self.assertEqual(summary["stopped"], "F001")
            self.assertNotEqual(summary["coverage"], "full")
            self.assertNotEqual(summary["films"][0]["textCoverage"], "full")

    def test_explicit_tail_policy_is_stored_for_a_new_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            config["alignmentTimePolicy"] = batch.ALIGNMENT_TIME_POLICY
            summary = batch.preflight(config)
            self.assertEqual(summary["alignmentTimePolicy"], batch.ALIGNMENT_TIME_POLICY)
            self.assertEqual(batch.ALIGNMENT_TIME_POLICY, "last-unit-tail-80ms-v1")
            manifest = batch.read_json(output / "manifest.json")
            self.assertEqual(manifest["alignmentTimePolicy"], batch.ALIGNMENT_TIME_POLICY)
            protocol = batch.read_json(output / "films" / "F001" / "asr" / "protocol.json")
            self.assertEqual(protocol["alignment_time_policy"], batch.ALIGNMENT_TIME_POLICY)
            self.assertEqual(protocol["transcription_language_policy"], "auto-v1")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            config["alignmentTimePolicy"] = "widen-1ms"
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(config)
            self.assertEqual(caught.exception.reason, "language-policy")
            self.assertFalse(output.exists())

    def test_unknown_failure_still_stops_before_the_next_film_when_tail_policy_is_on(self):
        cases = (
            ("unknown", "timeout", 3),
            ("failed", "process", 2),
        )
        for status, reason, expected in cases:
            with self.subTest(status=status, reason=reason):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    output = root / "output"
                    config = self.film_config(root, output, 2)
                    config["unsupportedAlignLanguagePolicy"] = batch.UNSUPPORTED_ALIGN_SKIP_POLICY
                    config["alignmentTimePolicy"] = batch.ALIGNMENT_TIME_POLICY
                    batch.preflight(config)
                    asr = output / "films" / "F001" / "asr"
                    protocol = batch.read_json(asr / "protocol.json")
                    self.assertEqual(protocol["alignment_time_policy"], batch.ALIGNMENT_TIME_POLICY)
                    batch.write_json(asr / "prepare.json", {"status": "succeeded"})
                    batch.write_json(asr / "chunks.json", [{"chunk_id": "c0000"}])
                    batch.write_json(asr / "asr-tasks.json", [{"chunk_id": "c0000", "status": "succeeded"}])
                    batch.write_json(asr / "align-tasks.json", [{
                        "chunk_id": "c0000", "status": status, "reason": reason,
                    }])
                    calls = []
                    code, summary = batch.run_batch(
                        config,
                        execute=lambda label, stage: calls.append((label, stage)) or 0,
                        send=lambda label, stage, entry: calls.append(("send", label)),
                    )
                    self.assertEqual(calls, [])
                    self.assertEqual(code, expected)
                    self.assertEqual(summary["stopped"], "F001")
                    self.assertNotEqual(summary["films"][1]["coverage"], "full")

    def test_storage_interruption_authorization_does_not_resend_inflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            config = self.film_config(root, output)
            config["unsupportedAlignLanguagePolicy"] = batch.UNSUPPORTED_ALIGN_SKIP_POLICY
            config["alignmentTimePolicy"] = batch.ALIGNMENT_TIME_POLICY
            config["storageInterruptionRecovery"] = {
                "version": "storage-interruption-v1",
                "film": "F011",
                "candidate": "joint-named-once",
                "inputHash": "ab" * 32,
                "stateSha256": "cd" * 32,
                "snapshotSha256": "ef" * 32,
            }
            batch.preflight(config)
            film_dir = output / "films" / "F001"
            succeed_transcription(film_dir, [])
            batch.write_json(film_dir / "asr" / "align-language-gaps.json", [])
            batch.write_json(film_dir / "asr" / "documents-state.json", {
                "status": "succeeded", "text_coverage": "full", "gap_count": 0,
            })
            batch.publish_transcription(film_dir / "asr", film_dir / "embed")
            protocol = batch.read_json(film_dir / "embed" / "protocol.json")
            entry = protocol["joint_windows"][0]
            batch.write_json(film_dir / "embed" / "joint" / (entry["candidate_id"] + ".state.json"), {
                "candidate_id": entry["candidate_id"], "status": "inflight", "input_hash": "ab" * 32,
            })
            sent = []
            code, summary = batch.run_batch(
                config,
                execute=lambda label, stage: sent.append((label, stage)) or 0,
                send=lambda label, stage, item: sent.append(("send", label, item["candidate_id"])),
            )
            self.assertEqual(sent, [])
            self.assertEqual(code, 3)
            self.assertEqual(summary["stopped"], "F001")
            self.assertEqual(summary["films"][0]["status"], "inflight")


def duration_probe(audio_seconds, video_seconds, *, video_first=True):
    video = {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0.000000", "duration": video_seconds}
    audio = {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": audio_seconds}
    if not video_first:
        video["index"] = 1
        audio["index"] = 0
    return {"streams": [video, audio]}


class VideoTailBatch(unittest.TestCase):
    def test_over_bound_probe_refuses_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            row["probe"] = duration_probe("12.000000", "11.500000")
            config = config_for(root, [row], output)
            config["videoTailPolicy"] = contract.VIDEO_TAIL_POLICY
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(config)
            self.assertEqual(caught.exception.reason, "video-tail")
            self.assertFalse(output.exists())

    def test_within_bound_and_unknown_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            within = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            within["probe"] = duration_probe("12.000000", "11.850113")
            unknown = source_file(root / "media", "film-F002.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F002")
            unknown["probe"] = {"streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0.000000"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": "12.000000"},
            ]}
            longer = source_file(root / "media", "film-F003.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F003")
            longer["probe"] = duration_probe("11.850000", "12.000000")
            swapped = source_file(root / "media", "film-F004.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F004")
            swapped["source"]["audioStreamIndex"] = 0
            swapped["probe"] = duration_probe("12.000000", "11.900000", video_first=False)
            config = config_for(root, [within, unknown, longer, swapped], output)
            config["videoTailPolicy"] = contract.VIDEO_TAIL_POLICY
            summary = batch.preflight(config)
            self.assertEqual(summary["videoTailPolicy"], contract.VIDEO_TAIL_POLICY)
            self.assertEqual(summary["videoTailRisk"]["withinBound"], ["F001", "F004"])
            self.assertEqual(summary["videoTailRisk"]["unknown"], ["F002"])
            self.assertEqual(summary["videoTailRisk"]["overBound"], [])
            self.assertNotIn("F003", summary["videoTailRisk"]["withinBound"])
            self.assertNotIn("F003", summary["videoTailRisk"]["unknown"])
            maps = {item["label"]: item for item in summary["pictureMap"]}
            self.assertEqual(maps["F001"]["videoMap"], "0:v:0")
            self.assertEqual(maps["F004"]["videoMap"], "0:1")
            self.assertEqual(maps["F004"]["audioStream"], 0)
            self.assertEqual(maps["F004"]["videoStream"], 1)
            protocol = batch.read_json(output / "films" / "F001" / "embed" / "protocol.json")
            self.assertEqual(protocol["video_tail_policy"], contract.VIDEO_TAIL_POLICY)
            asr = batch.read_json(output / "films" / "F001" / "asr" / "protocol.json")
            self.assertNotIn("video_tail_policy", asr)

    def test_picture_that_reaches_runtime_is_not_an_unknown_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            runtime = 4_253_432_220
            row = source_file(root / "media", "film-F024.mp4", str(uuid.uuid4()), runtime, label="F024")
            row["probe"] = {"streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0.023222", "duration": "425.320000"},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": "425.296689"},
            ]}
            config = config_for(root, [row], output)
            config["videoTailPolicy"] = contract.VIDEO_TAIL_POLICY
            summary = batch.preflight(config)
            self.assertEqual(summary["videoTailRisk"]["unknown"], [])
            self.assertEqual(summary["videoTailRisk"]["withinBound"], [])
            self.assertEqual(summary["videoTailRisk"]["overBound"], [])
            self.assertTrue(output.is_dir())

    def test_known_bad_picture_map_refuses_before_output(self):
        cases = [
            {"streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "duration": "12.000000"},
                {"index": 1, "codec_type": "video", "codec_name": "hevc", "duration": "12.000000"},
                {"index": 2, "codec_type": "audio", "codec_name": "aac", "duration": "12.000000"},
            ], "audio": 2},
            {"streams": [
                {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "duration": "12.000000"},
            ], "audio": 1},
            {"streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "duration": "12.000000"},
            ], "audio": 1},
        ]
        for case in cases:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                output = root / "output"
                row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
                row["source"]["audioStreamIndex"] = case["audio"]
                row["probe"] = {"streams": case["streams"]}
                config = config_for(root, [row], output)
                config["videoTailPolicy"] = contract.VIDEO_TAIL_POLICY
                with self.assertRaises(batch.BatchBlocked) as caught:
                    batch.preflight(config)
                self.assertEqual(caught.exception.reason, "probe-map")
                self.assertFalse(output.exists())

    def test_clip_rejection_records_the_stage_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            row["probe"] = duration_probe("12.000000", "11.850113")
            config = config_for(root, [row], output)
            config["videoTailPolicy"] = contract.VIDEO_TAIL_POLICY
            batch.preflight(config)
            film = output / "films" / "F001"
            succeed_transcription(film, [])
            batch.publish_transcription(film / "asr", film / "embed")

            def send(_label, _stage, _entry):
                raise contract.ContractError("clipped container is outside 12 seconds plus or minus 0.15")

            code, summary = batch.run_batch(config, execute=lambda *_args: 0, send=send)
            self.assertEqual(code, 2)
            self.assertEqual(summary["stopped"], "F001")
            self.assertEqual(summary["stage"], "joint")
            self.assertEqual(summary["reason"], "clip-rejected")
            self.assertEqual(batch.read_json(output / "progress.json")["reason"], "clip-rejected")

    def test_policy_does_not_resend_an_inflight_joint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            row["probe"] = duration_probe("12.000000", "11.850113")
            config = config_for(root, [row], output)
            config["videoTailPolicy"] = contract.VIDEO_TAIL_POLICY
            batch.preflight(config)
            film = output / "films" / "F001"
            succeed_transcription(film, [])
            batch.publish_transcription(film / "asr", film / "embed")
            protocol = batch.read_json(film / "embed" / "protocol.json")
            entry = protocol["joint_windows"][0]
            batch.write_json(film / "embed" / "joint" / (entry["candidate_id"] + ".state.json"), {
                "candidate_id": entry["candidate_id"], "status": "inflight", "input_hash": "ab" * 32,
            })
            sent = []
            code, summary = batch.run_batch(
                config,
                execute=lambda label, stage: sent.append((label, stage)) or 0,
                send=lambda label, stage, item: sent.append(("send", label, item["candidate_id"])),
            )
            self.assertEqual(sent, [])
            self.assertEqual(code, 3)
            self.assertEqual(summary["stopped"], "F001")
            self.assertEqual(summary["films"][0]["status"], "inflight")


class AudioGapBatch(unittest.TestCase):
    def test_unknown_policy_refuses_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            config = config_for(root, [row], output)
            config["audioGapPolicy"] = "fill-everything"
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(config)
            self.assertEqual(caught.exception.reason, "config")
            self.assertFalse(output.exists())

    def test_policy_is_stamped_without_scanning_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            row["probe"] = duration_probe("12.000000", "12.000000")
            config = config_for(root, [row], output)
            config["audioGapPolicy"] = contract.AUDIO_GAP_POLICY
            config["videoTailPolicy"] = contract.VIDEO_TAIL_POLICY
            summary = batch.preflight(config)
            self.assertEqual(summary["audioGapPolicy"], contract.AUDIO_GAP_POLICY)
            protocol = batch.read_json(output / "films" / "F001" / "embed" / "protocol.json")
            self.assertEqual(protocol["audio_gap_policy"], contract.AUDIO_GAP_POLICY)
            self.assertEqual(protocol["video_tail_policy"], contract.VIDEO_TAIL_POLICY)
            asr = batch.read_json(output / "films" / "F001" / "asr" / "protocol.json")
            self.assertNotIn("audio_gap_policy", asr)

    def test_whitelist_does_not_resend_a_terminal_invalid_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            row["probe"] = duration_probe("12.000000", "12.000000")
            config = config_for(root, [row], output)
            config["audioGapPolicy"] = contract.AUDIO_GAP_POLICY
            config["audioGapRecovery"] = {
                "version": "audio-gap-recovery-v1",
                "film": "F041",
                "candidate": "joint-named-once",
                "inputHash": "ab" * 32,
                "stateSha256": "cd" * 32,
                "responseSha256": "ef" * 32,
                "clipSha256": "12" * 32,
                "clipMetaSha256": "34" * 32,
                "snapshotSha256": "56" * 32,
                "errorCode": "invalid_media",
            }
            batch.preflight(config)
            film = output / "films" / "F001"
            succeed_transcription(film, [])
            batch.publish_transcription(film / "asr", film / "embed")
            protocol = batch.read_json(film / "embed" / "protocol.json")
            entry = protocol["joint_windows"][0]
            batch.write_json(film / "embed" / "joint" / (entry["candidate_id"] + ".state.json"), {
                "candidate_id": entry["candidate_id"], "status": "terminal-invalid",
                "http_status": 422, "input_hash": "ab" * 32,
            })
            sent = []
            code, summary = batch.run_batch(
                config,
                execute=lambda label, stage: sent.append((label, stage)) or 0,
                send=lambda label, stage, item: sent.append(("send", label, item["candidate_id"])),
            )
            self.assertEqual(sent, [])
            self.assertEqual(code, 3)
            self.assertEqual(summary["reason"], "not-succeeded")
            self.assertEqual(summary["films"][0]["status"], "terminal-invalid")

    def test_gap_record_changes_the_candidate_hash(self):
        protocol = {"signature": {"id": "model"}, "window": contract.window_template()}
        entry = {"candidate_id": "joint-1", "start_ticks": 0, "end_ticks": contract.LENGTH_TICKS}
        gap = {
            "audio_gap_policy": contract.AUDIO_GAP_POLICY,
            "audio_gap_total_seconds": "0.080000",
            "audio_gap_count": 1,
            "audio_gaps": [{"start_seconds": "0.256000", "end_seconds": "0.336000", "seconds": "0.080000"}],
        }
        old = batch.candidate_hash(protocol, "joint", entry, "abc")
        filled = batch.candidate_hash(protocol, "joint", entry, "abc", gap)
        self.assertNotEqual(old, filled)
        reused = batch.dispatch_entries(
            [entry], lambda _entry: {"status": "succeeded", "input_hash": old}, [],
            protocol, "joint", lambda _entry: "abc", lambda _entry: None,
        )
        self.assertEqual(reused["reused"], ["joint-1"])
        self.assertEqual(reused["sent"], [])
        with self.assertRaises(batch.BatchBlocked) as caught:
            batch.dispatch_entries(
                [entry], lambda _entry: {"status": "succeeded", "input_hash": old}, [],
                protocol, "joint", lambda _entry: "abc", lambda _entry: gap,
            )
        self.assertEqual(caught.exception.reason, "cache-hash-mismatch")
        matched = batch.dispatch_entries(
            [entry], lambda _entry: {"status": "succeeded", "input_hash": filled}, [],
            protocol, "joint", lambda _entry: "abc", lambda _entry: gap,
        )
        self.assertEqual(matched["sent"], [])
        self.assertEqual(matched["reused"], ["joint-1"])


class FrozenSourceSelection(unittest.TestCase):
    def test_explicit_policy_ignores_one_extra_and_keeps_frozen_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            first = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            second = source_file(
                root / "media", "film-F002.mp4", str(uuid.uuid4()), 200_000_000, label="F002",
            )
            (root / "media" / "notes.txt").write_text("skip", encoding="utf-8")
            extra = root / "media" / "extra.mp4"
            extra.write_bytes(b"not-on-the-frozen-list")
            config = config_for(root, [first, second], output)
            config["expectedFilmCount"] = 2
            frozen_windows = [
                len(batch.plan_film_windows(first["source"]["runtimeTicks"])),
                len(batch.plan_film_windows(second["source"]["runtimeTicks"])),
            ]

            strict = batch.preflight(config)
            self.assertFalse(strict["ready"])
            self.assertEqual(strict["missingFiles"], 1)
            self.assertEqual(strict["ignoredExtraFiles"], 0)
            self.assertEqual(strict["films"], 2)
            self.assertEqual(strict["catalog"], [])
            self.assertNotIn("sourceSelectionPolicy", strict)
            self.assertFalse(output.exists())
            calls = []
            code, run_summary = batch.run_batch(
                config,
                execute=lambda label, stage: calls.append((label, stage)) or 0,
                send=lambda label, stage, entry: calls.append(("send", label)),
            )
            self.assertEqual(code, 1)
            self.assertFalse(run_summary["ready"])
            self.assertEqual(run_summary["missingFiles"], 1)
            self.assertEqual(calls, [])
            self.assertFalse(output.exists())
            config["sourceSelectionPolicy"] = ""
            empty = batch.preflight(config)
            self.assertFalse(empty["ready"])
            self.assertEqual(empty["missingFiles"], 1)
            self.assertEqual(empty["ignoredExtraFiles"], 0)
            self.assertFalse(output.exists())
            del config["sourceSelectionPolicy"]

            extra.unlink()
            baseline = batch.preflight(config)
            self.assertTrue(baseline["ready"])
            self.assertEqual([row["label"] for row in baseline["catalog"]], ["F001", "F002"])
            self.assertEqual([row["windows"] for row in baseline["catalog"]], frozen_windows)
            self.assertEqual(baseline["ignoredExtraFiles"], 0)
            self.assertNotIn("sourceSelectionPolicy", baseline)
            identities = {
                "manifest": output / "manifest.json",
                "f001-embed": output / "films" / "F001" / "embed" / "protocol.json",
                "f001-asr": output / "films" / "F001" / "asr" / "protocol.json",
                "f002-embed": output / "films" / "F002" / "embed" / "protocol.json",
                "f002-asr": output / "films" / "F002" / "asr" / "protocol.json",
            }
            cache = output / "films" / "F001" / "embed" / "joint" / "kept.json"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(b"succeeded-cache")
            identities["cache"] = cache
            frozen_bytes = {name: path.read_bytes() for name, path in identities.items()}
            baseline_ids = [row["itemId"] for row in baseline["catalog"]]

            extra.write_bytes(b"not-on-the-frozen-list")
            config["sourceSelectionPolicy"] = batch.SOURCE_SELECTION_POLICY
            selected = batch.preflight(config)
            self.assertTrue(selected["ready"])
            self.assertEqual(selected["sourceSelectionPolicy"], batch.SOURCE_SELECTION_POLICY)
            self.assertEqual(selected["ignoredExtraFiles"], 1)
            self.assertEqual(selected["missingFiles"], 0)
            self.assertEqual(selected["unmatchedFacts"], 0)
            self.assertEqual(selected["sourceDrift"], 0)
            self.assertEqual(selected["films"], 2)
            self.assertEqual(selected["skippedFiles"], 1)
            self.assertEqual([row["label"] for row in selected["catalog"]], ["F001", "F002"])
            self.assertEqual([row["windows"] for row in selected["catalog"]], frozen_windows)
            self.assertEqual([row["itemId"] for row in selected["catalog"]], baseline_ids)
            self.assertFalse((output / "films" / "F003").exists())
            for name, path in identities.items():
                self.assertEqual(path.read_bytes(), frozen_bytes[name], name)
            manifest = batch.read_json(output / "manifest.json")
            self.assertNotIn("sourceSelectionPolicy", manifest)
            self.assertEqual([film["label"] for film in manifest["films"]], ["F001", "F002"])
            protocol = batch.read_json(identities["f001-embed"])
            self.assertNotIn("sourceSelectionPolicy", protocol)
            self.assertNotIn("source_selection_policy", protocol)
            written = batch.read_json(output / "preflight.json")
            self.assertEqual(written["sourceSelectionPolicy"], batch.SOURCE_SELECTION_POLICY)
            self.assertEqual(written["ignoredExtraFiles"], 1)
            self.assertEqual(written["windows"], sum(frozen_windows))

            held = root / "held-film"
            Path(first["source"]["sourcePath"]).rename(held)
            hidden = batch.preflight(config)
            self.assertFalse(hidden["ready"])
            self.assertEqual(hidden["unmatchedFacts"], 1)
            self.assertEqual(hidden["sourceDrift"], 0)
            self.assertEqual(hidden["missingFiles"], 0)
            self.assertEqual(hidden["ignoredExtraFiles"], 1)
            self.assertEqual(hidden["catalog"], [])
            self.assertEqual((output / "manifest.json").read_bytes(), frozen_bytes["manifest"])
            held.rename(first["source"]["sourcePath"])

            second_path = Path(second["source"]["sourcePath"])
            original = second_path.read_bytes()
            mtime_ns = second_path.stat().st_mtime_ns
            second_path.write_bytes(original + b"x")
            drifted = batch.preflight(config)
            self.assertFalse(drifted["ready"])
            self.assertEqual(drifted["sourceDrift"], 1)
            self.assertEqual(drifted["missingFiles"], 0)
            self.assertEqual(drifted["unmatchedFacts"], 0)
            self.assertEqual(drifted["ignoredExtraFiles"], 1)
            self.assertEqual(drifted["catalog"], [])
            self.assertEqual((output / "manifest.json").read_bytes(), frozen_bytes["manifest"])
            self.assertEqual(cache.read_bytes(), frozen_bytes["cache"])
            second_path.write_bytes(original)
            os.utime(second_path, ns=(mtime_ns, mtime_ns))
            restored = batch.preflight(config)
            self.assertTrue(restored["ready"])
            self.assertEqual([row["label"] for row in restored["catalog"]], ["F001", "F002"])
            self.assertEqual([row["windows"] for row in restored["catalog"]], frozen_windows)
            for name, path in identities.items():
                self.assertEqual(path.read_bytes(), frozen_bytes[name], name)

            fresh = root / "fresh-output"
            refused = dict(config)
            refused["outputRoot"] = str(fresh)
            refused["sourceSelectionPolicy"] = "include-new-files"
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(refused)
            self.assertEqual(caught.exception.reason, "config")
            self.assertFalse(fresh.exists())
            self.assertEqual((output / "manifest.json").read_bytes(), frozen_bytes["manifest"])

    def test_policy_still_refuses_duplicate_identity_and_probe_anomalies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            clone = json.loads(json.dumps(row))
            other = str(uuid.uuid4())
            clone["label"] = "F002"
            clone["source"]["itemId"] = other
            clone["source"]["sourceItemId"] = other
            (root / "media" / "extra.mp4").write_bytes(b"extra")
            config = config_for(root, [row, clone], output)
            config["sourceSelectionPolicy"] = batch.SOURCE_SELECTION_POLICY
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(config)
            self.assertEqual(caught.exception.reason, "source-mismatch")
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            row["source"]["sourceItemId"] = str(uuid.uuid4())
            config = config_for(root, [row], output)
            config["sourceSelectionPolicy"] = batch.SOURCE_SELECTION_POLICY
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(config)
            self.assertEqual(caught.exception.reason, "source-mismatch")
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            short = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), 1_000_000, label="F001")
            (root / "media" / "extra.mp4").write_bytes(b"extra")
            config = config_for(root, [short], output)
            config["sourceSelectionPolicy"] = batch.SOURCE_SELECTION_POLICY
            config["expectedFilmCount"] = 1
            summary = batch.preflight(config)
            self.assertFalse(summary["ready"])
            self.assertEqual(summary["ineligible"], 1)
            self.assertEqual(summary["ignoredExtraFiles"], 1)
            self.assertEqual(summary["missingFiles"], 0)
            self.assertEqual(summary["catalog"], [])
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            row = source_file(root / "media", "film-F001.mp4", str(uuid.uuid4()), contract.LENGTH_TICKS, label="F001")
            row["probe"] = duration_probe("12.000000", "11.500000")
            (root / "media" / "extra.mp4").write_bytes(b"extra")
            config = config_for(root, [row], output)
            config["sourceSelectionPolicy"] = batch.SOURCE_SELECTION_POLICY
            config["videoTailPolicy"] = contract.VIDEO_TAIL_POLICY
            with self.assertRaises(batch.BatchBlocked) as caught:
                batch.preflight(config)
            self.assertEqual(caught.exception.reason, "video-tail")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
