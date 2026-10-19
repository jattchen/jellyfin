#!/usr/bin/env python3
"""Synthetic recovery prepare. No real batch directory and no hard links."""
import hashlib
import importlib.util
import json
import os
import unittest
from pathlib import Path
import tempfile


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


if __name__ == "__main__":
    unittest.main()
