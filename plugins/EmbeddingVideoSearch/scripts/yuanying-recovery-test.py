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


def unit_vector(index):
    values = [0.0] * 768
    values[index % 768] = 1.0
    return values


def joint_processing():
    return {
        "modalities": ["video", "audio"],
        "video": {
            "duration_seconds": 12.0, "audio_included": True, "has_audio": True,
            "sampled_seconds": [0, 3, 6, 9], "offsets_seconds": [0, 3, 6, 9],
            "frame_count": 4, "profile": batch.contract.PROFILE, "window_seconds": 12,
            "max_soft_tokens": 70, "add_timestamps": False, "overflow_strategy": "reject",
        },
        "audio": {
            "duration_seconds": 12.0, "sample_rate": 16000, "channels": 1,
            "truncated": False, "coverage": "full_track",
        },
    }


def embedding_response(signature, processing, vector):
    return {
        "model": signature["id"], "revision": signature["revision"],
        "processing_id": signature["processing_id"], "dimension": 768, "normalized": True,
        "data": [{"index": 0, "embedding": vector, "processing": processing}],
    }


class StorageInterruption(unittest.TestCase):
    """Partial joint tree. No model call and no real recovery directory."""

    def config(self, root, source, output, snapshot_path, authorization):
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
            "alignmentTimePolicy": batch.ALIGNMENT_TIME_POLICY,
            "recoverySource": str(source),
            "recoverySnapshot": str(snapshot_path),
        }
        if authorization is not None:
            body["storageInterruptionRecovery"] = authorization
        return body

    def film_source(self, path, item, runtime):
        return {
            "itemId": item, "mediaSourceId": "mediasource",
            "libraryId": "22222222-2222-4222-8222-222222222222",
            "contentVersion": "content-fixed", "runtimeTicks": runtime,
            "container": "mp4", "audioStreamIndex": 1, "sourceItemId": item,
            "sourcePath": str(path), "sizeBytes": 4, "modifiedTicks": 1,
        }

    def embed_protocol(self, source, label, media, item, runtime):
        signature = batch.contract.signature_template(batch.EXPECTED_PROCESSING_ID)
        facts = self.film_source(media, item, runtime)
        entries = batch.window_entries(facts)
        return {
            "version": "issue3-yuanying-film-v1",
            "fullFilm": True,
            "label": label,
            "asr_run": str((source / "films" / label / "asr").resolve()),
            "shared_document_cache": {
                "directory": str((source / "shared-document-cache").resolve()),
                "scope": "batch",
            },
            "embedding_base_url": batch.contract.ISOLATED_BASE,
            "max_inflight_embeddings": 1,
            "signature": signature,
            "window": batch.contract.window_template(),
            "source": facts,
            "joint_windows": entries,
            "worker_python": "/worker/python",
            "transcription_language_policy": "auto-v1",
        }

    def write_clip(self, source, label, candidate, payload):
        clip = source / "films" / label / "embed" / "clips" / f"{candidate}.mp4"
        digest = write_bytes(clip, payload)
        write_json(clip.with_suffix(".clip.json"), {"sha256": digest, "bytes": len(payload)})
        return digest

    def write_succeeded_joint(self, source, protocol, entry, vector):
        label = protocol["label"]
        candidate = entry["candidate_id"]
        clip_sha = self.write_clip(source, label, candidate, b"clip-" + candidate.encode())
        body = embedding_response(protocol["signature"], joint_processing(), vector)
        response_sha = write_json(source / "films" / label / "embed" / "joint" / f"{candidate}.json", body)
        digest, _window = batch.embed.embedding_input(protocol, "joint", entry, clip_sha)
        write_json(source / "films" / label / "embed" / "joint" / f"{candidate}.state.json", {
            "candidate_id": candidate, "status": "succeeded", "input_hash": digest,
            "response_sha256": response_sha, "clip": {"sha256": clip_sha},
        })
        return digest

    def write_transcription(self, source, label, protocol, *, origin=None, skipped=False):
        asr = source / "films" / label / "asr"
        write_json(asr / "protocol.json", protocol)
        output_sha = write_bytes(asr / "asr" / "c0214.json", b"{\"text\":\"kept\"}\n")
        align_sha = write_bytes(asr / "align" / "c0214.json", b"{\"end\":28.96}\n")
        write_json(asr / "prepare.json", {"status": "succeeded", "full_wav": "audio/full.wav"})
        write_json(asr / "chunks.json", [{"chunk_id": "c0214"}])
        write_json(asr / "asr-tasks.json", [{
            "chunk_id": "c0214", "status": "succeeded", "output_sha256": output_sha,
        }])
        if skipped:
            task = {
                "chunk_id": "c0214", "status": "skipped", "reason": "language-unsupported",
                "skip_policy": recovery.POLICY, "alignment_executed": False,
                "detected_language": "Dutch", "output_sha256": align_sha,
            }
        else:
            task = {
                "chunk_id": "c0214", "status": "succeeded",
                "reason": "derived-tail-boundary-validated", "output_sha256": align_sha,
            }
        write_json(asr / "align-tasks.json", [task])
        write_json(asr / "documents-state.json", {"status": "succeeded"})
        write_json(asr / "candidates.json", [])
        if origin is not None:
            body = json.loads((asr / "protocol.json").read_text(encoding="utf-8"))
            body["recovery_origin"] = {
                "run": origin,
                "align_tasks_sha256": hashlib.sha256((asr / "align-tasks.json").read_bytes()).hexdigest(),
            }
            body["alignment_time_policy"] = transcribe.TAIL_TIME_POLICY
            body["unsupported_align_language_policy"] = recovery.POLICY
            write_json(asr / "protocol.json", body)

    def build(self, root, **flags):
        source = root / "source"
        media = root / "media-f011.mp4"
        write_bytes(media, b"film")
        signature = batch.contract.signature_template(batch.EXPECTED_PROCESSING_ID)
        f011 = self.embed_protocol(
            source, "F011", media, "11111111-1111-4111-8111-111111111111", 420_000_000)
        self.assertEqual(len(f011["joint_windows"]), 4)
        succeeded, inflight, partial, cached = f011["joint_windows"]
        self.write_transcription(source, "F011", {
            "version": "issue3-yuanying-asr-v1", "fullFilm": True, "label": "F011",
            "transcription_language_policy": "auto-v1",
            "worker_python": "/worker/python",
            "asr_model_path": "/models/asr", "aligner_model_path": "/models/align",
            "source": f011["source"],
        }, origin="/older-tree/films/F011/asr")
        wav_sha = write_bytes(source / "films" / "F011" / "asr" / "audio" / "chunks" / "c0214.wav", b"RIFFfake")
        write_json(source / "films" / "F011" / "asr" / "chunks.json", [{
            "chunk_id": "c0214", "wav_sha256": wav_sha,
            "wav_path": "/old-v2/films/F011/asr/audio/chunks/c0214.wav",
        }])
        vector = [1.0, 0.0] if flags.get("bad_vector") else unit_vector(0)
        self.write_succeeded_joint(source, f011, succeeded, vector)
        if flags.get("bad_response_hash"):
            state_path = source / "films" / "F011" / "embed" / "joint" / f"{succeeded['candidate_id']}.state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["response_sha256"] = "ab" * 32
            write_json(state_path, state)
        if flags.get("bad_clip_hash"):
            meta = source / "films" / "F011" / "embed" / "clips" / f"{succeeded['candidate_id']}.clip.json"
            body = json.loads(meta.read_text(encoding="utf-8"))
            body["sha256"] = "ab" * 32
            write_json(meta, body)
        inflight_sha = self.write_clip(source, "F011", inflight["candidate_id"], b"clip-inflight")
        inflight_hash, _window = batch.embed.embedding_input(f011, "joint", inflight, inflight_sha)
        inflight_state = {
            "candidate_id": inflight["candidate_id"], "status": "inflight",
            "input_hash": inflight_hash, "clip": {"sha256": inflight_sha},
            "source_facts": {"size": 4}, "started_at": 0,
        }
        if flags.get("terminal_response"):
            inflight_state["status"] = "terminal-response"
            write_json(source / "films" / "F011" / "embed" / "joint" / f"{inflight['candidate_id']}.json", {"http": 500})
        if flags.get("inflight_response"):
            write_json(source / "films" / "F011" / "embed" / "joint" / f"{inflight['candidate_id']}.json", {"http": 200})
        if flags.get("authorized_succeeded"):
            body = embedding_response(signature, joint_processing(), unit_vector(1))
            response_sha = write_json(
                source / "films" / "F011" / "embed" / "joint" / f"{inflight['candidate_id']}.json", body)
            inflight_state["status"] = "succeeded"
            inflight_state["response_sha256"] = response_sha
        write_json(source / "films" / "F011" / "embed" / "joint" / f"{inflight['candidate_id']}.state.json", inflight_state)
        write_bytes(
            source / "films" / "F011" / "embed" / "clips" / f"{partial['candidate_id']}.partial.mp4",
            b"partial-bytes",
        )
        if flags.get("half_clip"):
            write_bytes(source / "films" / "F011" / "embed" / "clips" / f"{cached['candidate_id']}.mp4", b"half")
        else:
            self.write_clip(source, "F011", cached["candidate_id"], b"clip-cached")
        if flags.get("second_unknown"):
            cached_sha = hashlib.sha256((source / "films" / "F011" / "embed" / "clips" / f"{cached['candidate_id']}.mp4").read_bytes()).hexdigest()
            cached_hash, _window = batch.embed.embedding_input(f011, "joint", cached, cached_sha)
            write_json(source / "films" / "F011" / "embed" / "joint" / f"{cached['candidate_id']}.state.json", {
                "candidate_id": cached["candidate_id"], "status": "inflight",
                "input_hash": cached_hash, "clip": {"sha256": cached_sha},
            })
        if flags.get("orphan_response"):
            write_json(source / "films" / "F011" / "embed" / "joint" / "orphan-joint.json", {"orphan": True})
        write_json(source / "films" / "F011" / "embed" / "protocol.json", f011)
        f001_media = root / "media-f001.mp4"
        write_bytes(f001_media, b"film")
        f001 = self.embed_protocol(
            source, "F001", f001_media, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", batch.contract.LENGTH_TICKS)
        self.write_transcription(source, "F001", {
            "version": "issue3-yuanying-asr-v1", "fullFilm": True, "label": "F001",
            "transcription_language_policy": "auto-v1", "source": f001["source"],
        })
        self.write_succeeded_joint(source, f001, f001["joint_windows"][0], unit_vector(2))
        text_entry = {"candidate_id": "c0000-U01", "text": "synthetic line"}
        text_hash, _window = batch.embed.embedding_input(f001, "text", text_entry)
        text_body = embedding_response(signature, {"modalities": ["text"], "task": "document"}, unit_vector(5))
        text_path = source / "films" / "F001" / "embed" / "text" / "c0000-U01.json"
        if flags.get("text_unknown"):
            write_json(source / "films" / "F001" / "embed" / "text" / "c0000-U01.state.json", {
                "candidate_id": "c0000-U01", "status": "inflight", "input_hash": text_hash,
            })
        else:
            text_sha = write_json(text_path, text_body)
            write_json(source / "films" / "F001" / "embed" / "text" / "c0000-U01.state.json", {
                "candidate_id": "c0000-U01", "status": "succeeded", "input_hash": text_hash,
                "response_sha256": text_sha,
            })
        write_json(source / "films" / "F001" / "embed" / "candidates.json", [text_entry])
        write_json(source / "films" / "F001" / "embed" / "protocol.json", f001)
        f006_media = root / "media-f006.mp4"
        write_bytes(f006_media, b"film")
        f006 = self.embed_protocol(
            source, "F006", f006_media, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", batch.contract.LENGTH_TICKS)
        self.write_transcription(source, "F006", {
            "version": "issue3-yuanying-asr-v1", "fullFilm": True, "label": "F006",
            "transcription_language_policy": "auto-v1",
            "unsupported_align_language_policy": recovery.POLICY,
            "source": f006["source"],
        }, skipped=True)
        self.write_succeeded_joint(source, f006, f006["joint_windows"][0], unit_vector(3))
        write_json(source / "films" / "F006" / "embed" / "protocol.json", f006)
        f012 = self.embed_protocol(
            source, "F012", root / "media-f012.mp4", "cccccccc-cccc-4ccc-8ccc-cccccccccccc", batch.contract.LENGTH_TICKS)
        write_json(source / "films" / "F012" / "asr" / "protocol.json", {
            "transcription_language_policy": "auto-v1",
            "unsupported_align_language_policy": recovery.POLICY,
            "alignment_time_policy": transcribe.TAIL_TIME_POLICY,
            "source": {"sourcePath": str(root / "media-f012.mp4")},
        })
        write_json(source / "films" / "F012" / "embed" / "protocol.json", f012)
        document = {"candidate_id": "doc-1", "text": "synthetic document"}
        document_hash, _window = batch.embed.embedding_input(f011, "text", document)
        document_body = embedding_response(
            signature, {"modalities": ["text"], "task": "document"}, unit_vector(4))
        document_sha = write_json(source / "shared-document-cache" / f"{document_hash}.json", document_body)
        write_json(source / "shared-document-cache" / f"{document_hash}.state.json", {
            "status": "succeeded", "kind": "document", "input_hash": document_hash,
            "response_sha256": document_sha,
        })
        write_json(source / "manifest.json", {"films": [{"label": "F011"}, {"label": "F001"}, {"label": "F006"}, {"label": "F012"}]})
        write_bytes(source / "preflight.json", b"{\"ready\":true}\n")
        write_bytes(source / "recovery-receipt.json", b"{\"marker\":\"old-recovery-receipt\"}\n")
        snap = snapshot_for(source)
        snap_path = root / "snapshot.json"
        write_json(snap_path, snap)
        state_path = source / "films" / "F011" / "embed" / "joint" / f"{inflight['candidate_id']}.state.json"
        authorization = {
            "version": recovery.INTERRUPTION_MODE,
            "film": "F011",
            "candidate": inflight["candidate_id"],
            "inputHash": inflight_hash,
            "stateSha256": hashlib.sha256(state_path.read_bytes()).hexdigest(),
            "snapshotSha256": hashlib.sha256(snap_path.read_bytes()).hexdigest(),
        }
        if flags.get("wrong_candidate"):
            authorization["candidate"] = "joint-not-the-inflight"
        if flags.get("wrong_input"):
            authorization["inputHash"] = "12" * 32
        if flags.get("wrong_state"):
            authorization["stateSha256"] = "34" * 32
        if flags.get("wrong_snapshot"):
            authorization["snapshotSha256"] = "56" * 32
        return source, snap_path, authorization, inflight["candidate_id"]

    def prepare(self, config):
        original = batch.embed.dispatch

        def boom(*_args, **_kwargs):
            raise AssertionError("model dispatch")

        batch.embed.dispatch = boom
        try:
            return recovery.prepare(config)
        finally:
            batch.embed.dispatch = original

    def test_prepare_omits_only_the_authorized_inflight_joint(self):
        source_text = Path(recovery.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.link", source_text)
        self.assertNotIn("clonefile", source_text)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snap_path, authorization, candidate = self.build(root)
            output = root / "output"
            before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            state_rel = f"films/F011/embed/joint/{candidate}.state.json"
            nlink = os.lstat(source / state_rel).st_nlink
            receipt = self.prepare(self.config(root, source, output, snap_path, authorization))
            self.assertEqual(os.lstat(source / state_rel).st_nlink, 1)
            self.assertEqual(nlink, 1)
            after = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            self.assertFalse((output / state_rel).exists())
            self.assertFalse((output / "preflight.json").exists())
            self.assertFalse((output / "films" / "F011" / "asr" / "tail-revalidation.json").exists())
            self.assertFalse((output / "films" / "F011" / "asr" / ".stage.lock").exists())
            succeeded_id = json.loads((output / "films" / "F011" / "embed" / "protocol.json").read_text(encoding="utf-8"))["joint_windows"][0]["candidate_id"]
            copied_response = output / "films" / "F011" / "embed" / "joint" / f"{succeeded_id}.json"
            original_response = source / "films" / "F011" / "embed" / "joint" / f"{succeeded_id}.json"
            self.assertEqual(copied_response.read_bytes(), original_response.read_bytes())
            self.assertNotEqual(os.lstat(copied_response).st_ino, os.lstat(original_response).st_ino)
            self.assertEqual(
                (output / "films" / "F011" / "asr" / "protocol.json").read_bytes(),
                (source / "films" / "F011" / "asr" / "protocol.json").read_bytes(),
            )
            self.assertIn("/older-tree/films/F011/asr", (output / "films" / "F011" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            self.assertNotIn(str(output), (output / "films" / "F011" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(
                (output / "films" / "F011" / "asr" / "align" / "c0214.json").read_bytes(),
                (source / "films" / "F011" / "asr" / "align" / "c0214.json").read_bytes(),
            )
            self.assertEqual(
                (output / "films" / "F011" / "asr" / "align-tasks.json").read_bytes(),
                (source / "films" / "F011" / "asr" / "align-tasks.json").read_bytes(),
            )
            self.assertEqual(
                (output / "films" / "F001" / "asr" / "protocol.json").read_bytes(),
                (source / "films" / "F001" / "asr" / "protocol.json").read_bytes(),
            )
            self.assertEqual(
                (output / "films" / "F006" / "asr" / "align-tasks.json").read_bytes(),
                (source / "films" / "F006" / "asr" / "align-tasks.json").read_bytes(),
            )
            self.assertEqual(
                (output / "films" / "F012" / "asr" / "protocol.json").read_bytes(),
                (source / "films" / "F012" / "asr" / "protocol.json").read_bytes(),
            )
            self.assertEqual((output / "manifest.json").read_bytes(), (source / "manifest.json").read_bytes())
            chunks = json.loads((output / "films" / "F011" / "asr" / "chunks.json").read_text(encoding="utf-8"))
            self.assertNotIn("/old-v2/", chunks[0]["wav_path"])
            self.assertTrue(chunks[0]["wav_path"].endswith("films/F011/asr/audio/chunks/c0214.wav"))
            rebound = json.loads((output / "films" / "F011" / "embed" / "protocol.json").read_text(encoding="utf-8"))
            self.assertTrue(rebound["asr_run"].endswith("films/F011/asr"))
            self.assertTrue(rebound["shared_document_cache"]["directory"].endswith("shared-document-cache"))
            self.assertEqual(rebound["shared_document_cache"]["scope"], "batch")
            self.assertEqual(rebound["source"]["sourcePath"], str(root / "media-f011.mp4"))
            self.assertEqual(rebound["joint_windows"], f011_windows(source))
            partial_name = rebound["joint_windows"][2]["candidate_id"] + ".partial.mp4"
            self.assertFalse((output / "films" / "F011" / "embed" / "clips" / partial_name).exists())
            self.assertTrue((source / "films" / "F011" / "embed" / "clips" / partial_name).is_file())
            cached_id = rebound["joint_windows"][3]["candidate_id"]
            self.assertTrue((output / "films" / "F011" / "embed" / "clips" / f"{cached_id}.mp4").is_file())
            self.assertFalse((output / "films" / "F011" / "embed" / "joint" / f"{candidate}.state.json").exists())
            self.assertTrue((output / "films" / "F011" / "embed" / "clips" / f"{candidate}.mp4").is_file())
            digest, _window = batch.embed.embedding_input(rebound, "joint", rebound["joint_windows"][0], batch.joint_clip_sha(output / "films" / "F011" / "embed", rebound["joint_windows"][0]))
            stored = json.loads((output / "films" / "F011" / "embed" / "joint" / f"{succeeded_id}.state.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["input_hash"], digest)
            sent = []
            decision = batch.dispatch_entries(
                rebound["joint_windows"],
                lambda entry: batch.state_for(output / "films" / "F011" / "embed", "joint", entry),
                sent.append, rebound, "joint",
                lambda entry: batch.joint_clip_sha(output / "films" / "F011" / "embed", entry),
            )
            self.assertEqual([item["candidate_id"] for item in sent], [
                rebound["joint_windows"][1]["candidate_id"],
                rebound["joint_windows"][2]["candidate_id"],
                rebound["joint_windows"][3]["candidate_id"],
            ])
            self.assertEqual(decision["reused"], [succeeded_id])
            self.assertFalse(decision["resentUnknown"])
            self.assertEqual(receipt["mode"], recovery.INTERRUPTION_MODE)
            self.assertEqual(receipt["authorization"], authorization)
            self.assertEqual(receipt["modelRequests"], 0)
            self.assertEqual(receipt["authorizedResends"], 1)
            self.assertIs(receipt["batchExitAccepted"], False)
            self.assertIs(receipt["transcriptionAccuracyVerified"], False)
            self.assertEqual(receipt["reusedSucceededJoints"], 3)
            self.assertEqual(receipt["notStartedJoints"], 2)
            self.assertEqual(receipt["cachedClipsWithoutDispatch"], 1)
            self.assertEqual(receipt["omittedDispatch"], [state_rel])
            self.assertEqual(receipt["unknownEvidence"]["status"], "inflight")
            self.assertIs(receipt["unknownEvidence"]["responsePresent"], False)
            self.assertEqual(receipt["unknownEvidence"]["stateSha256"], authorization["stateSha256"])
            self.assertEqual(receipt["omittedIncompleteClips"], [{
                "path": f"films/F011/embed/clips/{partial_name}",
                "reason": "undispatched-partial",
                "successRecord": False,
            }])
            self.assertIn("preflight.json", receipt["omittedSourceFiles"])
            self.assertIn("recovery-receipt.json", receipt["omittedSourceFiles"])
            self.assertNotIn("synthetic document", json.dumps(receipt))
            self.assertNotIn("source_facts", json.dumps(receipt["unknownEvidence"]))
            again = self.prepare(self.config(root, source, output, snap_path, authorization))
            self.assertEqual(again, receipt)
            without = dict(self.config(root, source, output, snap_path, authorization))
            without.pop("storageInterruptionRecovery")
            with self.assertRaises(batch.BatchBlocked) as caught:
                self.prepare(without)
            self.assertEqual(caught.exception.reason, "output-exists")
            copied_response.write_bytes(copied_response.read_bytes() + b"\n")
            with self.assertRaises(batch.BatchBlocked) as caught:
                self.prepare(self.config(root, source, output, snap_path, authorization))
            self.assertEqual(caught.exception.reason, "output-exists")

    def test_refusals_happen_before_output(self):
        cases = (
            ({"omit_authorization": True}, "transcription-failed"),
            ({"wrong_candidate": True}, "source-mismatch"),
            ({"wrong_input": True}, "source-mismatch"),
            ({"wrong_state": True}, "source-mismatch"),
            ({"wrong_snapshot": True}, "source-mismatch"),
            ({"second_unknown": True}, "not-succeeded"),
            ({"terminal_response": True}, "not-succeeded"),
            ({"inflight_response": True}, "source-mismatch"),
            ({"authorized_succeeded": True}, "not-succeeded"),
            ({"orphan_response": True}, "source-mismatch"),
            ({"bad_response_hash": True}, "cache-hash-mismatch"),
            ({"bad_clip_hash": True}, "cache-hash-mismatch"),
            ({"bad_vector": True}, "signature-mismatch"),
            ({"half_clip": True}, "source-mismatch"),
            ({"text_unknown": True}, "not-succeeded"),
        )
        for flags, reason in cases:
            with self.subTest(flags=flags):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    options = dict(flags)
                    omit = options.pop("omit_authorization", False)
                    source, snap_path, authorization, _candidate = self.build(root, **options)
                    output = root / "output"
                    before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
                    with self.assertRaises(batch.BatchBlocked) as caught:
                        self.prepare(self.config(root, source, output, snap_path, None if omit else authorization))
                    self.assertEqual(caught.exception.reason, reason)
                    self.assertFalse(output.exists())
                    after = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
                    self.assertEqual(after, before)


def f011_windows(source):
    return json.loads((source / "films" / "F011" / "embed" / "protocol.json").read_text(encoding="utf-8"))["joint_windows"]


def tail_probe(audio_seconds, video_seconds):
    return {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0.000000", "duration": video_seconds},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": audio_seconds},
    ]}


class VideoTailRecovery(unittest.TestCase):
    """One succeeded window and one last-window partial. No model call."""

    def config(self, root, source, output, snapshot_path, authorization):
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
            "alignmentTimePolicy": batch.ALIGNMENT_TIME_POLICY,
            "videoTailPolicy": batch.contract.VIDEO_TAIL_POLICY,
            "recoverySource": str(source),
            "recoverySnapshot": str(snapshot_path),
        }
        if authorization is not None:
            body["videoTailRecovery"] = authorization
        return body

    def film_source(self, path, item, runtime):
        return {
            "itemId": item, "mediaSourceId": "mediasource",
            "libraryId": "22222222-2222-4222-8222-222222222222",
            "contentVersion": "content-fixed", "runtimeTicks": runtime,
            "container": "mp4", "audioStreamIndex": 1, "sourceItemId": item,
            "sourcePath": str(path), "sizeBytes": 4, "modifiedTicks": 1,
        }

    def embed_protocol(self, source, label, media, item, runtime, probe):
        facts = self.film_source(media, item, runtime)
        signature = batch.contract.signature_template(batch.EXPECTED_PROCESSING_ID)
        return {
            "version": "issue3-yuanying-film-v1",
            "fullFilm": True,
            "label": label,
            "asr_run": str((source / "films" / label / "asr").resolve()),
            "shared_document_cache": {
                "directory": str((source / "shared-document-cache").resolve()),
                "scope": "batch",
            },
            "embedding_base_url": batch.contract.ISOLATED_BASE,
            "max_inflight_embeddings": 1,
            "signature": signature,
            "window": batch.contract.window_template(),
            "source": facts,
            "probe_facts": probe,
            "joint_windows": batch.window_entries(facts),
            "worker_python": "/worker/python",
            "transcription_language_policy": "auto-v1",
        }

    def write_clip(self, source, label, candidate, payload):
        clip = source / "films" / label / "embed" / "clips" / f"{candidate}.mp4"
        digest = write_bytes(clip, payload)
        write_json(clip.with_suffix(".clip.json"), {"sha256": digest, "bytes": len(payload)})
        return digest

    def write_succeeded_joint(self, source, protocol, entry):
        label = protocol["label"]
        candidate = entry["candidate_id"]
        clip_sha = self.write_clip(source, label, candidate, b"clip-" + candidate.encode())
        body = embedding_response(protocol["signature"], joint_processing(), unit_vector(0))
        response_sha = write_json(source / "films" / label / "embed" / "joint" / f"{candidate}.json", body)
        digest, _window = batch.embed.embedding_input(protocol, "joint", entry, clip_sha)
        write_json(source / "films" / label / "embed" / "joint" / f"{candidate}.state.json", {
            "candidate_id": candidate, "status": "succeeded", "input_hash": digest,
            "response_sha256": response_sha, "clip": {"sha256": clip_sha},
        })
        return digest

    def write_settled_asr(self, source, label, facts, origin=None):
        asr = source / "films" / label / "asr"
        protocol = {
            "version": "issue3-yuanying-asr-v1", "fullFilm": True, "label": label,
            "transcription_language_policy": "auto-v1",
            "unsupported_align_language_policy": recovery.POLICY,
            "alignment_time_policy": transcribe.TAIL_TIME_POLICY,
            "worker_python": "/worker/python",
            "source": facts,
        }
        if origin is not None:
            protocol["recovery_origin"] = {"run": origin}
        write_json(asr / "protocol.json", protocol)
        output_sha = write_bytes(asr / "asr" / "c0001.json", b"{}\n")
        align_sha = write_bytes(asr / "align" / "c0001.json", b"{}\n")
        write_json(asr / "prepare.json", {"status": "succeeded", "full_wav": "audio/full.wav"})
        write_json(asr / "chunks.json", [{"chunk_id": "c0001"}])
        write_json(asr / "asr-tasks.json", [{
            "chunk_id": "c0001", "status": "succeeded", "output_sha256": output_sha,
        }])
        write_json(asr / "align-tasks.json", [{
            "chunk_id": "c0001", "status": "succeeded", "output_sha256": align_sha,
        }])
        write_json(asr / "documents-state.json", {"status": "succeeded"})
        write_json(asr / "candidates.json", [])

    def build(self, root, **flags):
        source = root / "source"
        f020_media = root / "media-f020.mp4"
        write_bytes(f020_media, b"film")
        f020_probe = tail_probe("20.000000", "19.850113")
        if flags.get("unknown_authorized"):
            f020_probe = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}
        f020 = self.embed_protocol(
            source, "F020", f020_media, "11111111-1111-4111-8111-111111111120",
            200_000_000, f020_probe,
        )
        self.assertEqual(len(f020["joint_windows"]), 2)
        first, last = f020["joint_windows"]
        self.assertEqual(last["end_ticks"], 200_000_000)
        self.write_settled_asr(source, "F020", f020["source"], origin="/older-tree/films/F020/asr")
        if flags.get("not_last"):
            partial_entry = first
            succeeded_entry = last
        else:
            partial_entry = last
            succeeded_entry = first
        self.write_succeeded_joint(source, f020, succeeded_entry)
        if flags.get("inflight"):
            state_path = source / "films" / "F020" / "embed" / "joint" / f"{succeeded_entry['candidate_id']}.state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["status"] = "inflight"
            state.pop("response_sha256", None)
            write_json(state_path, state)
        partial_sha = write_bytes(
            source / "films" / "F020" / "embed" / "clips" / f"{partial_entry['candidate_id']}.partial.mp4",
            b"partial-tail",
        )
        if flags.get("second_partial"):
            write_bytes(source / "films" / "F020" / "embed" / "clips" / "extra.partial.mp4", b"second")
        write_json(source / "films" / "F020" / "embed" / "protocol.json", f020)
        f001_media = root / "media-f001.mp4"
        write_bytes(f001_media, b"film")
        f001_video = "11.500000" if flags.get("over_bound_other") else "11.940000"
        f001 = self.embed_protocol(
            source, "F001", f001_media, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            batch.contract.LENGTH_TICKS, tail_probe("12.000000", f001_video),
        )
        self.write_settled_asr(source, "F001", f001["source"])
        self.write_succeeded_joint(source, f001, f001["joint_windows"][0])
        write_json(source / "films" / "F001" / "embed" / "protocol.json", f001)
        write_json(source / "manifest.json", {"films": [{"label": "F001"}, {"label": "F020"}]})
        write_bytes(source / "preflight.json", b"{\"ready\":true}\n")
        snap = snapshot_for(source)
        snap_path = root / "snapshot.json"
        write_json(snap_path, snap)
        authorization = {
            "version": recovery.VIDEO_TAIL_MODE,
            "film": "F020",
            "candidate": partial_entry["candidate_id"],
            "partialSha256": "12" * 32 if flags.get("wrong_partial") else partial_sha,
            "snapshotSha256": "56" * 32 if flags.get("wrong_snapshot") else hashlib.sha256(snap_path.read_bytes()).hexdigest(),
        }
        return source, snap_path, authorization, partial_entry["candidate_id"]

    def prepare(self, config):
        original = batch.embed.dispatch

        def boom(*_args, **_kwargs):
            raise AssertionError("model dispatch")

        batch.embed.dispatch = boom
        try:
            return recovery.prepare(config)
        finally:
            batch.embed.dispatch = original

    def test_prepare_omits_only_the_undispatched_last_window(self):
        source_text = Path(recovery.__file__).read_text(encoding="utf-8")
        self.assertNotIn("os.link", source_text)
        self.assertNotIn("clonefile", source_text)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snap_path, authorization, candidate = self.build(root)
            output = root / "output"
            before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            partial_rel = f"films/F020/embed/clips/{candidate}.partial.mp4"
            nlink = os.lstat(source / partial_rel).st_nlink
            receipt = self.prepare(self.config(root, source, output, snap_path, authorization))
            self.assertEqual(os.lstat(source / partial_rel).st_nlink, 1)
            self.assertEqual(nlink, 1)
            after = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            self.assertEqual(after, before)
            self.assertFalse((output / partial_rel).exists())
            self.assertTrue((source / partial_rel).is_file())
            self.assertFalse((output / "films" / "F020" / "asr" / "tail-revalidation.json").exists())
            self.assertEqual(
                (output / "films" / "F020" / "asr" / "protocol.json").read_bytes(),
                (source / "films" / "F020" / "asr" / "protocol.json").read_bytes(),
            )
            self.assertIn("/older-tree/films/F020/asr", (output / "films" / "F020" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual((output / "manifest.json").read_bytes(), (source / "manifest.json").read_bytes())
            rebound = json.loads((output / "films" / "F020" / "embed" / "protocol.json").read_text(encoding="utf-8"))
            self.assertEqual(rebound["video_tail_policy"], batch.contract.VIDEO_TAIL_POLICY)
            self.assertTrue(rebound["asr_run"].endswith("films/F020/asr"))
            self.assertNotIn("video_tail_policy", (output / "films" / "F020" / "asr" / "protocol.json").read_text(encoding="utf-8"))
            succeeded = rebound["joint_windows"][0]
            digest, _window = batch.embed.embedding_input(
                rebound, "joint", succeeded,
                batch.joint_clip_sha(output / "films" / "F020" / "embed", succeeded),
            )
            stored = json.loads((output / "films" / "F020" / "embed" / "joint" / f"{succeeded['candidate_id']}.state.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["input_hash"], digest)
            sent = []
            decision = batch.dispatch_entries(
                rebound["joint_windows"],
                lambda entry: batch.state_for(output / "films" / "F020" / "embed", "joint", entry),
                sent.append, rebound, "joint",
                lambda entry: batch.joint_clip_sha(output / "films" / "F020" / "embed", entry),
            )
            self.assertEqual([item["candidate_id"] for item in sent], [candidate])
            self.assertEqual(decision["reused"], [succeeded["candidate_id"]])
            self.assertFalse(decision["resentUnknown"])
            self.assertEqual(receipt["mode"], recovery.VIDEO_TAIL_MODE)
            self.assertEqual(receipt["authorization"], authorization)
            self.assertEqual(receipt["modelRequests"], 0)
            self.assertEqual(receipt["rebuiltPartial"], 1)
            self.assertNotIn("authorizedResends", receipt)
            self.assertEqual(receipt["videoTailPolicy"], batch.contract.VIDEO_TAIL_POLICY)
            self.assertIs(receipt["batchExitAccepted"], False)
            self.assertIs(receipt["transcriptionAccuracyVerified"], False)
            self.assertEqual(receipt["reusedSucceededJoints"], 2)
            self.assertEqual(receipt["notStartedJoints"], 1)
            self.assertEqual(receipt["unknownEvidence"]["status"], "undispatched-partial")
            self.assertIs(receipt["unknownEvidence"]["responsePresent"], False)
            self.assertIs(receipt["unknownEvidence"]["dispatchPresent"], False)
            self.assertEqual(receipt["omittedIncompleteClips"], [{
                "path": partial_rel, "reason": "undispatched-partial", "successRecord": False,
            }])
            self.assertNotIn("media-f020", json.dumps(receipt))
            self.assertNotIn("source_facts", json.dumps(receipt))
            again = self.prepare(self.config(root, source, output, snap_path, authorization))
            self.assertEqual(again, receipt)
            without = dict(self.config(root, source, output, snap_path, authorization))
            without.pop("videoTailRecovery")
            with self.assertRaises(batch.BatchBlocked) as caught:
                self.prepare(without)
            self.assertEqual(caught.exception.reason, "output-exists")

    def test_refusals_happen_before_output(self):
        cases = (
            ({"omit_authorization": True}, "transcription-failed"),
            ({"wrong_partial": True}, "source-mismatch"),
            ({"wrong_snapshot": True}, "source-mismatch"),
            ({"second_partial": True}, "source-mismatch"),
            ({"inflight": True}, "not-succeeded"),
            ({"not_last": True}, "source-mismatch"),
            ({"unknown_authorized": True}, "video-tail"),
            ({"over_bound_other": True}, "video-tail"),
            ({"missing_policy": True}, "config"),
            ({"both_modes": True}, "config"),
        )
        for flags, reason in cases:
            with self.subTest(flags=flags):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    options = dict(flags)
                    omit = options.pop("omit_authorization", False)
                    missing_policy = options.pop("missing_policy", False)
                    both_modes = options.pop("both_modes", False)
                    source, snap_path, authorization, _candidate = self.build(root, **options)
                    output = root / "output"
                    before = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
                    config = self.config(root, source, output, snap_path, None if omit else authorization)
                    if missing_policy:
                        config.pop("videoTailPolicy")
                    if both_modes:
                        config["storageInterruptionRecovery"] = {
                            "version": recovery.INTERRUPTION_MODE,
                            "film": "F011",
                            "candidate": "joint-named-once",
                            "inputHash": "ab" * 32,
                            "stateSha256": "cd" * 32,
                            "snapshotSha256": "ef" * 32,
                        }
                    with self.assertRaises(batch.BatchBlocked) as caught:
                        self.prepare(config)
                    self.assertEqual(caught.exception.reason, reason)
                    self.assertFalse(output.exists())
                    after = {str(path.relative_to(source)): path.read_bytes() for path in source.rglob("*") if path.is_file()}
                    self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
