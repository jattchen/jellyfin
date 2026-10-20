#!/usr/bin/env python3
"""Synthetic recovery prepare. No real batch directory and no hard links."""
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = load("yuanying_recovery_test", "yuanying-recovery.py")
batch = recovery.batch
transcribe = recovery.transcribe


def write_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    return write_bytes(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def asr_body(text, language):
    return {
        "raw_result": {"text": text, "truncated": False, "finish_reason": "eos", "segments": None},
        "language_forced": None,
        "language_detected": language,
        "transcription_language_policy": "auto-v1",
        "language_granularity": "chunk",
        "context": "",
        "max_new_tokens": 1024,
        "dtype": "bfloat16",
        "alignment_executed": False,
        "mlx_peak_allocated_bytes": 42,
    }


def source_facts(path):
    return {
        "itemId": "11111111-1111-4111-8111-111111111111",
        "mediaSourceId": "mediasource",
        "libraryId": "22222222-2222-4222-8222-222222222222",
        "contentVersion": "content-fixed",
        "runtimeTicks": 120_000_000,
        "container": "mp4",
        "audioStreamIndex": 1,
        "sourceItemId": "11111111-1111-4111-8111-111111111111",
        "sourcePath": str(path),
        "sizeBytes": 4,
        "modifiedTicks": 1,
    }


def snapshot_for(source):
    files = {}
    total = 0
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        data = path.read_bytes()
        files[str(path.relative_to(source))] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        total += len(data)
    return {
        "createdAt": "2026-10-20T00:00:00+00:00",
        "fileCount": len(files),
        "totalBytes": total,
        "root": str(source.resolve()),
        "files": files,
    }


class RecoveryPrepare(unittest.TestCase):
    def config(self, root, source, output, snapshot_path):
        media = root / "media"
        media.mkdir(parents=True, exist_ok=True)
        (media / "film.mp4").write_bytes(b"film")
        worker = root / "worker.py"
        worker.write_text("raise SystemExit(0)\n", encoding="utf-8")
        (root / "facts.json").write_text("[]\n", encoding="utf-8")
        return {
            "sourceDirectory": str(media),
            "sourceFacts": str(root / "facts.json"),
            "outputRoot": str(output),
            "workerPython": str(worker),
            "asrModel": str(root / "asr-model"),
            "alignerModel": str(root / "align-model"),
            "embeddingBaseUrl": batch.contract.ISOLATED_BASE,
            "processingId": batch.EXPECTED_PROCESSING_ID,
            "maxInflightEmbeddings": 1,
            "transcriptionLanguagePolicy": "auto-v1",
            "unsupportedAlignLanguagePolicy": recovery.POLICY,
            "recoverySource": str(source),
            "recoverySnapshot": str(snapshot_path),
        }

    def build(self, root, *, identity_language=None, bad_hash=False, orphan=False, unknown=False, second_failure=False):
        source = root / "source"
        media = root / "media.mp4"
        write_bytes(media, b"film")
        protocol = {
            "version": "issue3-yuanying-asr-v1",
            "fullFilm": True,
            "transcription_language_policy": "auto-v1",
            "worker_python": "/worker/python",
            "asr_model_path": "/models/asr",
            "aligner_model_path": "/models/align",
            "source": source_facts(media),
        }
        chunk = {
            "chunk_id": "c0000",
            "wav_sha256": "ab" * 32,
            "start_sample": 0,
            "end_sample": 16000,
            "start_ticks": 0,
            "end_ticks": 10_000_000,
        }
        record = asr_body("Ik ah", "Dutch")
        asr_sha = write_json(source / "films" / "F006" / "asr" / "asr" / "c0000.json", record)
        identity = transcribe.align_identity(protocol, chunk, asr_sha, record)
        if identity_language is not None:
            identity = dict(identity)
            identity["language"] = identity_language
        write_json(source / "films" / "F006" / "asr" / "protocol.json", protocol)
        write_json(source / "films" / "F006" / "asr" / "prepare.json", {"status": "succeeded", "sample_count": 16000})
        write_json(source / "films" / "F006" / "asr" / "chunks.json", [chunk])
        write_json(source / "films" / "F006" / "asr" / "asr-tasks.json", [{
            "chunk_id": "c0000", "status": "succeeded", "output_sha256": asr_sha,
        }])
        failure = {
            "chunk_id": "c0000",
            "status": "unknown" if unknown else "failed",
            "reason": "timeout" if unknown else "language-unsupported",
            "detected_language": "Dutch",
            "identity": identity,
        }
        write_json(source / "films" / "F006" / "asr" / "align-tasks.json", [failure])
        if orphan:
            write_bytes(source / "films" / "F006" / "asr" / "align" / "c0000.json", b"orphan")
        self.write_embed(source, "F006", "/media/f006.mp4")
        self.write_settled(source, protocol)
        self.write_pending(source)
        if second_failure:
            self.write_other_failure(source)
        write_json(source / "manifest.json", {"films": [
            {"label": "F001", "itemId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            {"label": "F006", "itemId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
            {"label": "F007", "itemId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
        ]})
        write_bytes(source / "preflight.json", b"{\"ready\":false}\n")
        write_bytes(source / "run-identity.json", b"{\"run\":1}\n")
        write_bytes(source / "shared-document-cache" / "kept.json", b"cached-document")
        if bad_hash:
            tasks = json.loads((source / "films" / "F001" / "asr" / "asr-tasks.json").read_text(encoding="utf-8"))
            tasks[0]["output_sha256"] = "0" * 64
            write_json(source / "films" / "F001" / "asr" / "asr-tasks.json", tasks)
        snap = snapshot_for(source)
        snap_path = root / "snapshot.json"
        write_json(snap_path, snap)
        return source, snap_path

    def write_embed(self, source, label, media_path):
        write_json(source / "films" / label / "embed" / "protocol.json", {
            "asr_run": str((source / "films" / label / "asr").resolve()),
            "shared_document_cache": {"directory": str((source / "shared-document-cache").resolve())},
            "worker_python": "/worker/python",
            "source": {"sourcePath": media_path},
            "joint_windows": [{"candidate_id": "w0", "source_path": media_path}],
        })

    def write_settled(self, source, protocol):
        settled = dict(protocol)
        settled["label"] = "F001"
        raw = write_json(source / "films" / "F001" / "asr" / "protocol.json", settled)
        self.settled_protocol_sha = raw
        output_sha = write_bytes(source / "films" / "F001" / "asr" / "asr" / "c0000.json", b"{\"text\":\"kept\"}\n")
        align_sha = write_bytes(source / "films" / "F001" / "asr" / "align" / "c0000.json", b"{\"aligned\":true}\n")
        write_json(source / "films" / "F001" / "asr" / "prepare.json", {"status": "succeeded"})
        write_json(source / "films" / "F001" / "asr" / "chunks.json", [{"chunk_id": "c0000"}])
        write_json(source / "films" / "F001" / "asr" / "asr-tasks.json", [{
            "chunk_id": "c0000", "status": "succeeded", "output_sha256": output_sha,
        }])
        write_json(source / "films" / "F001" / "asr" / "align-tasks.json", [{
            "chunk_id": "c0000", "status": "succeeded", "output_sha256": align_sha,
        }])
        write_json(source / "films" / "F001" / "asr" / "documents-state.json", {"status": "succeeded"})
        write_json(source / "films" / "F001" / "asr" / "candidates.json", [])
        write_bytes(source / "films" / "F001" / "sample-reuse-audit.json", b"{\"reused\":0}\n")
        self.write_embed(source, "F001", "/media/f001.mp4")

    def write_pending(self, source):
        write_json(source / "films" / "F007" / "asr" / "protocol.json", {
            "transcription_language_policy": "auto-v1",
            "worker_python": "/worker/python",
            "source": {"sourcePath": "/media/f007.mp4"},
        })
        self.write_embed(source, "F007", "/media/f007.mp4")

    def write_other_failure(self, source):
        write_json(source / "films" / "F008" / "asr" / "protocol.json", {"transcription_language_policy": "auto-v1"})
        write_json(source / "films" / "F008" / "asr" / "asr-tasks.json", [{
            "chunk_id": "c0000", "status": "failed", "reason": "process",
        }])
        self.write_embed(source, "F008", "/media/f008.mp4")

    def test_prepare_copies_bytes_and_derives_only_the_language_gap(self):
        self.assertNotIn("os.link", Path(recovery.__file__).read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snap_path = self.build(root)
            output = root / "output"
            config = self.config(root, source, output, snap_path)
            before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            nlink = os.lstat(source / "shared-document-cache" / "kept.json").st_nlink
            receipt = recovery.prepare(config)
            self.assertEqual(os.lstat(source / "shared-document-cache" / "kept.json").st_nlink, nlink)
            self.assertEqual(nlink, 1)
            after = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            copied = output / "shared-document-cache" / "kept.json"
            original = source / "shared-document-cache" / "kept.json"
            self.assertEqual(copied.read_bytes(), original.read_bytes())
            self.assertNotEqual(os.lstat(copied).st_ino, os.lstat(original).st_ino)
            self.assertFalse((output / "preflight.json").exists())
            self.assertFalse((output / "run-identity.json").exists())
            self.assertEqual(
                (output / "films" / "F001" / "asr" / "protocol.json").read_bytes(),
                (source / "films" / "F001" / "asr" / "protocol.json").read_bytes(),
            )
            self.assertNotIn("unsupported_align_language_policy", (output / "films" / "F001" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            failed = json.loads((source / "films" / "F006" / "asr" / "align-tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(failed[0]["status"], "failed")
            derived = json.loads((output / "films" / "F006" / "asr" / "align-tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(derived[0]["status"], "skipped")
            self.assertEqual(derived[0]["reason"], "language-unsupported")
            self.assertIs(derived[0]["alignment_executed"], False)
            record = json.loads((output / "films" / "F006" / "asr" / "align" / "c0000.json").read_text(encoding="utf-8"))
            self.assertEqual(record["input_text"], "Ik ah")
            self.assertEqual(record["detected_language"], "Dutch")
            self.assertNotEqual(record["language"], "Chinese")
            self.assertEqual(record["units"], [])
            self.assertIs(record["alignment_executed"], False)
            f006 = json.loads((output / "films" / "F006" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(f006["unsupported_align_language_policy"], recovery.POLICY)
            self.assertEqual(f006["worker_python"], "/worker/python")
            self.assertEqual(f006["source"]["sourcePath"], str(root / "media.mp4"))
            pending = json.loads((output / "films" / "F007" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(pending["unsupported_align_language_policy"], recovery.POLICY)
            embed = json.loads((output / "films" / "F006" / "embed" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(embed["asr_run"], str((output / "films" / "F006" / "asr").resolve()))
            self.assertEqual(embed["shared_document_cache"]["directory"], str((output / "shared-document-cache").resolve()))
            self.assertEqual(embed["source"]["sourcePath"], "/media/f006.mp4")
            self.assertEqual(embed["joint_windows"][0]["source_path"], "/media/f006.mp4")
            self.assertEqual(embed["worker_python"], "/worker/python")
            action = batch.next_transcription_action(
                batch.load_checkpoints(output / "films" / "F006" / "asr"), f006)
            self.assertEqual(action, {"action": "run", "stage": "documents"})
            self.assertEqual(receipt["hardlink"], False)
            self.assertEqual(receipt["derivedSkips"][0]["chunk_id"], "c0000")
            self.assertNotIn("text", receipt["derivedSkips"][0])
            receipt_bytes = (output / "recovery-receipt.json").read_bytes()
            again = recovery.prepare(config)
            self.assertEqual((output / "recovery-receipt.json").read_bytes(), receipt_bytes)
            self.assertEqual(again["derivedSkips"], receipt["derivedSkips"])
            self.assertEqual(after, {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()})
            copied.write_bytes(b"changed")
            with self.assertRaises(batch.BatchBlocked) as caught:
                recovery.prepare(config)
            self.assertEqual(caught.exception.reason, "output-exists")
            self.assertEqual((source / "films" / "F006" / "asr" / "align-tasks.json").read_bytes(), before["films/F006/asr/align-tasks.json"])

    def test_hash_identity_orphan_and_other_failures_do_not_create_output(self):
        cases = (
            ({"bad_hash": True}, "source-mismatch"),
            ({"identity_language": "Chinese"}, "source-mismatch"),
            ({"orphan": True}, "source-mismatch"),
            ({"unknown": True}, "transcription-failed"),
            ({"second_failure": True}, "transcription-failed"),
        )
        for flags, reason in cases:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, snap_path = self.build(root, **flags)
                output = root / "output"
                with self.assertRaises(batch.BatchBlocked) as caught:
                    recovery.prepare(self.config(root, source, output, snap_path))
                self.assertEqual(caught.exception.reason, reason)
                self.assertFalse(output.exists())
                self.assertEqual(
                    json.loads((source / "films" / "F006" / "asr" / "align-tasks.json").read_text(encoding="utf-8"))[0]["status"],
                    "unknown" if flags.get("unknown") else "failed",
                )

    def test_snapshot_count_mismatch_refuses_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snap_path = self.build(root)
            body = json.loads(snap_path.read_text(encoding="utf-8"))
            body["fileCount"] = 1
            write_json(snap_path, body)
            output = root / "output"
            with self.assertRaises(batch.BatchBlocked) as caught:
                recovery.prepare(self.config(root, source, output, snap_path))
            self.assertEqual(caught.exception.reason, "source-mismatch")
            self.assertFalse(output.exists())


def write_silence(path, count):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(transcribe.SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * count)


def pcm_chunk(chunk_id, start, count, stored_path, digest):
    end = start + count
    return {
        "chunk_id": chunk_id,
        "wav_path": stored_path,
        "start_sample": start,
        "end_sample": end,
        "start_ticks": start * transcribe.TICKS_PER_SAMPLE,
        "end_ticks": end * transcribe.TICKS_PER_SAMPLE,
        "offset_seconds": start / transcribe.SAMPLE_RATE,
        "duration_seconds": count / transcribe.SAMPLE_RATE,
        "wav_sha256": digest,
    }


class TailRecovery(unittest.TestCase):
    def config(self, root, source, output, snapshot_path, *, tail=True):
        media = root / "library"
        media.mkdir(parents=True, exist_ok=True)
        (media / "film.mp4").write_bytes(b"film")
        worker = root / "worker.py"
        worker.write_text("raise SystemExit(0)\n", encoding="utf-8")
        (root / "facts.json").write_text("[]\n", encoding="utf-8")
        body = {
            "sourceDirectory": str(media),
            "sourceFacts": str(root / "facts.json"),
            "outputRoot": str(output),
            "workerPython": str(worker),
            "asrModel": str(root / "asr-model"),
            "alignerModel": str(root / "align-model"),
            "embeddingBaseUrl": batch.contract.ISOLATED_BASE,
            "processingId": batch.EXPECTED_PROCESSING_ID,
            "maxInflightEmbeddings": 1,
            "transcriptionLanguagePolicy": "auto-v1",
            "unsupportedAlignLanguagePolicy": recovery.POLICY,
            "recoverySource": str(source),
            "recoverySnapshot": str(snapshot_path),
        }
        if tail:
            body["alignmentTimePolicy"] = batch.ALIGNMENT_TIME_POLICY
        return body

    def build(self, root, *, last_start=25.52, last_end=28.96, first_end=1.0,
              bad_wav_hash=False, missing_wav=False, second_tail=False):
        source = root / "source"
        media = root / "media-f011.mp4"
        write_bytes(media, b"film")
        aligner = "/models/" + transcribe.ALIGNER_REVISION
        tail_count = 462896
        rest_count = 16000
        wav_dir = source / "films" / "F011" / "asr" / "audio" / "chunks"
        first_wav = wav_dir / "c0000.wav"
        second_wav = wav_dir / "c0001.wav"
        if missing_wav:
            first_digest = "cd" * 32
        else:
            write_silence(first_wav, tail_count)
            first_digest = hashlib.sha256(first_wav.read_bytes()).hexdigest()
        write_silence(second_wav, rest_count)
        second_digest = hashlib.sha256(second_wav.read_bytes()).hexdigest()
        first = pcm_chunk("c0000", 0, tail_count, str(first_wav.resolve()) if first_wav.exists() else "/old-v2/c0000.wav", first_digest)
        second = pcm_chunk("c0001", tail_count, rest_count, "/old-v2/films/F011/asr/audio/chunks/c0001.wav", second_digest)
        protocol = {
            "version": "issue3-yuanying-asr-v1",
            "fullFilm": True,
            "transcription_language_policy": "auto-v1",
            "unsupported_align_language_policy": recovery.POLICY,
            "worker_python": "/worker/python",
            "asr_model_path": "/models/asr",
            "aligner_model_path": aligner,
            "source": source_facts(media),
        }
        asr_record = asr_body("甲乙", "Chinese")
        asr_sha = write_json(source / "films" / "F011" / "asr" / "asr" / "c0000.json", asr_record)
        write_json(source / "films" / "F011" / "asr" / "asr" / "c0001.json", asr_body("丙", "Chinese"))
        second_asr_sha = hashlib.sha256((source / "films" / "F011" / "asr" / "asr" / "c0001.json").read_bytes()).hexdigest()
        units = [
            {"text": "甲", "start_time": 0.1, "end_time": first_end},
            {"text": "乙", "start_time": last_start, "end_time": last_end},
        ]
        slots = []
        for unit in units:
            slots.extend([unit["start_time"] * 1000, unit["end_time"] * 1000])
        align_record = {
            "input_text": "甲乙",
            "language": "Chinese",
            "detected_language": "Chinese",
            "dtype": "bfloat16",
            "alignment_executed": True,
            "asr_inference_executed": False,
            "audio_samples": tail_count,
            "audio_rate": transcribe.SAMPLE_RATE,
            "mlx_peak_allocated_bytes": 24,
            "units": units,
            "parser_capture": {"words": ["甲", "乙"], "raw_slot_predictions_ms": slots},
            "model_path": aligner,
            "transcription_language_policy": "auto-v1",
            "language_granularity": "chunk",
        }
        align_sha = write_json(source / "films" / "F011" / "asr" / "align" / "c0000.json", align_record)
        identity = transcribe.align_identity(protocol, first, asr_sha, asr_record)
        write_json(source / "films" / "F011" / "asr" / "protocol.json", protocol)
        write_json(source / "films" / "F011" / "asr" / "prepare.json", {
            "status": "succeeded",
            "sample_count": tail_count + rest_count,
            "full_wav": "audio/full.wav",
        })
        write_json(source / "films" / "F011" / "asr" / "chunks.json", [first, second])
        write_json(source / "films" / "F011" / "asr" / "asr-tasks.json", [
            {"chunk_id": "c0000", "status": "succeeded", "output_sha256": asr_sha},
            {"chunk_id": "c0001", "status": "succeeded", "output_sha256": second_asr_sha},
        ])
        failure = {
            "chunk_id": "c0000",
            "status": "failed",
            "reason": "validation:last-unit-out-of-bounds",
            "identity": identity,
            "output_sha256": align_sha,
            "retained_text": "甲乙",
        }
        tasks = [failure]
        if second_tail:
            tasks.append({
                "chunk_id": "c0009",
                "status": "failed",
                "reason": "validation:last-unit-out-of-bounds",
                "identity": identity,
                "output_sha256": align_sha,
            })
        write_json(source / "films" / "F011" / "asr" / "align-tasks.json", tasks)
        if bad_wav_hash and first_wav.exists():
            first_wav.write_bytes(first_wav.read_bytes() + b"\x00\x00")
        write_json(source / "films" / "F011" / "embed" / "protocol.json", {
            "asr_run": str((source / "films" / "F011" / "asr").resolve()),
            "shared_document_cache": {
                "directory": str((source / "shared-document-cache").resolve()),
                "scope": "batch",
            },
            "worker_python": "/worker/python",
            "source": {"sourcePath": str(media)},
            "joint_windows": [{"candidate_id": "w0", "source_path": str(media)}],
        })
        self.write_closed_film(source)
        self.write_settled_gap(source)
        write_json(source / "films" / "F012" / "asr" / "protocol.json", {
            "transcription_language_policy": "auto-v1",
            "unsupported_align_language_policy": recovery.POLICY,
            "worker_python": "/worker/python",
            "source": {"sourcePath": "/media/f012.mp4"},
        })
        write_json(source / "films" / "F012" / "embed" / "protocol.json", {
            "asr_run": str((source / "films" / "F012" / "asr").resolve()),
            "shared_document_cache": {"directory": str((source / "shared-document-cache").resolve())},
            "worker_python": "/worker/python",
            "source": {"sourcePath": "/media/f012.mp4"},
            "joint_windows": [{"candidate_id": "w0", "source_path": "/media/f012.mp4"}],
        })
        write_json(source / "manifest.json", {"films": [
            {"label": "F001"}, {"label": "F006"}, {"label": "F011"}, {"label": "F012"},
        ]})
        write_bytes(source / "preflight.json", b"{\"ready\":false}\n")
        write_bytes(source / "progress.json", b"{\"stopped\":\"F011\"}\n")
        write_bytes(source / "recovery-receipt.json", b"{\"marker\":\"old-recovery-receipt\"}\n")
        write_bytes(source / "shared-document-cache" / "kept.json", b"cached-document")
        snap_path = root / "snapshot.json"
        write_json(snap_path, snapshot_for(source))
        return source, snap_path

    def write_closed_film(self, source):
        protocol = {
            "version": "issue3-yuanying-asr-v1",
            "transcription_language_policy": "auto-v1",
            "worker_python": "/worker/python",
            "asr_model_path": "/models/asr",
            "aligner_model_path": "/models/align",
            "source": source_facts(Path("/media/f001.mp4")),
        }
        write_json(source / "films" / "F001" / "asr" / "protocol.json", protocol)
        output_sha = write_bytes(source / "films" / "F001" / "asr" / "asr" / "c0000.json", b"{\"text\":\"kept\"}\n")
        align_sha = write_bytes(source / "films" / "F001" / "asr" / "align" / "c0000.json", b"{\"aligned\":true}\n")
        write_json(source / "films" / "F001" / "asr" / "prepare.json", {"status": "succeeded", "full_wav": "audio/full.wav"})
        write_json(source / "films" / "F001" / "asr" / "chunks.json", [{"chunk_id": "c0000"}])
        write_json(source / "films" / "F001" / "asr" / "asr-tasks.json", [{
            "chunk_id": "c0000", "status": "succeeded", "output_sha256": output_sha,
        }])
        write_json(source / "films" / "F001" / "asr" / "align-tasks.json", [{
            "chunk_id": "c0000", "status": "succeeded", "output_sha256": align_sha,
        }])
        write_json(source / "films" / "F001" / "asr" / "documents-state.json", {"status": "succeeded", "text_coverage": "full"})
        write_json(source / "films" / "F001" / "asr" / "candidates.json", [])
        write_json(source / "films" / "F001" / "embed" / "protocol.json", {
            "asr_run": str((source / "films" / "F001" / "asr").resolve()),
            "shared_document_cache": {"directory": str((source / "shared-document-cache").resolve())},
            "worker_python": "/worker/python",
            "source": {"sourcePath": "/media/f001.mp4"},
            "joint_windows": [{"candidate_id": "w0", "source_path": "/media/f001.mp4"}],
        })

    def write_settled_gap(self, source):
        protocol = {
            "transcription_language_policy": "auto-v1",
            "unsupported_align_language_policy": recovery.POLICY,
            "worker_python": "/worker/python",
            "asr_model_path": "/models/asr",
            "aligner_model_path": "/models/align",
            "source": source_facts(Path("/media/f006.mp4")),
        }
        write_json(source / "films" / "F006" / "asr" / "protocol.json", protocol)
        wav = source / "films" / "F006" / "asr" / "audio" / "chunks" / "c0273.wav"
        write_silence(wav, 16000)
        digest = hashlib.sha256(wav.read_bytes()).hexdigest()
        write_json(source / "films" / "F006" / "asr" / "chunks.json", [
            pcm_chunk("c0273", 0, 16000, "/old-v2/films/F006/asr/audio/chunks/c0273.wav", digest),
        ])
        asr_sha = write_json(source / "films" / "F006" / "asr" / "asr" / "c0273.json", asr_body("Ik ah", "Dutch"))
        align_sha = write_json(source / "films" / "F006" / "asr" / "align" / "c0273.json", {
            "input_text": "Ik ah",
            "language": "Dutch",
            "detected_language": "Dutch",
            "units": [],
            "alignment_executed": False,
            "skipped": True,
            "skip_reason": "language-unsupported",
            "skip_policy": recovery.POLICY,
        })
        write_json(source / "films" / "F006" / "asr" / "prepare.json", {"status": "succeeded"})
        write_json(source / "films" / "F006" / "asr" / "asr-tasks.json", [{
            "chunk_id": "c0273", "status": "succeeded", "output_sha256": asr_sha,
        }])
        write_json(source / "films" / "F006" / "asr" / "align-tasks.json", [{
            "chunk_id": "c0273",
            "status": "skipped",
            "reason": "language-unsupported",
            "skip_policy": recovery.POLICY,
            "alignment_executed": False,
            "detected_language": "Dutch",
            "output_sha256": align_sha,
            "derived_from": {"source_status": "failed", "source_reason": "language-unsupported"},
        }])
        write_json(source / "films" / "F006" / "asr" / "documents-state.json", {
            "status": "succeeded", "text_coverage": "gapped", "gap_count": 1,
        })
        write_json(source / "films" / "F006" / "asr" / "align-language-gaps.json", [{
            "chunk_id": "c0273",
            "text": "Ik ah",
            "reason": "language-unsupported",
            "detected_language": "Dutch",
            "skip_policy": recovery.POLICY,
            "alignment_executed": False,
        }])
        write_json(source / "films" / "F006" / "asr" / "candidates.json", [])
        write_json(source / "films" / "F006" / "embed" / "protocol.json", {
            "asr_run": str((source / "films" / "F006" / "asr").resolve()),
            "shared_document_cache": {"directory": str((source / "shared-document-cache").resolve())},
            "worker_python": "/worker/python",
            "source": {"sourcePath": "/media/f006.mp4"},
            "joint_windows": [{"candidate_id": "w0", "source_path": "/media/f006.mp4"}],
        })

    def test_prepare_revalidates_one_tail_and_keeps_settled_films(self):
        self.assertEqual(batch.ALIGNMENT_TIME_POLICY, transcribe.TAIL_TIME_POLICY)
        source_text = Path(recovery.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.link", source_text)
        self.assertNotIn("clonefile", source_text)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media-f011.mp4"
            source, snap_path = self.build(root)
            output = root / "output"
            config = self.config(root, source, output, snap_path)
            before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            wav = source / "films" / "F011" / "asr" / "audio" / "chunks" / "c0000.wav"
            nlink = os.lstat(wav).st_nlink
            receipt = recovery.prepare(config)
            self.assertEqual(os.lstat(wav).st_nlink, nlink)
            self.assertEqual(nlink, 1)
            copied_wav = output / "films" / "F011" / "asr" / "audio" / "chunks" / "c0000.wav"
            self.assertEqual(copied_wav.read_bytes(), wav.read_bytes())
            self.assertNotEqual(os.lstat(copied_wav).st_ino, os.lstat(wav).st_ino)
            after = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            self.assertFalse((output / "preflight.json").exists())
            self.assertFalse((output / "progress.json").exists())
            self.assertNotIn("old-recovery-receipt", (output / "recovery-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(
                (output / "films" / "F001" / "asr" / "protocol.json").read_bytes(),
                (source / "films" / "F001" / "asr" / "protocol.json").read_bytes(),
            )
            self.assertNotIn("alignment_time_policy", (output / "films" / "F001" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(
                (output / "films" / "F001" / "asr" / "chunks.json").read_bytes(),
                (source / "films" / "F001" / "asr" / "chunks.json").read_bytes(),
            )
            self.assertEqual(
                (output / "films" / "F006" / "asr" / "protocol.json").read_bytes(),
                (source / "films" / "F006" / "asr" / "protocol.json").read_bytes(),
            )
            self.assertNotIn("alignment_time_policy", (output / "films" / "F006" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            for name in ("align-tasks.json", "align/c0273.json", "align-language-gaps.json"):
                self.assertEqual(
                    (output / "films" / "F006" / "asr" / name).read_bytes(),
                    (source / "films" / "F006" / "asr" / name).read_bytes(),
                )
            f006_chunks = json.loads((output / "films" / "F006" / "asr" / "chunks.json").read_text(encoding="utf-8"))
            self.assertEqual(
                f006_chunks[0]["wav_path"],
                str((output / "films" / "F006" / "asr" / "audio" / "chunks" / "c0273.wav").resolve()),
            )
            self.assertNotEqual(f006_chunks[0]["wav_path"], "/old-v2/films/F006/asr/audio/chunks/c0273.wav")
            gap_action = batch.next_transcription_action(
                batch.load_checkpoints(output / "films" / "F006" / "asr"),
                json.loads((output / "films" / "F006" / "asr" / "protocol.json").read_text(encoding="utf-8")),
            )
            self.assertEqual(gap_action["action"], "done")
            source_tasks = json.loads((source / "films" / "F011" / "asr" / "align-tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(source_tasks[0]["status"], "failed")
            derived = json.loads((output / "films" / "F011" / "asr" / "align-tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(derived[0]["status"], "succeeded")
            self.assertEqual(derived[0]["reason"], "derived-tail-boundary-validated")
            self.assertEqual(derived[0]["original_validation_failure"]["status"], "failed")
            self.assertEqual(derived[0]["original_validation_failure"]["retained_text"], "甲乙")
            raw_source = source / "films" / "F011" / "asr" / "align" / "c0000.json"
            raw_dest = output / "films" / "F011" / "asr" / "align" / "c0000.json"
            self.assertEqual(raw_dest.read_bytes(), raw_source.read_bytes())
            self.assertEqual(derived[0]["output_sha256"], hashlib.sha256(raw_dest.read_bytes()).hexdigest())
            self.assertEqual(json.loads(raw_dest.read_text(encoding="utf-8"))["units"][-1]["end_time"], 28.96)
            chunks = json.loads((output / "films" / "F011" / "asr" / "chunks.json").read_text(encoding="utf-8"))
            self.assertEqual(chunks[0]["wav_path"], str(copied_wav.resolve()))
            self.assertEqual(chunks[1]["wav_path"], str((output / "films" / "F011" / "asr" / "audio" / "chunks" / "c0001.wav").resolve()))
            self.assertNotIn("/old-v2/", chunks[1]["wav_path"])
            self.assertEqual(hashlib.sha256(copied_wav.read_bytes()).hexdigest(), chunks[0]["wav_sha256"])
            self.assertEqual(
                (output / "films" / "F011" / "asr" / "prepare.json").read_bytes(),
                (source / "films" / "F011" / "asr" / "prepare.json").read_bytes(),
            )
            f011 = json.loads((output / "films" / "F011" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(f011["alignment_time_policy"], transcribe.TAIL_TIME_POLICY)
            self.assertEqual(f011["unsupported_align_language_policy"], recovery.POLICY)
            self.assertEqual(f011["worker_python"], "/worker/python")
            self.assertEqual(f011["asr_model_path"], "/models/asr")
            self.assertEqual(f011["aligner_model_path"], "/models/" + transcribe.ALIGNER_REVISION)
            self.assertEqual(f011["source"]["sourcePath"], str(media))
            self.assertEqual(f011["recovery_origin"]["run"], str((source / "films" / "F011" / "asr").resolve()))
            self.assertNotEqual(f011["recovery_origin"]["run"], str((output / "films" / "F011" / "asr").resolve()))
            self.assertEqual(
                f011["recovery_origin"]["align_tasks_sha256"],
                hashlib.sha256((source / "films" / "F011" / "asr" / "align-tasks.json").read_bytes()).hexdigest(),
            )
            pending = json.loads((output / "films" / "F012" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(pending["alignment_time_policy"], transcribe.TAIL_TIME_POLICY)
            self.assertEqual(pending["unsupported_align_language_policy"], recovery.POLICY)
            self.assertEqual(pending["source"]["sourcePath"], "/media/f012.mp4")
            embed = json.loads((output / "films" / "F011" / "embed" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(embed["asr_run"], str((output / "films" / "F011" / "asr").resolve()))
            self.assertEqual(embed["shared_document_cache"]["directory"], str((output / "shared-document-cache").resolve()))
            self.assertEqual(embed["shared_document_cache"]["scope"], "batch")
            self.assertEqual(embed["worker_python"], "/worker/python")
            self.assertEqual(embed["source"]["sourcePath"], str(media))
            self.assertEqual(embed["joint_windows"][0]["source_path"], str(media))
            settled_embed = json.loads((output / "films" / "F001" / "embed" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(settled_embed["asr_run"], str((output / "films" / "F001" / "asr").resolve()))
            self.assertEqual(settled_embed["source"]["sourcePath"], "/media/f001.mp4")
            action = batch.next_transcription_action(batch.load_checkpoints(output / "films" / "F011" / "asr"), f011)
            self.assertEqual(action, {"action": "run", "stage": "align"})
            self.assertTrue((output / "films" / "F011" / "asr" / "tail-revalidation.json").is_file())
            self.assertIn("乙", (output / "films" / "F011" / "asr" / "tail-revalidation.json").read_text(encoding="utf-8"))
            self.assertFalse((output / "films" / "F011" / "asr" / ".stage.lock").exists())
            self.assertEqual(receipt["hardlink"], False)
            self.assertEqual(receipt["alignmentTimePolicy"], transcribe.TAIL_TIME_POLICY)
            self.assertEqual(receipt["derivedSkips"], [])
            self.assertNotIn("text", receipt["derivedTails"][0])
            self.assertEqual(receipt["derivedTails"][0]["chunk_id"], "c0000")
            self.assertEqual(receipt["derivedTails"][0]["reason"], "derived-tail-boundary-validated")
            self.assertEqual(receipt["derivedTails"][0]["end_retracted_ticks"], 290000)
            self.assertEqual(receipt["derivedTails"][0]["new_model_inputs"], 0)
            self.assertEqual(receipt["derivedTails"][0]["raw_validation_failure"], ["last-unit-out-of-bounds"])
            self.assertIs(receipt["derivedTails"][0]["raw_preserved"], True)
            self.assertNotIn("甲", json.dumps(receipt, ensure_ascii=False))
            self.assertNotIn("乙", json.dumps(receipt, ensure_ascii=False))
            self.assertNotIn("Ik ah", json.dumps(receipt, ensure_ascii=False))
            for name in ("preflight.json", "progress.json", "recovery-receipt.json"):
                self.assertIn(name, receipt["omittedSourceFiles"])
            rows = {item["path"]: item for item in receipt["files"]}
            self.assertEqual(rows["films/F011/asr/chunks.json"]["action"], "rebound")
            self.assertEqual(rows["films/F006/asr/chunks.json"]["action"], "rebound")
            self.assertEqual(rows["films/F001/asr/chunks.json"]["action"], "copied")
            self.assertEqual(rows["films/F001/asr/protocol.json"]["action"], "copied")
            self.assertEqual(rows["films/F011/asr/protocol.json"]["action"], "rebound")
            self.assertEqual(rows["films/F011/asr/align/c0000.json"]["action"], "copied")
            self.assertEqual(rows["films/F011/asr/align-tasks.json"]["action"], "derived")
            self.assertEqual(rows["films/F011/asr/tail-revalidation.json"]["action"], "derived")
            self.assertEqual(rows["films/F011/asr/prepare.json"]["action"], "copied")
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["alignmentTimePolicy"], transcribe.TAIL_TIME_POLICY)
            receipt_bytes = (output / "recovery-receipt.json").read_bytes()
            again = recovery.prepare(config)
            self.assertEqual((output / "recovery-receipt.json").read_bytes(), receipt_bytes)
            self.assertEqual(again["derivedTails"], receipt["derivedTails"])
            self.assertEqual(after, {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()})
            without = dict(config)
            without.pop("alignmentTimePolicy")
            with self.assertRaises(batch.BatchBlocked) as changed_policy:
                recovery.prepare(without)
            self.assertEqual(changed_policy.exception.reason, "output-exists")
            copied_wav.write_bytes(copied_wav.read_bytes() + b"x")
            with self.assertRaises(batch.BatchBlocked) as caught:
                recovery.prepare(config)
            self.assertEqual(caught.exception.reason, "output-exists")
            self.assertEqual((source / "films" / "F011" / "asr" / "align-tasks.json").read_bytes(), before["films/F011/asr/align-tasks.json"])

    def test_tail_outside_the_rule_refuses_before_output(self):
        duration = 462896 / transcribe.SAMPLE_RATE
        cases = (
            ({"tail": False}, "transcription-failed"),
            ({"last_end": duration + 0.081}, "transcription-failed"),
            ({"first_end": duration + 0.02, "last_start": duration + 0.03, "last_end": duration + 0.05}, "transcription-failed"),
            ({"second_tail": True}, "transcription-failed"),
            ({"bad_wav_hash": True}, "source-mismatch"),
            ({"missing_wav": True}, "source-mismatch"),
        )
        for flags, reason in cases:
            with self.subTest(flags=flags):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    options = dict(flags)
                    use_tail = options.pop("tail", True)
                    source, snap_path = self.build(root, **options)
                    output = root / "output"
                    before = (source / "films" / "F011" / "asr" / "align-tasks.json").read_bytes()
                    with self.assertRaises(batch.BatchBlocked) as caught:
                        recovery.prepare(self.config(root, source, output, snap_path, tail=use_tail))
                    self.assertEqual(caught.exception.reason, reason)
                    self.assertFalse(output.exists())
                    self.assertEqual((source / "films" / "F011" / "asr" / "align-tasks.json").read_bytes(), before)

    def test_symlink_refuses_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snap_path = RecoveryPrepare().build(root)
            outside = root / "elsewhere"
            outside.write_bytes(b"x")
            (source / "films" / "F006" / "alias.json").symlink_to(outside)
            write_json(snap_path, snapshot_for(source))
            output = root / "output"
            with self.assertRaises(batch.BatchBlocked) as caught:
                recovery.prepare(RecoveryPrepare().config(root, source, output, snap_path))
            self.assertEqual(caught.exception.reason, "source-mismatch")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
