#!/usr/bin/env python3
"""Copy one finished batch into a new directory and derive one language gap.

The source tree is read only. Every reused file is a new byte copy, never a
hard link, so a later write cannot change the source. The only rewrite is the
single align task whose reason is language-unsupported: the new directory
records that chunk as an explicit skip. Any other failure, unknown checkpoint,
or orphan output refuses the whole prepare.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path


POLICY = "unsupported-align-language-skip-v1"
RECEIPT_NAME = "recovery-receipt.json"
RECEIPT_VERSION = "yuanying-recovery-v1"
SKIPPED_ROOT_FILES = {
    "preflight.json",
    "progress.json",
    "exit-receipt.json",
    "run.log",
    "run-identity.json",
}
PENDING_FILM_FILES = {
    "asr/protocol.json",
    "embed/protocol.json",
    "sample-reuse-audit.json",
}


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


batch = load("yuanying_recovery_batch", "yuanying-batch.py")
transcribe = load("yuanying_recovery_transcribe", "full-film-transcribe.py")


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


def copy_bytes(source, dest):
    """Write a new file. A hard link would let a later writer change the source."""
    source = Path(source)
    before = os.lstat(source)
    data = source.read_bytes()
    atomic_write(dest, data)
    after = os.lstat(source)
    copied = os.lstat(dest)
    if after.st_nlink != before.st_nlink:
        raise batch.BatchBlocked("copy changed the source link count", reason="preserved-artifact")
    if copied.st_dev == before.st_dev and copied.st_ino == before.st_ino:
        raise batch.BatchBlocked("recovery copy must not be a hard link", reason="preserved-artifact")
    return data


def relative_key(value):
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or path.parts == ():
        raise batch.BatchBlocked("snapshot path escapes the source", reason="source-mismatch")
    return path


def listed_files(root):
    found = {}
    for path in sorted(item for item in Path(root).rglob("*") if item.is_file()):
        found[str(path.relative_to(root))] = path
    return found


def load_snapshot(path, source):
    snapshot = read_json(path)
    if not isinstance(snapshot, dict):
        raise batch.BatchBlocked("snapshot must be an object", reason="source-mismatch")
    for key in ("createdAt", "fileCount", "totalBytes", "root", "files"):
        if key not in snapshot:
            raise batch.BatchBlocked("snapshot missing " + key, reason="source-mismatch")
    files = snapshot["files"]
    if not isinstance(files, dict):
        raise batch.BatchBlocked("snapshot files must be an object", reason="source-mismatch")
    if int(snapshot["fileCount"]) != len(files):
        raise batch.BatchBlocked("snapshot fileCount does not match", reason="source-mismatch")
    if batch.canonical_path(snapshot["root"]) != batch.canonical_path(source):
        raise batch.BatchBlocked("snapshot root is not the recovery source", reason="source-mismatch")
    present = listed_files(source)
    if set(present) != set(files):
        raise batch.BatchBlocked("snapshot file list does not match the source", reason="source-mismatch")
    total = 0
    checked = {}
    for key, meta in files.items():
        rel = relative_key(key)
        if not isinstance(meta, dict) or "sha256" not in meta or "size" not in meta:
            raise batch.BatchBlocked("snapshot entry is incomplete", reason="source-mismatch")
        path = present[str(rel)]
        size = int(meta["size"])
        digest = file_sha256(path)
        if path.stat().st_size != size or digest != meta["sha256"]:
            raise batch.BatchBlocked("snapshot hash does not match the source", reason="source-mismatch")
        total += size
        checked[str(rel)] = {"path": path, "sha256": digest, "size": size, "nlink": os.lstat(path).st_nlink}
    if total != int(snapshot["totalBytes"]):
        raise batch.BatchBlocked("snapshot totalBytes does not match", reason="source-mismatch")
    return snapshot, checked


def source_unchanged(checked):
    for item in checked.values():
        path = item["path"]
        if os.lstat(path).st_nlink != item["nlink"] or file_sha256(path) != item["sha256"]:
            raise batch.BatchBlocked("recovery changed the source", reason="preserved-artifact")


def film_groups(checked):
    groups = {}
    shared = []
    roots = []
    for rel in checked:
        parts = Path(rel).parts
        if parts[0] == "films":
            if len(parts) < 3:
                raise batch.BatchBlocked("film snapshot entry is incomplete", reason="source-mismatch")
            groups.setdefault(parts[1], []).append(rel)
        elif parts[0] == "shared-document-cache":
            shared.append(rel)
        elif len(parts) == 1:
            roots.append(rel)
        else:
            raise batch.BatchBlocked("unexpected recovery source path", reason="source-mismatch")
    return groups, shared, roots


def task_status_problem(stage, task):
    status = task.get("status")
    if status == "succeeded":
        return None
    if (
        stage == "align"
        and status == "failed"
        and task.get("reason") == "language-unsupported"
        and task.get("detected_language")
    ):
        return "language-unsupported"
    return status or "missing"


def require_output(checked, rel, task):
    if rel not in checked:
        raise batch.BatchBlocked("succeeded checkpoint has no output", reason="source-mismatch")
    expected = task.get("output_sha256")
    if not expected or checked[rel]["sha256"] != expected:
        raise batch.BatchBlocked("succeeded output hash does not match its checkpoint", reason="source-mismatch")


def classify_film(label, rels, source, checked):
    prefix = f"films/{label}/"
    local = {rel[len(prefix):] for rel in rels}
    asr_tasks_rel = prefix + "asr/asr-tasks.json"
    align_tasks_rel = prefix + "asr/align-tasks.json"
    has_tasks = any(name in local for name in (
        "asr/asr-tasks.json", "asr/align-tasks.json", "asr/prepare.json", "asr/chunks.json",
        "asr/documents-state.json", "asr/candidates.json",
    ))
    if not has_tasks:
        extra = local - PENDING_FILM_FILES
        if extra or "asr/protocol.json" not in local or "embed/protocol.json" not in local:
            raise batch.BatchBlocked("pending film has an unexpected file", reason="source-mismatch", label=label)
        return {"label": label, "kind": "pending", "failure": None}
    asr_tasks = read_json(source / asr_tasks_rel) if asr_tasks_rel in checked else []
    align_tasks = read_json(source / align_tasks_rel) if align_tasks_rel in checked else []
    if not isinstance(asr_tasks, list) or not isinstance(align_tasks, list):
        raise batch.BatchBlocked("task file is not a list", reason="transcription-failed", label=label)
    failure = None
    for stage, tasks in (("asr", asr_tasks), ("align", align_tasks)):
        for task in tasks:
            problem = task_status_problem(stage, task)
            if problem is None:
                output_rel = f"{prefix}asr/{stage}/{task.get('chunk_id')}.json"
                require_output(checked, output_rel, task)
                continue
            if problem != "language-unsupported" or failure is not None:
                raise batch.BatchBlocked("only one language-unsupported align failure can be derived", reason="transcription-failed", label=label)
            failure = task
    documents_rel = prefix + "asr/documents-state.json"
    if documents_rel in checked:
        state = read_json(source / documents_rel)
        if state.get("status") != "succeeded" or failure is not None:
            raise batch.BatchBlocked("documents checkpoint cannot be reused", reason="transcription-failed", label=label)
    if failure is not None:
        chunk_id = failure.get("chunk_id")
        align_output = f"{prefix}asr/align/{chunk_id}.json"
        if align_output in checked:
            raise batch.BatchBlocked("failed align chunk already has an output", reason="source-mismatch", label=label)
        if prefix + "asr/chunks.json" not in checked or prefix + "asr/prepare.json" not in checked:
            raise batch.BatchBlocked("language failure is missing its chunk list", reason="source-mismatch", label=label)
        prepare = read_json(source / (prefix + "asr/prepare.json"))
        if prepare.get("status") != "succeeded":
            raise batch.BatchBlocked("prepare was not successful", reason="transcription-failed", label=label)
    known_outputs = set()
    for stage, tasks in (("asr", asr_tasks), ("align", align_tasks)):
        for task in tasks:
            if task.get("status") == "succeeded":
                known_outputs.add(f"asr/{stage}/{task.get('chunk_id')}.json")
    for name in local:
        if name.startswith("asr/asr/") or name.startswith("asr/align/"):
            if name not in known_outputs:
                raise batch.BatchBlocked("orphan transcription output", reason="source-mismatch", label=label)
    return {
        "label": label,
        "kind": "failed" if failure is not None else "settled",
        "failure": failure,
        "documents_done": documents_rel in checked,
    }


def rebind_embed(body, output, label):
    rewritten = copy.deepcopy(body)
    rewritten["asr_run"] = str((Path(output) / "films" / label / "asr").resolve())
    rewritten["shared_document_cache"] = {
        "directory": str((Path(output) / "shared-document-cache").resolve()),
    }
    return rewritten


def with_policy(body):
    rewritten = copy.deepcopy(body)
    current = rewritten.get("unsupported_align_language_policy")
    if current not in (None, "", POLICY):
        raise batch.BatchBlocked("film already has a different skip policy", reason="language-policy")
    rewritten["unsupported_align_language_policy"] = POLICY
    return rewritten


def derive_skip(source, label, failure, protocol):
    asr_run = source / "films" / label / "asr"
    chunks = read_json(asr_run / "chunks.json")
    matches = [chunk for chunk in chunks if chunk.get("chunk_id") == failure.get("chunk_id")]
    if len(matches) != 1:
        raise batch.BatchBlocked("failed chunk is not in the chunk list", reason="source-mismatch", label=label)
    chunk = matches[0]
    text, asr_sha = transcribe.asr_text_for_chunk(asr_run, chunk)
    asr_record = read_json(transcribe.output_path(asr_run, "asr", chunk["chunk_id"]))
    _routed, error = transcribe.align_route(asr_record, transcribe.AUTO_LANGUAGE_POLICY)
    if error != "language-unsupported" or asr_record.get("language_detected") != failure.get("detected_language"):
        raise batch.BatchBlocked("failed chunk is not an aligner language gap", reason="transcription-failed", label=label)
    if not transcribe.substantive(text):
        raise batch.BatchBlocked("empty text cannot be derived as a language gap", reason="transcription-failed", label=label)
    identity = transcribe.align_identity(protocol, chunk, asr_sha, asr_record)
    if identity != failure.get("identity"):
        raise batch.BatchBlocked("failed checkpoint identity does not match the source", reason="source-mismatch", label=label)
    sample_count = int(chunk["end_sample"]) - int(chunk["start_sample"])
    derived_from = {
        "source": f"films/{label}/asr/align-tasks.json",
        "source_status": "failed",
        "source_reason": "language-unsupported",
    }
    destination = asr_run / "align" / f"{chunk['chunk_id']}.json"
    # Build the record without writing into the source tree.
    record = {
        "asr_raw_sha256": asr_sha,
        "input_text": text,
        "input_text_sha256": transcribe.text_sha256(text),
        "language": asr_record.get("language_detected"),
        "detected_language": asr_record.get("language_detected"),
        "dtype": "bfloat16",
        "backend": None,
        "audio_samples": sample_count,
        "audio_rate": transcribe.SAMPLE_RATE,
        "units": [],
        "parser_capture": None,
        "alignment_executed": False,
        "asr_inference_executed": False,
        "skipped": True,
        "skip_reason": "language-unsupported",
        "skip_policy": POLICY,
        "transcription_language_policy": transcribe.AUTO_LANGUAGE_POLICY,
        "language_granularity": "chunk",
        "mlx_peak_allocated_bytes": None,
        "derived_from": derived_from,
    }
    payload = (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    task = {
        "chunk_id": chunk["chunk_id"],
        "status": "skipped",
        "identity": identity,
        "reason": "language-unsupported",
        "skip_policy": POLICY,
        "alignment_executed": False,
        "detected_language": asr_record.get("language_detected"),
        "elapsed_seconds": 0,
        "peak_rss_bytes": None,
        "mlx_peak_allocated_bytes": None,
        "output_sha256": digest,
        "text_sha256": transcribe.text_sha256(text),
        "derived_from": derived_from,
    }
    tasks = read_json(asr_run / "align-tasks.json")
    replaced = 0
    rewritten_tasks = []
    for item in tasks:
        if item.get("chunk_id") == chunk["chunk_id"]:
            rewritten_tasks.append(task)
            replaced += 1
        else:
            rewritten_tasks.append(item)
    if replaced != 1:
        raise batch.BatchBlocked("failed align task was not unique", reason="source-mismatch", label=label)
    tasks_payload = (json.dumps(rewritten_tasks, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    return {
        "align_rel": f"films/{label}/asr/align/{chunk['chunk_id']}.json",
        "tasks_rel": f"films/{label}/asr/align-tasks.json",
        "align_bytes": payload,
        "tasks_bytes": tasks_payload,
        "chunk_id": chunk["chunk_id"],
        "detected_language": asr_record.get("language_detected"),
        "input_text_sha256": transcribe.text_sha256(text),
    }


def plan_recovery(source, checked, output):
    groups, shared, roots = film_groups(checked)
    if "manifest.json" not in roots:
        raise batch.BatchBlocked("snapshot has no manifest", reason="source-mismatch")
    unexpected = [name for name in roots if name not in SKIPPED_ROOT_FILES and name != "manifest.json"]
    if unexpected:
        raise batch.BatchBlocked("unexpected recovery source file", reason="source-mismatch")
    films = [classify_film(label, rels, source, checked) for label, rels in sorted(groups.items())]
    failures = [film for film in films if film["failure"] is not None]
    if len(failures) != 1:
        raise batch.BatchBlocked("recovery requires exactly one language-unsupported failure", reason="transcription-failed")
    failure = failures[0]
    protocol = with_policy(read_json(source / "films" / failure["label"] / "asr" / "protocol.json"))
    if protocol.get("transcription_language_policy") != batch.LANGUAGE_POLICY:
        raise batch.BatchBlocked("recovery source is not auto-v1", reason="language-policy")
    derived = derive_skip(source, failure["label"], failure["failure"], protocol)
    copies = []
    rewrites = []
    for rel, item in sorted(checked.items()):
        if Path(rel).name and rel in SKIPPED_ROOT_FILES:
            continue
        if rel == derived["tasks_rel"]:
            continue
        if rel == "manifest.json":
            continue
        film_label = Path(rel).parts[1] if rel.startswith("films/") else None
        film = next((row for row in films if row["label"] == film_label), None)
        if rel.endswith("/asr/protocol.json") and film is not None and not film.get("documents_done"):
            rewrites.append({
                "path": rel,
                "action": "rebound",
                "source": rel,
                "data": (json.dumps(with_policy(read_json(item["path"])), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            })
            continue
        if rel.endswith("/embed/protocol.json") and film is not None:
            rewrites.append({
                "path": rel,
                "action": "rebound",
                "source": rel,
                "data": (json.dumps(rebind_embed(read_json(item["path"]), output, film_label), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            })
            continue
        copies.append({"path": rel, "action": "copied", "source": rel})
    manifest = read_json(source / "manifest.json")
    manifest = copy.deepcopy(manifest)
    manifest["unsupportedAlignLanguagePolicy"] = POLICY
    rewrites.append({
        "path": "manifest.json",
        "action": "rebound",
        "source": "manifest.json",
        "data": (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    })
    rewrites.append({
        "path": derived["align_rel"],
        "action": "derived",
        "source": None,
        "data": derived["align_bytes"],
    })
    rewrites.append({
        "path": derived["tasks_rel"],
        "action": "derived",
        "source": derived["tasks_rel"],
        "data": derived["tasks_bytes"],
    })
    omitted = sorted(name for name in roots if name in SKIPPED_ROOT_FILES)
    return {"copies": copies, "rewrites": rewrites, "derived": derived, "omittedSourceFiles": omitted}


def apply_plan(output, checked, plan):
    written = []
    (output / "shared-document-cache").mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in plan["copies"]:
        dest = output / item["path"]
        data = copy_bytes(checked[item["source"]]["path"], dest)
        written.append({
            "path": item["path"],
            "sha256": hashlib.sha256(data).hexdigest(),
            "action": "copied",
            "source": item["source"],
            "sourceSha256": checked[item["source"]]["sha256"],
        })
    for item in plan["rewrites"]:
        dest = output / item["path"]
        atomic_write(dest, item["data"])
        source_rel = item["source"]
        if source_rel is not None:
            source_path = checked[source_rel]["path"]
            if os.lstat(dest).st_ino == os.lstat(source_path).st_ino and os.lstat(dest).st_dev == os.lstat(source_path).st_dev:
                raise batch.BatchBlocked("recovery rewrite must not be a hard link", reason="preserved-artifact")
        written.append({
            "path": item["path"],
            "sha256": hashlib.sha256(item["data"]).hexdigest(),
            "action": item["action"],
            "source": source_rel,
            "sourceSha256": None if source_rel is None else checked[source_rel]["sha256"],
        })
    return written


def receipt_body(config, snapshot_path, snapshot, plan, written):
    derived = plan["derived"]
    return {
        "version": RECEIPT_VERSION,
        "policy": POLICY,
        "source": batch.canonical_path(config["recoverySource"]),
        "snapshot": batch.canonical_path(snapshot_path),
        "snapshotSha256": file_sha256(snapshot_path),
        "snapshotCreatedAt": snapshot.get("createdAt"),
        "snapshotFileCount": int(snapshot["fileCount"]),
        "outputFileCount": len(written),
        "omittedSourceFiles": plan["omittedSourceFiles"],
        "hardlink": False,
        "sourceUnchanged": True,
        "derivedSkips": [{
            "label": derived["tasks_rel"].split("/")[1],
            "chunk_id": derived["chunk_id"],
            "reason": "language-unsupported",
            "detected_language": derived["detected_language"],
            "skip_policy": POLICY,
            "alignment_executed": False,
            "text_retained": True,
            "input_text_sha256": derived["input_text_sha256"],
        }],
        "files": written,
    }


def confirm_idempotent(output, checked):
    receipt_path = Path(output) / RECEIPT_NAME
    if not receipt_path.is_file():
        raise batch.BatchBlocked("recovery output exists without a receipt", reason="output-exists")
    receipt = read_json(receipt_path)
    if receipt.get("version") != RECEIPT_VERSION or receipt.get("policy") != POLICY or receipt.get("hardlink") is not False:
        raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
    expected = {item["path"]: item for item in receipt.get("files") or []}
    present = listed_files(output)
    present.pop(RECEIPT_NAME, None)
    if set(present) != set(expected):
        raise batch.BatchBlocked("recovery output no longer matches the receipt", reason="output-exists")
    for rel, item in expected.items():
        if file_sha256(present[rel]) != item.get("sha256"):
            raise batch.BatchBlocked("recovery output no longer matches the receipt", reason="output-exists")
    source_unchanged(checked)
    return receipt


def prepare(config):
    config = batch.require_config(dict(config))
    if config.get("unsupportedAlignLanguagePolicy") != POLICY:
        raise batch.BatchBlocked("recovery requires the explicit skip policy", reason="language-policy")
    for key in ("recoverySource", "recoverySnapshot"):
        if not config.get(key):
            raise batch.BatchBlocked("config missing " + key, reason="config")
    source = Path(config["recoverySource"]).resolve()
    output = Path(config["outputRoot"]).resolve()
    snapshot_path = Path(config["recoverySnapshot"]).resolve()
    if not source.is_dir() or not snapshot_path.is_file():
        raise batch.BatchBlocked("recovery source or snapshot is missing", reason="config")
    if batch.is_inside(output, source) or batch.is_inside(source, output):
        raise batch.BatchBlocked("recovery output overlaps the source", reason="preserved-artifact")
    snapshot, checked = load_snapshot(snapshot_path, source)
    if output.exists():
        if not output.is_dir():
            raise batch.BatchBlocked("recovery output exists", reason="output-exists")
        receipt = confirm_idempotent(output, checked)
        source_unchanged(checked)
        return receipt
    plan = plan_recovery(source, checked, output)
    output.mkdir(mode=0o700)
    try:
        written = apply_plan(output, checked, plan)
        source_unchanged(checked)
        body = receipt_body(config, snapshot_path, snapshot, plan, written)
        payload = (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        atomic_write(output / RECEIPT_NAME, payload)
    except Exception:
        source_unchanged(checked)
        raise
    source_unchanged(checked)
    return read_json(output / RECEIPT_NAME)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="把已完成的批次复制到新目录，并派生一处语言缺口。不推理。")
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = batch.load_config(args.config)
        receipt = prepare(config)
    except batch.BatchBlocked as exc:
        print(json.dumps({"ready": False, "reason": exc.reason, "label": exc.label}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({
        "ready": True,
        "version": receipt["version"],
        "policy": receipt["policy"],
        "outputFileCount": receipt["outputFileCount"],
        "hardlink": receipt["hardlink"],
        "derivedSkips": receipt["derivedSkips"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
