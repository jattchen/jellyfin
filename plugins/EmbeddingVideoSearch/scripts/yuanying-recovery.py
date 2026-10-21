#!/usr/bin/env python3
"""Copy one finished batch into a new directory.

The source stays read only. Each reused file is read in blocks and written to
a new inode, so the destination may be on another volume. A later write in the
new directory cannot change the source.

Absolute paths stored in the old tree are handled one class at a time:
- Rebind a chunk wav_path to the copied wav in the new tree only when that
  file is in the snapshot and its sha256 equals wav_sha256. The stored
  location is not opened.
- Rebind embed asr_run, and only the directory field of shared_document_cache.
- Keep media paths, model paths, worker_python, a relative full_wav, and the
  raw asr/align JSON bytes.
- Omit preflight, progress, the old recovery receipt, exit receipt, run log,
  and run identity. They are not copied.

A settled language skip is copied as-is. When alignmentTimePolicy is
last-unit-tail-80ms-v1, exactly one validation:last-unit-out-of-bounds failure
is revalidated offline in the new directory. Any other failure refuses the
whole prepare.

storageInterruptionRecovery is a separate explicit mode. It copies a tree
whose alignment is already finished, checks every succeeded joint, and leaves
out one named inflight dispatch so the normal embed entry can send it once.
It does not invent an align failure, and it does not run a model.

videoTailRecovery is another explicit mode. It checks the same succeeded
responses, and leaves out one never-dispatched last-window partial so the new
tree can cut that window again under video-tail-hold-last-frame-200ms-v1.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path


POLICY = "unsupported-align-language-skip-v1"
RECEIPT_NAME = "recovery-receipt.json"
RECEIPT_VERSION = "yuanying-recovery-v1"
COPY_BLOCK = 8 * 1024 * 1024
SKIPPED_ROOT_FILES = {
    "preflight.json",
    "progress.json",
    "exit-receipt.json",
    "run.log",
    "run-identity.json",
    "recovery-receipt.json",
}
PENDING_FILM_FILES = {
    "asr/protocol.json",
    "embed/protocol.json",
    "sample-reuse-audit.json",
}
TAIL_REASON = "validation:last-unit-out-of-bounds"
INTERRUPTION_MODE = "storage-interruption-v1"
INTERRUPTION_FIELDS = (
    "version",
    "film",
    "candidate",
    "inputHash",
    "stateSha256",
    "snapshotSha256",
)
VIDEO_TAIL_MODE = "video-tail-recovery-v1"
VIDEO_TAIL_FIELDS = (
    "version",
    "film",
    "candidate",
    "partialSha256",
    "snapshotSha256",
)


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
        for block in iter(lambda: handle.read(COPY_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def write_all(descriptor, payload):
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise batch.BatchBlocked("recovery write was incomplete", reason="source-mismatch")
        view = view[written:]


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        write_all(descriptor, data)
    finally:
        os.close(descriptor)


def copy_bytes(source, dest):
    """Write a new file from a block stream. The destination may be another volume."""
    source = Path(source)
    dest = Path(dest)
    before = os.lstat(source)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise batch.BatchBlocked("recovery source file is not a regular file", reason="source-mismatch")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    source_fd = os.open(source, source_flags)
    descriptor = os.open(dest, flags, 0o600)
    try:
        while True:
            block = os.read(source_fd, COPY_BLOCK)
            if not block:
                break
            digest.update(block)
            write_all(descriptor, block)
    finally:
        os.close(source_fd)
        os.close(descriptor)
    after = os.lstat(source)
    copied = os.lstat(dest)
    if stat.S_ISLNK(copied.st_mode) or not stat.S_ISREG(copied.st_mode):
        raise batch.BatchBlocked("recovery copy is not a regular file", reason="preserved-artifact")
    if after.st_nlink != before.st_nlink:
        raise batch.BatchBlocked("copy changed the source link count", reason="preserved-artifact")
    if copied.st_dev == before.st_dev and copied.st_ino == before.st_ino:
        raise batch.BatchBlocked("recovery copy must not be a hard link", reason="preserved-artifact")
    if copied.st_size != before.st_size:
        raise batch.BatchBlocked("recovery copy size does not match the source", reason="source-mismatch")
    return digest.hexdigest()


def relative_key(value):
    path = Path(str(value))
    if path.is_absolute() or ".." in path.parts or path.parts == ():
        raise batch.BatchBlocked("snapshot path escapes the source", reason="source-mismatch")
    return path


def listed_files(root):
    found = {}
    for path in sorted(Path(root).rglob("*")):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise batch.BatchBlocked("recovery source contains a symlink", reason="source-mismatch")
        if stat.S_ISREG(info.st_mode):
            found[str(path.relative_to(root))] = path
        elif not stat.S_ISDIR(info.st_mode):
            raise batch.BatchBlocked("recovery source contains a non-regular file", reason="source-mismatch")
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
        file_path = present[str(rel)]
        info = os.lstat(file_path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise batch.BatchBlocked("recovery source file is not a regular file", reason="source-mismatch")
        size = int(meta["size"])
        digest = file_sha256(file_path)
        if info.st_size != size or digest != meta["sha256"]:
            raise batch.BatchBlocked("snapshot hash does not match the source", reason="source-mismatch")
        total += size
        checked[str(rel)] = {"path": file_path, "sha256": digest, "size": size, "nlink": info.st_nlink}
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
        and status == "skipped"
        and task.get("reason") == "language-unsupported"
        and task.get("skip_policy") == POLICY
        and task.get("alignment_executed") is False
    ):
        return "settled-skip"
    if (
        stage == "align"
        and status == "failed"
        and task.get("reason") == "language-unsupported"
        and task.get("detected_language")
    ):
        return "language-unsupported"
    if stage == "align" and status == "failed" and task.get("reason") == TAIL_REASON:
        return "tail-boundary"
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
        return {"label": label, "kind": "pending", "failure": None, "tail": None}
    asr_tasks = read_json(source / asr_tasks_rel) if asr_tasks_rel in checked else []
    align_tasks = read_json(source / align_tasks_rel) if align_tasks_rel in checked else []
    if not isinstance(asr_tasks, list) or not isinstance(align_tasks, list):
        raise batch.BatchBlocked("task file is not a list", reason="transcription-failed", label=label)
    failure = None
    tail = None
    for stage, tasks in (("asr", asr_tasks), ("align", align_tasks)):
        for task in tasks:
            problem = task_status_problem(stage, task)
            output_rel = f"{prefix}asr/{stage}/{task.get('chunk_id')}.json"
            if problem is None or problem == "settled-skip":
                require_output(checked, output_rel, task)
                continue
            if problem == "tail-boundary":
                if tail is not None or failure is not None:
                    raise batch.BatchBlocked(
                        "only one last-unit tail failure can be derived",
                        reason="transcription-failed", label=label)
                require_output(checked, output_rel, task)
                tail = task
                continue
            if problem == "language-unsupported" and failure is None and tail is None:
                failure = task
                continue
            raise batch.BatchBlocked(
                "only one language-unsupported align failure can be derived",
                reason="transcription-failed", label=label)
    documents_rel = prefix + "asr/documents-state.json"
    if documents_rel in checked:
        state = read_json(source / documents_rel)
        if state.get("status") != "succeeded" or failure is not None or tail is not None:
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
    if tail is not None:
        if prefix + "asr/chunks.json" not in checked or prefix + "asr/prepare.json" not in checked:
            raise batch.BatchBlocked("tail failure is missing its chunk list", reason="source-mismatch", label=label)
        prepare = read_json(source / (prefix + "asr/prepare.json"))
        if prepare.get("status") != "succeeded":
            raise batch.BatchBlocked("prepare was not successful", reason="transcription-failed", label=label)
    known_outputs = set()
    for stage, tasks in (("asr", asr_tasks), ("align", align_tasks)):
        for task in tasks:
            problem = task_status_problem(stage, task)
            if problem in (None, "settled-skip", "tail-boundary"):
                known_outputs.add(f"asr/{stage}/{task.get('chunk_id')}.json")
    for name in local:
        if name.startswith("asr/asr/") or name.startswith("asr/align/"):
            if name not in known_outputs:
                raise batch.BatchBlocked("orphan transcription output", reason="source-mismatch", label=label)
    if failure is not None:
        kind = "failed"
    elif tail is not None:
        kind = "tail"
    elif documents_rel in checked:
        kind = "settled"
    else:
        kind = "open"
    return {
        "label": label,
        "kind": kind,
        "failure": failure,
        "tail": tail,
        "documents_done": documents_rel in checked,
    }


def rebind_embed(body, output, label):
    rewritten = copy.deepcopy(body)
    rewritten["asr_run"] = str((Path(output) / "films" / label / "asr").resolve())
    cache = rewritten.get("shared_document_cache")
    if not isinstance(cache, dict):
        cache = {}
    cache = copy.deepcopy(cache)
    cache["directory"] = str((Path(output) / "shared-document-cache").resolve())
    rewritten["shared_document_cache"] = cache
    return rewritten


def stamp_video_tail(body, output, label):
    rewritten = rebind_embed(body, output, label)
    rewritten["video_tail_policy"] = batch.contract.VIDEO_TAIL_POLICY
    return rewritten


def with_policy(body):
    rewritten = copy.deepcopy(body)
    current = rewritten.get("unsupported_align_language_policy")
    if current not in (None, "", POLICY):
        raise batch.BatchBlocked("film already has a different skip policy", reason="language-policy")
    rewritten["unsupported_align_language_policy"] = POLICY
    return rewritten


def with_tail(body):
    rewritten = copy.deepcopy(body)
    current = rewritten.get("alignment_time_policy")
    if current not in (None, "", transcribe.TAIL_TIME_POLICY):
        raise batch.BatchBlocked("film already has a different alignment time policy", reason="language-policy")
    rewritten["alignment_time_policy"] = transcribe.TAIL_TIME_POLICY
    return rewritten


def rebind_chunks(chunks, output, checked, label):
    """Point wav_path at the new copy. Do not follow the stored absolute path."""
    if not isinstance(chunks, list):
        raise batch.BatchBlocked("chunk list is not a list", reason="source-mismatch", label=label)
    rewritten = copy.deepcopy(chunks)
    changed = False
    for chunk in rewritten:
        if "wav_path" not in chunk:
            continue
        chunk_id = chunk.get("chunk_id")
        if not isinstance(chunk_id, str) or chunk_id in ("", ".", "..") or "/" in chunk_id or "\\" in chunk_id:
            raise batch.BatchBlocked("chunk id is not a file name", reason="source-mismatch", label=label)
        wav_rel = f"films/{label}/asr/audio/chunks/{chunk_id}.wav"
        item = checked.get(wav_rel)
        expected = chunk.get("wav_sha256")
        if item is None or not expected or item["sha256"] != expected:
            raise batch.BatchBlocked("in-tree wav does not match wav_sha256", reason="source-mismatch", label=label)
        new_path = str((Path(output) / wav_rel).resolve())
        if chunk.get("wav_path") != new_path:
            chunk["wav_path"] = new_path
            changed = True
    return rewritten, changed


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
    payload = json_bytes(record)
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
    tasks_payload = json_bytes(rewritten_tasks)
    return {
        "align_rel": f"films/{label}/asr/align/{chunk['chunk_id']}.json",
        "tasks_rel": f"films/{label}/asr/align-tasks.json",
        "align_bytes": payload,
        "tasks_bytes": tasks_payload,
        "chunk_id": chunk["chunk_id"],
        "detected_language": asr_record.get("language_detected"),
        "input_text_sha256": transcribe.text_sha256(text),
    }


def prove_tail(source, label, failure, checked):
    """Reject a tail that is outside the 80ms last-unit rule before creating output."""
    asr_run = source / "films" / label / "asr"
    try:
        protocol = with_tail(with_policy(read_json(asr_run / "protocol.json")))
    except batch.BatchBlocked:
        raise
    if protocol.get("transcription_language_policy") != batch.LANGUAGE_POLICY:
        raise batch.BatchBlocked("recovery source is not auto-v1", reason="language-policy", label=label)
    chunks = read_json(asr_run / "chunks.json")
    matches = [chunk for chunk in chunks if chunk.get("chunk_id") == failure.get("chunk_id")]
    if len(matches) != 1:
        raise batch.BatchBlocked("failed chunk is not in the chunk list", reason="source-mismatch", label=label)
    chunk = matches[0]
    try:
        text, asr_sha = transcribe.asr_text_for_chunk(asr_run, chunk)
    except transcribe.StageBlocked as exc:
        raise batch.BatchBlocked(str(exc), reason="transcription-failed", label=label) from exc
    asr_record = read_json(transcribe.output_path(asr_run, "asr", chunk["chunk_id"]))
    routed, error = transcribe.align_route(asr_record, transcribe.AUTO_LANGUAGE_POLICY)
    if error:
        raise batch.BatchBlocked("tail failure is not an alignable language", reason="transcription-failed", label=label)
    identity = transcribe.align_identity(protocol, chunk, asr_sha, asr_record)
    if identity != failure.get("identity"):
        raise batch.BatchBlocked("failed checkpoint identity does not match the source", reason="source-mismatch", label=label)
    align_rel = f"films/{label}/asr/align/{chunk['chunk_id']}.json"
    tasks_rel = f"films/{label}/asr/align-tasks.json"
    if align_rel not in checked or checked[align_rel]["sha256"] != failure.get("output_sha256"):
        raise batch.BatchBlocked("tail align output hash does not match its checkpoint", reason="source-mismatch", label=label)
    if tasks_rel not in checked:
        raise batch.BatchBlocked("tail failure is missing its task ledger", reason="source-mismatch", label=label)
    record = read_json(source / align_rel)
    sample_count = int(chunk["end_sample"]) - int(chunk["start_sample"])
    duration = float(chunk["duration_seconds"])
    problems = transcribe.parse_align_record(
        record, text, sample_count, duration, routed, transcribe.AUTO_LANGUAGE_POLICY)
    if problems != ["last-unit-out-of-bounds"]:
        raise batch.BatchBlocked(
            "tail failure is not only a last-unit boundary",
            reason="transcription-failed", label=label)
    try:
        _view, receipt = transcribe.alignment_view(
            record, protocol, text, sample_count, duration, routed)
    except transcribe.StageBlocked as exc:
        raise batch.BatchBlocked(str(exc), reason="transcription-failed", label=label) from exc
    if receipt is None:
        raise batch.BatchBlocked(
            "tail failure is outside the 80ms last-unit rule",
            reason="transcription-failed", label=label)
    return {
        "label": label,
        "chunk_id": chunk["chunk_id"],
        "tasks_rel": tasks_rel,
        "align_tasks_sha256": checked[tasks_rel]["sha256"],
        "end_retracted_ticks": receipt["end_retracted_ticks"],
    }


def plan_recovery(source, checked, output, *, tail_policy=False):
    groups, _shared, roots = film_groups(checked)
    if "manifest.json" not in roots:
        raise batch.BatchBlocked("snapshot has no manifest", reason="source-mismatch")
    unexpected = [name for name in roots if name not in SKIPPED_ROOT_FILES and name != "manifest.json"]
    if unexpected:
        raise batch.BatchBlocked("unexpected recovery source file", reason="source-mismatch")
    films = [classify_film(label, rels, source, checked) for label, rels in sorted(groups.items())]
    failures = [film for film in films if film.get("failure") is not None]
    tails = [film for film in films if film.get("tail") is not None]
    if tail_policy:
        if len(tails) != 1:
            raise batch.BatchBlocked(
                "recovery requires exactly one last-unit tail failure",
                reason="transcription-failed")
        if len(failures) > 1:
            raise batch.BatchBlocked(
                "recovery requires exactly one language-unsupported failure",
                reason="transcription-failed")
    else:
        if tails:
            raise batch.BatchBlocked(
                "last-unit tail failure requires the explicit alignment time policy",
                reason="transcription-failed")
        if len(failures) != 1:
            raise batch.BatchBlocked(
                "recovery requires exactly one language-unsupported failure",
                reason="transcription-failed")
    proved = prove_tail(source, tails[0]["label"], tails[0]["tail"], checked) if tail_policy else None
    derived = None
    if failures:
        failure = failures[0]
        protocol = with_policy(read_json(source / "films" / failure["label"] / "asr" / "protocol.json"))
        if tail_policy:
            protocol = with_tail(protocol)
        if protocol.get("transcription_language_policy") != batch.LANGUAGE_POLICY:
            raise batch.BatchBlocked("recovery source is not auto-v1", reason="language-policy")
        derived = derive_skip(source, failure["label"], failure["failure"], protocol)
    copies = []
    rewrites = []
    for rel, item in sorted(checked.items()):
        if rel in SKIPPED_ROOT_FILES or rel == "manifest.json":
            continue
        if derived is not None and rel == derived["tasks_rel"]:
            continue
        film_label = Path(rel).parts[1] if rel.startswith("films/") else None
        film = next((row for row in films if row["label"] == film_label), None)
        if rel.endswith("/asr/chunks.json") and film is not None:
            rewritten, changed = rebind_chunks(read_json(item["path"]), output, checked, film_label)
            if changed:
                rewrites.append({
                    "path": rel,
                    "action": "rebound",
                    "source": rel,
                    "data": json_bytes(rewritten),
                })
                continue
        if rel.endswith("/asr/protocol.json") and film is not None and not film.get("documents_done"):
            body = with_policy(read_json(item["path"]))
            if tail_policy:
                body = with_tail(body)
            if proved is not None and film_label == proved["label"]:
                body["recovery_origin"] = {
                    "run": str((Path(source) / "films" / film_label / "asr").resolve()),
                    "align_tasks_sha256": proved["align_tasks_sha256"],
                }
            rewrites.append({
                "path": rel,
                "action": "rebound",
                "source": rel,
                "data": json_bytes(body),
            })
            continue
        if rel.endswith("/embed/protocol.json") and film is not None:
            rewrites.append({
                "path": rel,
                "action": "rebound",
                "source": rel,
                "data": json_bytes(rebind_embed(read_json(item["path"]), output, film_label)),
            })
            continue
        copies.append({"path": rel, "action": "copied", "source": rel})
    manifest = copy.deepcopy(read_json(source / "manifest.json"))
    manifest["unsupportedAlignLanguagePolicy"] = POLICY
    if tail_policy:
        manifest["alignmentTimePolicy"] = transcribe.TAIL_TIME_POLICY
    rewrites.append({
        "path": "manifest.json",
        "action": "rebound",
        "source": "manifest.json",
        "data": json_bytes(manifest),
    })
    if derived is not None:
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
    return {
        "copies": copies,
        "rewrites": rewrites,
        "derived": derived,
        "tail": proved,
        "derivedTails": [],
        "omittedSourceFiles": omitted,
    }


def apply_plan(output, checked, plan):
    written = []
    (output / "shared-document-cache").mkdir(parents=True, exist_ok=True, mode=0o700)
    for item in plan["copies"]:
        dest = output / item["path"]
        source_meta = checked[item["source"]]
        digest = copy_bytes(source_meta["path"], dest)
        if digest != source_meta["sha256"]:
            raise batch.BatchBlocked("recovery copy does not match the snapshot", reason="source-mismatch")
        written.append({
            "path": item["path"],
            "sha256": digest,
            "action": "copied",
            "source": item["source"],
            "sourceSha256": source_meta["sha256"],
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


def refresh_tail(output, plan, written):
    tail = plan["tail"]
    label = tail["label"]
    asr_run = Path(output) / "films" / label / "asr"
    try:
        transcribe.stage_revalidate_tail(asr_run)
    except transcribe.StageBlocked as exc:
        raise batch.BatchBlocked(str(exc), reason="transcription-failed", label=label) from exc
    tasks_rel = tail["tasks_rel"]
    tasks_path = Path(output) / tasks_rel
    matched = False
    for item in written:
        if item["path"] == tasks_rel:
            item["sha256"] = file_sha256(tasks_path)
            item["action"] = "derived"
            matched = True
            break
    if not matched:
        raise batch.BatchBlocked("tail task ledger was not copied", reason="source-mismatch", label=label)
    extra_rel = f"films/{label}/asr/tail-revalidation.json"
    extra_path = Path(output) / extra_rel
    if not extra_path.is_file():
        raise batch.BatchBlocked("tail revalidation receipt is missing", reason="transcription-failed", label=label)
    written.append({
        "path": extra_rel,
        "sha256": file_sha256(extra_path),
        "action": "derived",
        "source": None,
        "sourceSha256": None,
    })
    tasks = read_json(tasks_path)
    task = next((item for item in tasks if item.get("chunk_id") == tail["chunk_id"]), None)
    if task is None or task.get("status") != "succeeded" or task.get("reason") != "derived-tail-boundary-validated":
        raise batch.BatchBlocked("tail revalidation did not derive the failed chunk", reason="transcription-failed", label=label)
    derivation = task.get("time_boundary_derivation") or {}
    public = {
        "label": label,
        "chunk_id": tail["chunk_id"],
        "reason": task["reason"],
        "new_model_inputs": 0,
    }
    for key in (
        "version", "unit_index", "end_retracted_ticks", "raw_validation_failure",
        "raw_preserved", "transcription_accuracy_verified",
    ):
        if key not in derivation:
            raise batch.BatchBlocked("tail derivation receipt is incomplete", reason="transcription-failed", label=label)
        public[key] = derivation[key]
    if "text" in public:
        raise batch.BatchBlocked("public tail receipt must not include original text", reason="preserved-artifact", label=label)
    plan["derivedTails"] = [public]
    return written


def hex64(value):
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def film_label(value):
    if not isinstance(value, str) or len(value) != 4 or not value.startswith("F") or not value[1:].isdigit():
        raise batch.BatchBlocked("interruption film must look like F011", reason="config")
    return value


def safe_file_name(value):
    if not isinstance(value, str) or value in ("", ".", "..") or "/" in value or "\\" in value:
        raise batch.BatchBlocked("interruption candidate is not a file name", reason="config")
    return value


def configured_interruption(config):
    """Return the explicit whitelist, or None when this prepare is not that mode."""
    value = config.get("storageInterruptionRecovery", None)
    if value in (None, ""):
        return None
    if not isinstance(value, dict) or set(value) != set(INTERRUPTION_FIELDS):
        raise batch.BatchBlocked("storage interruption authorization is incomplete", reason="config")
    if value.get("version") != INTERRUPTION_MODE:
        raise batch.BatchBlocked("unknown storage interruption mode", reason="config")
    film_label(value.get("film"))
    safe_file_name(value.get("candidate"))
    for key in ("inputHash", "stateSha256", "snapshotSha256"):
        if not hex64(value.get(key)):
            raise batch.BatchBlocked("storage interruption hash is not 64 hex characters", reason="config")
    return {key: value[key] for key in INTERRUPTION_FIELDS}


def configured_video_tail(config):
    """Return the explicit undispatched-partial whitelist, or None."""
    value = config.get("videoTailRecovery", None)
    if value in (None, ""):
        return None
    if not isinstance(value, dict) or set(value) != set(VIDEO_TAIL_FIELDS):
        raise batch.BatchBlocked("video tail recovery authorization is incomplete", reason="config")
    if value.get("version") != VIDEO_TAIL_MODE:
        raise batch.BatchBlocked("unknown video tail recovery mode", reason="config")
    film = value.get("film")
    if not isinstance(film, str) or len(film) != 4 or not film.startswith("F") or not film[1:].isdigit():
        raise batch.BatchBlocked("video tail film must look like F020", reason="config")
    candidate = value.get("candidate")
    if not isinstance(candidate, str) or candidate in ("", ".", "..") or "/" in candidate or "\\" in candidate:
        raise batch.BatchBlocked("video tail candidate is not a file name", reason="config")
    for key in ("partialSha256", "snapshotSha256"):
        if not hex64(value.get(key)):
            raise batch.BatchBlocked("video tail hash is not 64 hex characters", reason="config")
    return {key: value[key] for key in VIDEO_TAIL_FIELDS}


def load_stored_json(path, label=None):
    try:
        return read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise batch.BatchBlocked("stored record cannot be read", reason="source-mismatch", label=label) from exc


def require_embedding(response, signature, kind, window, label):
    try:
        batch.embed.validate_response(response, signature, kind, window)
    except AssertionError as exc:
        raise batch.BatchBlocked(
            "stored embedding does not match the model signature",
            reason="signature-mismatch", label=label,
        ) from exc


def embedding_digest(protocol, kind, entry, clip_sha, label):
    try:
        digest, window = batch.embed.embedding_input(protocol, kind, entry, clip_sha)
    except AssertionError as exc:
        raise batch.BatchBlocked(
            "stored embedding input cannot be checked",
            reason="cache-hash-mismatch", label=label,
        ) from exc
    return digest, window


def require_window(protocol, label):
    try:
        window = batch.embed.active_ten_second(protocol)
    except AssertionError as exc:
        raise batch.BatchBlocked(
            "film window is not the 12-second contract",
            reason="window-mismatch", label=label,
        ) from exc
    if window is None or window.get("version") != batch.contract.VERSION:
        raise batch.BatchBlocked(
            "film window is not the 12-second contract",
            reason="window-mismatch", label=label,
        )
    return window


def require_signature(protocol, expected, label):
    found = protocol.get("signature")
    if not isinstance(found, dict):
        raise batch.BatchBlocked("film signature is missing", reason="signature-mismatch", label=label)
    for key in ("id", "revision", "processing_id", "dimension", "normalized"):
        if found.get(key) != expected.get(key):
            raise batch.BatchBlocked("film signature does not match the frozen model", reason="signature-mismatch", label=label)
    return found


def clip_rels(label, candidate):
    base = f"films/{label}/embed/clips/{candidate}"
    return base + ".mp4", base + ".clip.json", base + ".partial.mp4"


def same_clip_sha(checked, clip_rel, meta_rel, claimed, label):
    if clip_rel not in checked or meta_rel not in checked:
        raise batch.BatchBlocked("joint clip is missing", reason="source-mismatch", label=label)
    meta = load_stored_json(checked[meta_rel]["path"], label)
    file_sha = checked[clip_rel]["sha256"]
    if not claimed or claimed != file_sha or meta.get("sha256") != file_sha:
        raise batch.BatchBlocked("joint clip hash does not match", reason="cache-hash-mismatch", label=label)
    return file_sha


def audit_shared_documents(checked, signature, window):
    prefix = "shared-document-cache/"
    rels = [rel for rel in checked if rel.startswith(prefix)]
    states, responses = [], []
    for rel in rels:
        name = rel[len(prefix):]
        if "/" in name:
            raise batch.BatchBlocked("shared document cache has an unexpected file", reason="source-mismatch")
        if name.endswith(".state.json"):
            states.append(rel)
        elif name.endswith(".json"):
            responses.append(rel)
        else:
            raise batch.BatchBlocked("shared document cache has an unexpected file", reason="source-mismatch")
    if len(states) != len(responses):
        raise batch.BatchBlocked("shared document cache has an orphan file", reason="source-mismatch")
    reused = 0
    for rel in states:
        stem = Path(rel).name[: -len(".state.json")]
        response_rel = prefix + stem + ".json"
        if not hex64(stem) or response_rel not in checked:
            raise batch.BatchBlocked("shared document cache has an orphan file", reason="source-mismatch")
        state = load_stored_json(checked[rel]["path"])
        if not isinstance(state, dict) or state.get("status") != "succeeded" or state.get("kind") != "document":
            raise batch.BatchBlocked("unresolved shared document is not the authorized joint", reason="not-succeeded")
        if state.get("input_hash") != stem or state.get("response_sha256") != checked[response_rel]["sha256"]:
            raise batch.BatchBlocked("shared document hash does not match", reason="cache-hash-mismatch")
        require_embedding(load_stored_json(checked[response_rel]["path"]), signature, "text", window, None)
        reused += 1
    return reused


def audit_text(checked, label, protocol, signature, window):
    prefix = f"films/{label}/embed/text/"
    states = {}
    responses = set()
    for rel in checked:
        if not rel.startswith(prefix):
            continue
        name = rel[len(prefix):]
        if "/" in name:
            raise batch.BatchBlocked("text embedding has an unexpected file", reason="source-mismatch", label=label)
        if name.endswith(".state.json"):
            states[name[: -len(".state.json")]] = rel
        elif name.endswith(".json"):
            responses.add(name[:-5])
        else:
            raise batch.BatchBlocked("text embedding has an unexpected file", reason="source-mismatch", label=label)
    if not states and not responses:
        return 0
    candidates_rel = f"films/{label}/embed/candidates.json"
    if candidates_rel not in checked:
        raise batch.BatchBlocked("text embedding has no candidate list", reason="source-mismatch", label=label)
    candidates = load_stored_json(checked[candidates_rel]["path"], label)
    if not isinstance(candidates, list):
        raise batch.BatchBlocked("text candidates are not a list", reason="source-mismatch", label=label)
    by_id = {}
    for entry in candidates:
        if not isinstance(entry, dict):
            raise batch.BatchBlocked("text candidate is incomplete", reason="source-mismatch", label=label)
        candidate = entry.get("candidate_id")
        if not isinstance(candidate, str) or candidate in ("", ".", "..") or "/" in candidate or "\\" in candidate:
            raise batch.BatchBlocked("text candidate is not a file name", reason="source-mismatch", label=label)
        if candidate in by_id:
            raise batch.BatchBlocked("text candidate ids are duplicated", reason="source-mismatch", label=label)
        by_id[candidate] = entry
    reused = 0
    for candidate, rel in states.items():
        state = load_stored_json(checked[rel]["path"], label)
        if not isinstance(state, dict) or state.get("status") != "succeeded":
            raise batch.BatchBlocked(
                "unresolved text dispatch is not the authorized joint",
                reason="not-succeeded", label=label,
            )
        entry = by_id.get(candidate)
        response_rel = prefix + candidate + ".json"
        if entry is None or response_rel not in checked:
            raise batch.BatchBlocked("text embedding does not match a candidate", reason="source-mismatch", label=label)
        if state.get("response_sha256") != checked[response_rel]["sha256"]:
            raise batch.BatchBlocked("text response hash does not match", reason="cache-hash-mismatch", label=label)
        digest, _window = embedding_digest(protocol, "text", entry, None, label)
        if state.get("input_hash") != digest:
            raise batch.BatchBlocked("text input hash does not match", reason="cache-hash-mismatch", label=label)
        require_embedding(load_stored_json(checked[response_rel]["path"], label), signature, "text", window, label)
        reused += 1
    orphans = responses - set(states)
    if orphans:
        raise batch.BatchBlocked("orphan text response", reason="source-mismatch", label=label)
    return reused


def audit_embeddings(source, checked, authorization, signature):
    """Check joints before any output directory is created. Reads only."""
    groups, _shared, _roots = film_groups(checked)
    expected = batch.contract.signature_template(signature)
    window = None
    unknowns = []
    omit_partials = []
    referenced = set()
    reused_joints = 0
    reused_text = 0
    not_started = 0
    cached_without_dispatch = 0
    for label, _rels in sorted(groups.items()):
        protocol_rel = f"films/{label}/embed/protocol.json"
        if protocol_rel not in checked:
            raise batch.BatchBlocked("embed protocol is missing", reason="source-mismatch", label=label)
        protocol = load_stored_json(checked[protocol_rel]["path"], label)
        if not isinstance(protocol, dict):
            raise batch.BatchBlocked("embed protocol is not an object", reason="source-mismatch", label=label)
        film_window = require_window(protocol, label)
        require_signature(protocol, expected, label)
        window = film_window
        windows = protocol.get("joint_windows") or []
        if not isinstance(windows, list):
            raise batch.BatchBlocked("joint windows are not a list", reason="source-mismatch", label=label)
        seen = set()
        for entry in windows:
            if not isinstance(entry, dict):
                raise batch.BatchBlocked("joint window is incomplete", reason="source-mismatch", label=label)
            candidate = entry.get("candidate_id")
            if not isinstance(candidate, str) or candidate in ("", ".", "..") or "/" in candidate or "\\" in candidate:
                raise batch.BatchBlocked("joint candidate is not a file name", reason="source-mismatch", label=label)
            if candidate in seen:
                raise batch.BatchBlocked("joint candidate ids are duplicated", reason="source-mismatch", label=label)
            seen.add(candidate)
            state_rel = f"films/{label}/embed/joint/{candidate}.state.json"
            response_rel = f"films/{label}/embed/joint/{candidate}.json"
            clip_rel, meta_rel, partial_rel = clip_rels(label, candidate)
            referenced.update(rel for rel in (state_rel, response_rel, clip_rel, meta_rel, partial_rel) if rel in checked)
            has_state = state_rel in checked
            has_response = response_rel in checked
            has_clip = clip_rel in checked
            has_meta = meta_rel in checked
            has_partial = partial_rel in checked
            if not has_state:
                if has_response:
                    raise batch.BatchBlocked("orphan joint response", reason="source-mismatch", label=label)
                if has_partial and not has_clip and not has_meta:
                    omit_partials.append(partial_rel)
                    not_started += 1
                    continue
                if has_partial or has_clip != has_meta:
                    raise batch.BatchBlocked("incomplete clip needs inspection", reason="source-mismatch", label=label)
                if has_clip and has_meta:
                    same_clip_sha(checked, clip_rel, meta_rel, load_stored_json(checked[meta_rel]["path"], label).get("sha256"), label)
                    cached_without_dispatch += 1
                    continue
                not_started += 1
                continue
            state = load_stored_json(checked[state_rel]["path"], label)
            if not isinstance(state, dict) or state.get("candidate_id") != candidate:
                raise batch.BatchBlocked("joint state does not match its candidate", reason="source-mismatch", label=label)
            status = state.get("status")
            if status != "succeeded":
                unknowns.append({
                    "label": label,
                    "candidate": candidate,
                    "status": status,
                    "inputHash": state.get("input_hash"),
                    "stateSha256": checked[state_rel]["sha256"],
                    "responsePresent": has_response,
                    "stateRel": state_rel,
                    "state": state,
                    "protocol": protocol,
                    "entry": entry,
                    "hasPartial": has_partial,
                })
                continue
            if has_partial:
                raise batch.BatchBlocked("incomplete clip needs inspection", reason="source-mismatch", label=label)
            if not has_response:
                raise batch.BatchBlocked("succeeded joint is missing its response", reason="source-mismatch", label=label)
            clip_sha = same_clip_sha(checked, clip_rel, meta_rel, (state.get("clip") or {}).get("sha256"), label)
            if state.get("response_sha256") != checked[response_rel]["sha256"]:
                raise batch.BatchBlocked("joint response hash does not match", reason="cache-hash-mismatch", label=label)
            digest, checked_window = embedding_digest(protocol, "joint", entry, clip_sha, label)
            if state.get("input_hash") != digest:
                raise batch.BatchBlocked("joint input hash does not match", reason="cache-hash-mismatch", label=label)
            require_embedding(
                load_stored_json(checked[response_rel]["path"], label),
                protocol["signature"], "joint", checked_window, label,
            )
            reused_joints += 1
        reused_text += audit_text(checked, label, protocol, protocol["signature"], film_window)
        prefix = f"films/{label}/embed/"
        for rel in checked:
            if not rel.startswith(prefix):
                continue
            name = rel[len(prefix):]
            if name.startswith("joint/") or name.startswith("clips/"):
                if rel not in referenced and name != "protocol.json":
                    if rel.startswith(prefix + "joint/") or rel.startswith(prefix + "clips/"):
                        raise batch.BatchBlocked("orphan joint file", reason="source-mismatch", label=label)
    if window is None:
        raise batch.BatchBlocked("recovery source has no embed protocol", reason="source-mismatch")
    for rel in checked:
        if rel.endswith(".partial.mp4") and rel not in omit_partials:
            raise batch.BatchBlocked("incomplete clip needs inspection", reason="source-mismatch")
    if len(unknowns) != 1:
        raise batch.BatchBlocked(
            "storage interruption recovery requires exactly one unresolved joint",
            reason="not-succeeded",
        )
    unknown = unknowns[0]
    if (
        unknown["label"] != authorization["film"]
        or unknown["candidate"] != authorization["candidate"]
        or unknown["inputHash"] != authorization["inputHash"]
        or unknown["stateSha256"] != authorization["stateSha256"]
    ):
        raise batch.BatchBlocked(
            "authorized joint does not match the inflight state",
            reason="source-mismatch", label=unknown["label"],
        )
    if unknown["status"] != "inflight":
        raise batch.BatchBlocked(
            "authorized joint is not an inflight dispatch",
            reason="not-succeeded", label=unknown["label"],
        )
    if unknown["responsePresent"]:
        raise batch.BatchBlocked("inflight joint already has a response", reason="source-mismatch", label=unknown["label"])
    if unknown["hasPartial"]:
        raise batch.BatchBlocked("incomplete clip needs inspection", reason="source-mismatch", label=unknown["label"])
    clip_rel, meta_rel, _partial_rel = clip_rels(unknown["label"], unknown["candidate"])
    clip_sha = same_clip_sha(
        checked, clip_rel, meta_rel,
        (unknown["state"].get("clip") or {}).get("sha256"), unknown["label"],
    )
    digest, _window = embedding_digest(unknown["protocol"], "joint", unknown["entry"], clip_sha, unknown["label"])
    if digest != authorization["inputHash"]:
        raise batch.BatchBlocked("inflight input hash does not match the clip", reason="cache-hash-mismatch", label=unknown["label"])
    reused_shared = audit_shared_documents(checked, expected, window)
    evidence = {
        "film": unknown["label"],
        "candidate": unknown["candidate"],
        "status": "inflight",
        "inputHash": authorization["inputHash"],
        "stateSha256": authorization["stateSha256"],
        "responsePresent": False,
        "sourceRelativePath": unknown["stateRel"],
        "clipSha256": clip_sha,
    }
    return {
        "reusedSucceededJoints": reused_joints,
        "reusedSucceededText": reused_text,
        "reusedSharedDocuments": reused_shared,
        "notStartedJoints": not_started,
        "cachedClipsWithoutDispatch": cached_without_dispatch,
        "omit": [unknown["stateRel"], *omit_partials],
        "omittedDispatch": [unknown["stateRel"]],
        "omittedIncompleteClips": [
            {"path": rel, "reason": "undispatched-partial", "successRecord": False}
            for rel in omit_partials
        ],
        "unknownEvidence": evidence,
    }


def plan_interruption(source, checked, output, authorization, snapshot):
    """Copy a finished alignment tree. Omit only the one authorized inflight state."""
    groups, _shared, roots = film_groups(checked)
    if "manifest.json" not in roots:
        raise batch.BatchBlocked("snapshot has no manifest", reason="source-mismatch")
    unexpected = [name for name in roots if name not in SKIPPED_ROOT_FILES and name != "manifest.json"]
    if unexpected:
        raise batch.BatchBlocked("unexpected recovery source file", reason="source-mismatch")
    films = [classify_film(label, rels, source, checked) for label, rels in sorted(groups.items())]
    for film in films:
        if film["kind"] not in ("settled", "pending"):
            raise batch.BatchBlocked(
                "storage interruption recovery does not accept an unresolved transcription failure",
                reason="transcription-failed", label=film["label"],
            )
    audit = audit_embeddings(source, checked, authorization, batch.EXPECTED_PROCESSING_ID)
    omit = set(audit["omit"])
    copies = []
    rewrites = []
    for rel in sorted(checked):
        if rel in SKIPPED_ROOT_FILES or rel in omit:
            continue
        if rel == "manifest.json":
            copies.append({"path": rel, "action": "copied", "source": rel})
            continue
        film_label_value = Path(rel).parts[1] if rel.startswith("films/") else None
        if rel.endswith("/asr/chunks.json") and film_label_value is not None:
            rewritten, changed = rebind_chunks(read_json(source / rel), output, checked, film_label_value)
            if changed:
                rewrites.append({
                    "path": rel, "action": "rebound", "source": rel, "data": json_bytes(rewritten),
                })
                continue
        if rel.endswith("/embed/protocol.json") and film_label_value is not None:
            rewrites.append({
                "path": rel,
                "action": "rebound",
                "source": rel,
                "data": json_bytes(rebind_embed(read_json(source / rel), output, film_label_value)),
            })
            continue
        copies.append({"path": rel, "action": "copied", "source": rel})
    omitted = sorted(name for name in roots if name in SKIPPED_ROOT_FILES)
    return {
        "copies": copies,
        "rewrites": rewrites,
        "derived": None,
        "tail": None,
        "derivedTails": [],
        "omittedSourceFiles": omitted,
        "interruption": {
            "authorization": {key: authorization[key] for key in INTERRUPTION_FIELDS},
            "unknownEvidence": audit["unknownEvidence"],
            "omittedDispatch": audit["omittedDispatch"],
            "omittedIncompleteClips": audit["omittedIncompleteClips"],
            "reusedSucceededJoints": audit["reusedSucceededJoints"],
            "reusedSucceededText": audit["reusedSucceededText"],
            "reusedSharedDocuments": audit["reusedSharedDocuments"],
            "notStartedJoints": audit["notStartedJoints"],
            "cachedClipsWithoutDispatch": audit["cachedClipsWithoutDispatch"],
            "snapshotTotalBytes": int(snapshot["totalBytes"]),
        },
    }


def probe_tail_status(protocol):
    source = protocol.get("source") if isinstance(protocol, dict) else None
    if not isinstance(source, dict):
        return {"status": "unknown"}
    return batch.contract.classify_video_tail(
        protocol.get("probe_facts"), source.get("audioStreamIndex"), source.get("runtimeTicks"))


def audit_video_tail(source, checked, authorization, signature):
    """Check every film before creating output. Only one undispatched last-window partial may be omitted."""
    groups, _shared, _roots = film_groups(checked)
    expected = batch.contract.signature_template(signature)
    window = None
    partials = []
    referenced = set()
    reused_joints = 0
    reused_text = 0
    not_started = 0
    cached_without_dispatch = 0
    for label, _rels in sorted(groups.items()):
        protocol_rel = f"films/{label}/embed/protocol.json"
        if protocol_rel not in checked:
            raise batch.BatchBlocked("embed protocol is missing", reason="source-mismatch", label=label)
        protocol = load_stored_json(checked[protocol_rel]["path"], label)
        if not isinstance(protocol, dict):
            raise batch.BatchBlocked("embed protocol is not an object", reason="source-mismatch", label=label)
        film_window = require_window(protocol, label)
        require_signature(protocol, expected, label)
        window = film_window
        if probe_tail_status(protocol)["status"] == "over-bound":
            raise batch.BatchBlocked(
                "a frozen probe video tail is longer than 200 milliseconds",
                reason="video-tail", label=label,
            )
        windows = protocol.get("joint_windows") or []
        if not isinstance(windows, list):
            raise batch.BatchBlocked("joint windows are not a list", reason="source-mismatch", label=label)
        try:
            runtime = int(protocol["source"]["runtimeTicks"])
        except (TypeError, ValueError, KeyError) as exc:
            raise batch.BatchBlocked("film runtime is missing", reason="source-mismatch", label=label) from exc
        seen = set()
        for entry in windows:
            if not isinstance(entry, dict):
                raise batch.BatchBlocked("joint window is incomplete", reason="source-mismatch", label=label)
            candidate = entry.get("candidate_id")
            if not isinstance(candidate, str) or candidate in ("", ".", "..") or "/" in candidate or "\\" in candidate:
                raise batch.BatchBlocked("joint candidate is not a file name", reason="source-mismatch", label=label)
            if candidate in seen:
                raise batch.BatchBlocked("joint candidate ids are duplicated", reason="source-mismatch", label=label)
            seen.add(candidate)
            state_rel = f"films/{label}/embed/joint/{candidate}.state.json"
            response_rel = f"films/{label}/embed/joint/{candidate}.json"
            clip_rel, meta_rel, partial_rel = clip_rels(label, candidate)
            referenced.update(rel for rel in (state_rel, response_rel, clip_rel, meta_rel, partial_rel) if rel in checked)
            has_state = state_rel in checked
            has_response = response_rel in checked
            has_clip = clip_rel in checked
            has_meta = meta_rel in checked
            has_partial = partial_rel in checked
            if not has_state:
                if has_response:
                    raise batch.BatchBlocked("orphan joint response", reason="source-mismatch", label=label)
                if has_partial and not has_clip and not has_meta:
                    partials.append({
                        "label": label,
                        "candidate": candidate,
                        "rel": partial_rel,
                        "sha256": checked[partial_rel]["sha256"],
                        "endTicks": int(entry["end_ticks"]),
                        "runtimeTicks": runtime,
                        "protocol": protocol,
                    })
                    not_started += 1
                    continue
                if has_partial or has_clip != has_meta:
                    raise batch.BatchBlocked("incomplete clip needs inspection", reason="source-mismatch", label=label)
                if has_clip and has_meta:
                    same_clip_sha(checked, clip_rel, meta_rel, load_stored_json(checked[meta_rel]["path"], label).get("sha256"), label)
                    cached_without_dispatch += 1
                    continue
                not_started += 1
                continue
            state = load_stored_json(checked[state_rel]["path"], label)
            if not isinstance(state, dict) or state.get("candidate_id") != candidate:
                raise batch.BatchBlocked("joint state does not match its candidate", reason="source-mismatch", label=label)
            if state.get("status") != "succeeded":
                raise batch.BatchBlocked(
                    "unresolved joint is not an undispatched partial",
                    reason="not-succeeded", label=label,
                )
            if has_partial:
                raise batch.BatchBlocked("incomplete clip needs inspection", reason="source-mismatch", label=label)
            if not has_response:
                raise batch.BatchBlocked("succeeded joint is missing its response", reason="source-mismatch", label=label)
            clip_sha = same_clip_sha(checked, clip_rel, meta_rel, (state.get("clip") or {}).get("sha256"), label)
            if state.get("response_sha256") != checked[response_rel]["sha256"]:
                raise batch.BatchBlocked("joint response hash does not match", reason="cache-hash-mismatch", label=label)
            digest, checked_window = embedding_digest(protocol, "joint", entry, clip_sha, label)
            if state.get("input_hash") != digest:
                raise batch.BatchBlocked("joint input hash does not match", reason="cache-hash-mismatch", label=label)
            require_embedding(
                load_stored_json(checked[response_rel]["path"], label),
                protocol["signature"], "joint", checked_window, label,
            )
            reused_joints += 1
        reused_text += audit_text(checked, label, protocol, protocol["signature"], film_window)
        prefix = f"films/{label}/embed/"
        for rel in checked:
            if not rel.startswith(prefix):
                continue
            name = rel[len(prefix):]
            if name.startswith("joint/") or name.startswith("clips/"):
                if rel not in referenced and name != "protocol.json":
                    if rel.startswith(prefix + "joint/") or rel.startswith(prefix + "clips/"):
                        raise batch.BatchBlocked("orphan joint file", reason="source-mismatch", label=label)
    if window is None:
        raise batch.BatchBlocked("recovery source has no embed protocol", reason="source-mismatch")
    known_partials = {item["rel"] for item in partials}
    for rel in checked:
        if rel.endswith(".partial.mp4") and rel not in known_partials:
            raise batch.BatchBlocked("incomplete clip needs inspection", reason="source-mismatch")
    if len(partials) != 1:
        raise batch.BatchBlocked(
            "video tail recovery requires exactly one undispatched partial",
            reason="source-mismatch",
        )
    found = partials[0]
    if found["label"] != authorization["film"] or found["candidate"] != authorization["candidate"]:
        raise batch.BatchBlocked(
            "authorized partial does not match the undispatched file",
            reason="source-mismatch", label=found["label"],
        )
    if found["sha256"] != authorization["partialSha256"]:
        raise batch.BatchBlocked(
            "authorized partial hash does not match",
            reason="source-mismatch", label=found["label"],
        )
    if found["endTicks"] != found["runtimeTicks"]:
        raise batch.BatchBlocked(
            "authorized partial is not the last window",
            reason="source-mismatch", label=found["label"],
        )
    if probe_tail_status(found["protocol"])["status"] != "within-bound":
        raise batch.BatchBlocked(
            "authorized partial is not a proven video tail within 200 milliseconds",
            reason="video-tail", label=found["label"],
        )
    reused_shared = audit_shared_documents(checked, expected, window)
    evidence = {
        "film": found["label"],
        "candidate": found["candidate"],
        "status": "undispatched-partial",
        "partialSha256": authorization["partialSha256"],
        "responsePresent": False,
        "dispatchPresent": False,
        "sourceRelativePath": found["rel"],
    }
    return {
        "reusedSucceededJoints": reused_joints,
        "reusedSucceededText": reused_text,
        "reusedSharedDocuments": reused_shared,
        "notStartedJoints": not_started,
        "cachedClipsWithoutDispatch": cached_without_dispatch,
        "omit": [found["rel"]],
        "omittedIncompleteClips": [{
            "path": found["rel"],
            "reason": "undispatched-partial",
            "successRecord": False,
        }],
        "unknownEvidence": evidence,
    }


def plan_video_tail(source, checked, output, authorization, snapshot):
    """Copy a finished alignment tree. Omit only the one never-dispatched last-window partial."""
    groups, _shared, roots = film_groups(checked)
    if "manifest.json" not in roots:
        raise batch.BatchBlocked("snapshot has no manifest", reason="source-mismatch")
    unexpected = [name for name in roots if name not in SKIPPED_ROOT_FILES and name != "manifest.json"]
    if unexpected:
        raise batch.BatchBlocked("unexpected recovery source file", reason="source-mismatch")
    films = [classify_film(label, rels, source, checked) for label, rels in sorted(groups.items())]
    for film in films:
        if film["kind"] not in ("settled", "pending"):
            raise batch.BatchBlocked(
                "video tail recovery does not accept an unresolved transcription failure",
                reason="transcription-failed", label=film["label"],
            )
    audit = audit_video_tail(source, checked, authorization, batch.EXPECTED_PROCESSING_ID)
    omit = set(audit["omit"])
    copies = []
    rewrites = []
    for rel in sorted(checked):
        if rel in SKIPPED_ROOT_FILES or rel in omit:
            continue
        if rel == "manifest.json":
            copies.append({"path": rel, "action": "copied", "source": rel})
            continue
        film_label_value = Path(rel).parts[1] if rel.startswith("films/") else None
        if rel.endswith("/asr/chunks.json") and film_label_value is not None:
            rewritten, changed = rebind_chunks(read_json(source / rel), output, checked, film_label_value)
            if changed:
                rewrites.append({
                    "path": rel, "action": "rebound", "source": rel, "data": json_bytes(rewritten),
                })
                continue
        if rel.endswith("/embed/protocol.json") and film_label_value is not None:
            rewrites.append({
                "path": rel,
                "action": "rebound",
                "source": rel,
                "data": json_bytes(stamp_video_tail(read_json(source / rel), output, film_label_value)),
            })
            continue
        copies.append({"path": rel, "action": "copied", "source": rel})
    omitted = sorted(name for name in roots if name in SKIPPED_ROOT_FILES)
    return {
        "copies": copies,
        "rewrites": rewrites,
        "derived": None,
        "tail": None,
        "derivedTails": [],
        "omittedSourceFiles": omitted,
        "videoTail": {
            "authorization": {key: authorization[key] for key in VIDEO_TAIL_FIELDS},
            "unknownEvidence": audit["unknownEvidence"],
            "omittedIncompleteClips": audit["omittedIncompleteClips"],
            "reusedSucceededJoints": audit["reusedSucceededJoints"],
            "reusedSucceededText": audit["reusedSucceededText"],
            "reusedSharedDocuments": audit["reusedSharedDocuments"],
            "notStartedJoints": audit["notStartedJoints"],
            "cachedClipsWithoutDispatch": audit["cachedClipsWithoutDispatch"],
            "snapshotTotalBytes": int(snapshot["totalBytes"]),
        },
    }


def receipt_body(config, snapshot_path, snapshot, plan, written):
    derived = plan.get("derived")
    skips = []
    if derived is not None:
        skips.append({
            "label": derived["tasks_rel"].split("/")[1],
            "chunk_id": derived["chunk_id"],
            "reason": "language-unsupported",
            "detected_language": derived["detected_language"],
            "skip_policy": POLICY,
            "alignment_executed": False,
            "text_retained": True,
            "input_text_sha256": derived["input_text_sha256"],
        })
    body = {
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
        "derivedSkips": skips,
        "derivedTails": plan.get("derivedTails") or [],
        "files": written,
    }
    if plan.get("tail") is not None:
        body["alignmentTimePolicy"] = transcribe.TAIL_TIME_POLICY
    interruption = plan.get("interruption")
    if interruption is not None:
        body["mode"] = INTERRUPTION_MODE
        body["authorization"] = interruption["authorization"]
        body["unknownEvidence"] = interruption["unknownEvidence"]
        body["omittedDispatch"] = interruption["omittedDispatch"]
        body["omittedIncompleteClips"] = interruption["omittedIncompleteClips"]
        body["reusedSucceededJoints"] = interruption["reusedSucceededJoints"]
        body["reusedSucceededText"] = interruption["reusedSucceededText"]
        body["reusedSharedDocuments"] = interruption["reusedSharedDocuments"]
        body["notStartedJoints"] = interruption["notStartedJoints"]
        body["cachedClipsWithoutDispatch"] = interruption["cachedClipsWithoutDispatch"]
        body["snapshotTotalBytes"] = interruption["snapshotTotalBytes"]
        body["copyCount"] = len(plan["copies"])
        body["rebindCount"] = len(plan["rewrites"])
        body["modelRequests"] = 0
        body["authorizedResends"] = 1
        body["transcriptionAccuracyVerified"] = False
        body["batchExitAccepted"] = False
    video_tail = plan.get("videoTail")
    if video_tail is not None:
        body["mode"] = VIDEO_TAIL_MODE
        body["authorization"] = video_tail["authorization"]
        body["unknownEvidence"] = video_tail["unknownEvidence"]
        body["omittedIncompleteClips"] = video_tail["omittedIncompleteClips"]
        body["reusedSucceededJoints"] = video_tail["reusedSucceededJoints"]
        body["reusedSucceededText"] = video_tail["reusedSucceededText"]
        body["reusedSharedDocuments"] = video_tail["reusedSharedDocuments"]
        body["notStartedJoints"] = video_tail["notStartedJoints"]
        body["cachedClipsWithoutDispatch"] = video_tail["cachedClipsWithoutDispatch"]
        body["snapshotTotalBytes"] = video_tail["snapshotTotalBytes"]
        body["copyCount"] = len(plan["copies"])
        body["rebindCount"] = len(plan["rewrites"])
        body["modelRequests"] = 0
        body["rebuiltPartial"] = 1
        body["videoTailPolicy"] = batch.contract.VIDEO_TAIL_POLICY
        body["transcriptionAccuracyVerified"] = False
        body["batchExitAccepted"] = False
    return body


def confirm_idempotent(output, checked, tail_policy=None, interruption=None, video_tail=None):
    receipt_path = Path(output) / RECEIPT_NAME
    if not receipt_path.is_file():
        raise batch.BatchBlocked("recovery output exists without a receipt", reason="output-exists")
    receipt = read_json(receipt_path)
    if receipt.get("version") != RECEIPT_VERSION or receipt.get("policy") != POLICY or receipt.get("hardlink") is not False:
        raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
    if interruption is not None and video_tail is not None:
        raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
    if interruption is None and video_tail is None:
        if receipt.get("mode"):
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if (receipt.get("alignmentTimePolicy") or None) != (tail_policy or None):
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
    elif interruption is not None:
        if receipt.get("mode") != INTERRUPTION_MODE or receipt.get("authorization") != interruption:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if receipt.get("modelRequests") != 0 or receipt.get("authorizedResends") != 1:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if receipt.get("batchExitAccepted") is not False or receipt.get("transcriptionAccuracyVerified") is not False:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if receipt.get("snapshotSha256") != interruption["snapshotSha256"]:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
    else:
        evidence = receipt.get("unknownEvidence") or {}
        if receipt.get("mode") != VIDEO_TAIL_MODE or receipt.get("authorization") != video_tail:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if receipt.get("modelRequests") != 0 or receipt.get("rebuiltPartial") != 1 or "authorizedResends" in receipt:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if receipt.get("videoTailPolicy") != batch.contract.VIDEO_TAIL_POLICY:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if receipt.get("batchExitAccepted") is not False or receipt.get("transcriptionAccuracyVerified") is not False:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if receipt.get("snapshotSha256") != video_tail["snapshotSha256"]:
            raise batch.BatchBlocked("recovery receipt does not match this prepare", reason="output-exists")
        if (
            evidence.get("status") != "undispatched-partial"
            or evidence.get("responsePresent") is not False
            or evidence.get("dispatchPresent") is not False
        ):
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


def configured_tail(config):
    value = config.get("alignmentTimePolicy", None)
    if value in (None, ""):
        return None
    if value != transcribe.TAIL_TIME_POLICY:
        raise batch.BatchBlocked("unknown alignment time policy", reason="language-policy")
    return value


def prepare(config):
    config = batch.require_config(dict(config))
    if config.get("unsupportedAlignLanguagePolicy") != POLICY:
        raise batch.BatchBlocked("recovery requires the explicit skip policy", reason="language-policy")
    tail_name = configured_tail(config)
    interruption = configured_interruption(config)
    video_tail = configured_video_tail(config)
    if interruption is not None and video_tail is not None:
        raise batch.BatchBlocked("recovery accepts only one explicit mode", reason="config")
    if video_tail is not None and config.get("videoTailPolicy") != batch.contract.VIDEO_TAIL_POLICY:
        raise batch.BatchBlocked("video tail recovery requires the hold-last-frame policy", reason="config")
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
    if interruption is not None and file_sha256(snapshot_path) != interruption["snapshotSha256"]:
        raise batch.BatchBlocked(
            "snapshot hash does not match the interruption authorization",
            reason="source-mismatch",
        )
    if video_tail is not None and file_sha256(snapshot_path) != video_tail["snapshotSha256"]:
        raise batch.BatchBlocked(
            "snapshot hash does not match the video tail authorization",
            reason="source-mismatch",
        )
    if output.exists():
        if not output.is_dir():
            raise batch.BatchBlocked("recovery output exists", reason="output-exists")
        receipt = confirm_idempotent(output, checked, tail_name, interruption, video_tail)
        source_unchanged(checked)
        return receipt
    if interruption is not None:
        plan = plan_interruption(source, checked, output, interruption, snapshot)
    elif video_tail is not None:
        plan = plan_video_tail(source, checked, output, video_tail, snapshot)
    else:
        plan = plan_recovery(source, checked, output, tail_policy=tail_name is not None)
    output.mkdir(mode=0o700)
    try:
        written = apply_plan(output, checked, plan)
        source_unchanged(checked)
        if plan.get("tail") is not None:
            written = refresh_tail(output, plan, written)
            source_unchanged(checked)
        body = receipt_body(config, snapshot_path, snapshot, plan, written)
        atomic_write(output / RECEIPT_NAME, json_bytes(body))
    except Exception:
        source_unchanged(checked)
        raise
    source_unchanged(checked)
    return read_json(output / RECEIPT_NAME)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="把已完成的批次按字节复制到新目录。可派生一处语言缺口，离线复核一处不超过 80 毫秒的末字越界，按显式白名单省略一处存储中断的音画请求，或省略一处从未派发的片尾残片以便定格重切。不推理。")
    parser.add_argument("command", choices=("prepare",))
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = batch.load_config(args.config)
        receipt = prepare(config)
    except batch.BatchBlocked as exc:
        print(json.dumps({"ready": False, "reason": exc.reason, "label": exc.label}, ensure_ascii=False), file=sys.stderr)
        return 1
    summary = {
        "ready": True,
        "version": receipt["version"],
        "policy": receipt["policy"],
        "outputFileCount": receipt["outputFileCount"],
        "hardlink": receipt["hardlink"],
        "derivedSkips": receipt["derivedSkips"],
        "derivedTails": receipt.get("derivedTails") or [],
    }
    if receipt.get("alignmentTimePolicy"):
        summary["alignmentTimePolicy"] = receipt["alignmentTimePolicy"]
    if receipt.get("mode") == INTERRUPTION_MODE:
        summary["mode"] = receipt["mode"]
        summary["film"] = receipt["authorization"]["film"]
        summary["candidate"] = receipt["authorization"]["candidate"]
        summary["reusedSucceededJoints"] = receipt["reusedSucceededJoints"]
        summary["modelRequests"] = receipt["modelRequests"]
        summary["authorizedResends"] = receipt["authorizedResends"]
        summary["batchExitAccepted"] = receipt["batchExitAccepted"]
        summary["transcriptionAccuracyVerified"] = receipt["transcriptionAccuracyVerified"]
    if receipt.get("mode") == VIDEO_TAIL_MODE:
        summary["mode"] = receipt["mode"]
        summary["film"] = receipt["authorization"]["film"]
        summary["candidate"] = receipt["authorization"]["candidate"]
        summary["reusedSucceededJoints"] = receipt["reusedSucceededJoints"]
        summary["modelRequests"] = receipt["modelRequests"]
        summary["rebuiltPartial"] = receipt["rebuiltPartial"]
        summary["videoTailPolicy"] = receipt["videoTailPolicy"]
        summary["batchExitAccepted"] = receipt["batchExitAccepted"]
        summary["transcriptionAccuracyVerified"] = receipt["transcriptionAccuracyVerified"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
