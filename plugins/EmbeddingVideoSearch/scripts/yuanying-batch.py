#!/usr/bin/env python3
"""One directory, one twelve-second pass. Not a queue and not a new agent.

The pass plans every included film from zero through its real runtime.
Coverage is full only after prepare, ASR, alignment, documents, joint and
text have all succeeded and the joint windows cover that runtime. A failed
or unknown transcription is never treated as an empty transcript.

Sample windows are read only. A matching clock range is not reused unless
the stored input hash equals the hash of this film's own candidate. The
hash includes the candidate object, so a sample id or full_film flag makes
the inputs different. This script never rewrites a sample checkpoint.
"""
import argparse
import importlib.util
import json
import os
import sqlite3
import struct
import subprocess
import sys
import unicodedata
from pathlib import Path


BATCH_VERSION = "issue3-yuanying-batch-v1"
FILM_VERSION = "issue3-yuanying-film-v1"
ASR_VERSION = "issue3-yuanying-asr-v1"
EXPECTED_PROCESSING_ID = "eg2-7c43611ad2e20661c3d3"
LANGUAGE_POLICY = "auto-v1"
MODEL_CONCURRENCY = 1
TRANSCRIBE_STAGES = ("prepare", "asr", "align", "documents")
EMBED_STAGES = ("joint", "text")
UNKNOWN_STATUSES = {"unknown", "started", "inflight", "completion-unknown", "terminal-response"}
SOURCE_FIELDS = (
    "itemId", "mediaSourceId", "libraryId", "contentVersion", "runtimeTicks",
    "container", "audioStreamIndex", "sourceItemId", "sourcePath", "sizeBytes", "modifiedTicks",
)
PRESERVED_PARTS = {
    "multilingual-auto-v3", "multilingual-auto-v1", "encoding-code",
    "asr-C1", "asr-C2", "asr-C3", "asr-C2-rejected-audio-v1",
}
LIVE_INDEX_ID = "8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17"
PUBLIC_HIDDEN = {
    "sourcePath", "source_path", "text", "transcript", "vector", "embedding",
    "token", "wav_path", "path", "originalSourcePath", "displayName", "filename",
    "priorInputHash", "filmInputHash", "input_hash", "response_sha256",
}


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sample_import = load("yuanying_sample_import", "sample-c-import.py")
contract = sample_import.contract
film_import = sample_import.importer
embed = film_import.embedding


class BatchBlocked(contract.ContractError):
    def __init__(self, message, *, reason, status=None, stage=None, label=None):
        super().__init__(message)
        self.reason = reason
        self.status = status
        self.stage = stage
        self.label = label


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    contract.save_json(path, value)


def canonical_path(path):
    return unicodedata.normalize("NFC", str(Path(path).expanduser().resolve()))


def same_path(left, right):
    return canonical_path(left) == canonical_path(right)


def file_sha256(path):
    return embed.sha(path)


def snapshot_tree(root):
    root = Path(root)
    found = {}
    if not root.exists():
        return found
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        found[str(path.relative_to(root))] = file_sha256(path)
    return found


def modified_ticks(path):
    return Path(path).stat().st_mtime_ns // 100 + 621355968000000000


def label_for(index):
    return f"F{index:03d}"


def public_value(value):
    if isinstance(value, dict):
        return {key: public_value(inner) for key, inner in value.items() if key not in PUBLIC_HIDDEN}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def refuse_preserved(path):
    path = Path(path)
    contract.refuse_write_target(path)
    for part in (path, *path.parents):
        if part.name in PRESERVED_PARTS or part.name.startswith("asr-"):
            raise BatchBlocked("refusing to write preserved artifacts", reason="preserved-artifact")
        if part.name == LIVE_INDEX_ID:
            raise BatchBlocked("refusing to write the live index directory", reason="live-index")


def is_inside(child, parent):
    child = Path(canonical_path(child))
    parent = Path(canonical_path(parent))
    return child == parent or parent in child.parents


def refuse_live_index(path):
    path = Path(path)
    if path.name == "index.db" or path.resolve().name == "index.db":
        raise BatchBlocked("refusing to open index.db", reason="live-index")
    if LIVE_INDEX_ID in path.resolve().parts and path.resolve().name.endswith(".db"):
        raise BatchBlocked("refusing to open the live index directory", reason="live-index")


def plan_film_windows(runtime_ticks):
    runtime = int(runtime_ticks)
    if runtime < contract.LENGTH_TICKS:
        raise BatchBlocked("film is shorter than one 12-second window", reason="shorter-than-12s")
    try:
        windows = contract.plan_local(runtime)
    except contract.ContractError as exc:
        raise BatchBlocked("film windows do not cover the runtime", reason="coverage-incomplete") from exc
    if windows[0][0] != 0 or windows[-1][1] != runtime:
        raise BatchBlocked("windows do not cover the runtime", reason="coverage-incomplete")
    for start, end in windows:
        if end - start != contract.LENGTH_TICKS:
            raise BatchBlocked("a whole-film window was shortened", reason="coverage-incomplete")
    return windows


def window_entries(source):
    item = contract.canonical_item(source["itemId"]).replace("-", "")
    entries = []
    for index, (start, end) in enumerate(plan_film_windows(source["runtimeTicks"])):
        entries.append({
            "candidate_id": f"joint-{item}-{index:04d}",
            "start_ticks": start,
            "end_ticks": end,
            "item_id": contract.canonical_item(source["itemId"]),
            "content_version": source["contentVersion"],
            "source_path": source["sourcePath"],
            "full_film": True,
        })
    return entries


def copy_source(source):
    """Copy the frozen source. presence stays whatever the file stored."""
    copied = {key: source[key] for key in SOURCE_FIELDS}
    if "presence" in source:
        copied["presence"] = source["presence"]
    return copied


def source_tuple(source):
    """Identity is the file, media and version fields. presence is only an observation."""
    values = []
    for field in SOURCE_FIELDS:
        if field not in source:
            raise BatchBlocked("source fact missing: " + field, reason="source-mismatch")
        value = source[field]
        if field in {"itemId", "libraryId", "sourceItemId"}:
            value = contract.canonical_item(value)
        elif field == "sourcePath":
            value = canonical_path(value)
        elif field in {"runtimeTicks", "audioStreamIndex", "sizeBytes", "modifiedTicks"}:
            value = int(value)
        else:
            value = str(value)
        values.append((field, value))
    if values[0][1] != dict(values)["sourceItemId"]:
        raise BatchBlocked("itemId and sourceItemId differ", reason="source-mismatch")
    return tuple(values)


def same_source(left, right):
    return source_tuple(left) == source_tuple(right)


def require_config(config):
    if not isinstance(config, dict):
        raise BatchBlocked("config must be a JSON object", reason="config")
    base = str(config.get("embeddingBaseUrl") or "")
    for port in (18780, 18783, 8096):
        if f":{port}" in base:
            raise BatchBlocked("batch model port is 18784", reason="wrong-port")
    try:
        contract.require_isolated_base(base)
    except contract.ContractError as exc:
        raise BatchBlocked("batch model port is 18784", reason="wrong-port") from exc
    if config.get("processingId") != EXPECTED_PROCESSING_ID:
        raise BatchBlocked("processing_id does not match the frozen 18784 fingerprint", reason="signature-mismatch")
    if int(config.get("maxInflightEmbeddings", MODEL_CONCURRENCY)) != MODEL_CONCURRENCY:
        raise BatchBlocked("model concurrency must stay 1", reason="concurrency")
    if config.get("transcriptionLanguagePolicy", LANGUAGE_POLICY) != LANGUAGE_POLICY:
        raise BatchBlocked("language policy must stay auto-v1", reason="language-policy")
    for key in ("sourceDirectory", "sourceFacts", "outputRoot", "workerPython", "asrModel", "alignerModel"):
        if not config.get(key):
            raise BatchBlocked("config missing " + key, reason="config")
    refuse_preserved(config["outputRoot"])
    sample_root = config.get("sampleEmbedRoot")
    if sample_root and is_inside(config["outputRoot"], sample_root):
        raise BatchBlocked("output is inside the sample embeddings", reason="preserved-artifact")
    return config


def load_config(path):
    config = read_json(path)
    require_config(config)
    expected = config.get("orchestratorPython")
    if expected and canonical_path(expected) != canonical_path(sys.executable):
        raise BatchBlocked("orchestrator python does not match config", reason="interpreter-mismatch")
    return config


def list_media(directory):
    included, skipped = [], []
    for path in sorted(Path(directory).iterdir(), key=lambda item: item.name):
        if path.is_file() and path.suffix.lower() == ".mp4":
            included.append(path)
        elif path.is_file() or path.is_dir():
            skipped.append(path)
    return included, skipped


def frozen_label(item):
    if not isinstance(item, dict) or not item.get("label"):
        return None
    label = str(item["label"])
    if len(label) != 4 or not label.startswith("F") or not label[1:].isdigit():
        raise BatchBlocked("frozen label must look like F001", reason="source-mismatch")
    return label


def load_facts(path):
    raw = read_json(path)
    if isinstance(raw, dict) and isinstance(raw.get("films"), list):
        raw = raw["films"]
    if not isinstance(raw, list) or not raw:
        raise BatchBlocked("source facts must be a non-empty list", reason="source-mismatch")
    facts = []
    seen = set()
    for item in raw:
        source = item.get("source") if isinstance(item, dict) and "source" in item else item
        source_tuple(source)
        if isinstance(item, dict) and item.get("path") and not same_path(item["path"], source["sourcePath"]):
            raise BatchBlocked("frozen path does not match sourcePath", reason="source-mismatch")
        label = frozen_label(item)
        if label in seen:
            raise BatchBlocked("duplicate frozen label", reason="source-mismatch")
        if label:
            seen.add(label)
        facts.append({
            "source": copy_source(source),
            "probe": item.get("probe") if isinstance(item, dict) else None,
            "label": label,
        })
    return facts


def match_facts(directory, facts):
    included, skipped = list_media(directory)
    by_path = {}
    for fact in facts:
        key = canonical_path(fact["source"]["sourcePath"])
        if key in by_path:
            raise BatchBlocked("duplicate source path in facts", reason="source-mismatch")
        by_path[key] = fact
    missing, matched, drift = [], [], []
    for path in included:
        fact = by_path.pop(canonical_path(path), None)
        if fact is None:
            missing.append(path)
            continue
        source = fact["source"]
        stat = path.stat()
        if int(source["sizeBytes"]) != stat.st_size or int(source["modifiedTicks"]) != modified_ticks(path):
            drift.append(contract.canonical_item(source["itemId"]))
            continue
        matched.append((path, fact))
    unmatched_facts = [contract.canonical_item(fact["source"]["itemId"]) for fact in by_path.values()]
    return {
        "matched": matched,
        "missingFiles": len(missing),
        "unmatchedFacts": unmatched_facts,
        "drift": drift,
        "skipped": len(skipped),
    }


def signature_for(config):
    return contract.signature_template(config["processingId"])


def build_film_record(index, fact, config):
    source = copy_source(fact["source"])
    windows = window_entries(source)
    label = fact.get("label") or label_for(index)
    return {
        "label": label,
        "itemId": contract.canonical_item(source["itemId"]),
        "runtimeTicks": int(source["runtimeTicks"]),
        "windows": len(windows),
        "source": source,
        "probe": fact.get("probe"),
        "eligible": True,
    }


def root_protocol(config, films):
    return {
        "version": BATCH_VERSION,
        "fullFilm": True,
        "coverage": "pending",
        "window": contract.window_template(),
        "embedding_base_url": contract.ISOLATED_BASE,
        "max_inflight_embeddings": MODEL_CONCURRENCY,
        "signature": signature_for(config),
        "transcription_language_policy": LANGUAGE_POLICY,
        "sampleReuse": {"policy": "explicit-equivalence-v1", "forgedInputHash": False},
        "films": films,
    }


def asr_protocol(record, config):
    return {
        "version": ASR_VERSION,
        "fullFilm": True,
        "coverage": "pending",
        "label": record["label"],
        "transcription_language_policy": LANGUAGE_POLICY,
        "asr_max_chunk_seconds": 30,
        "asr_max_new_tokens": 1024,
        "worker_python": str(config["workerPython"]),
        "asr_model_path": str(config["asrModel"]),
        "aligner_model_path": str(config["alignerModel"]),
        "source": record["source"],
    }


def embed_protocol(record, config, output_root):
    shared = Path(config["outputRoot"]) / "shared-document-cache"
    return {
        "version": FILM_VERSION,
        "fullFilm": True,
        "coverage": "pending",
        "label": record["label"],
        "window": contract.window_template(),
        "embedding_base_url": contract.ISOLATED_BASE,
        "max_inflight_embeddings": MODEL_CONCURRENCY,
        "signature": signature_for(config),
        "source": record["source"],
        "probe_facts": record.get("probe"),
        "joint_windows": window_entries(record["source"]),
        "transcription_language_policy": LANGUAGE_POLICY,
        "asr_run": str((Path(output_root) / "films" / record["label"] / "asr").resolve()),
        "shared_document_cache": {"directory": str(shared.resolve())},
        "sampleReuse": {"policy": "explicit-equivalence-v1", "forgedInputHash": False},
    }


def write_if_absent(path, value):
    path = Path(path)
    if path.exists():
        current = read_json(path)
        if current != value and path.name == "protocol.json":
            current_source = current.get("source") or {}
            new_source = value.get("source") or {}
            if current_source and new_source and not same_source(current_source, new_source):
                raise BatchBlocked("existing protocol source changed", reason="source-mismatch")
            if current.get("joint_windows") and value.get("joint_windows") and current["joint_windows"] != value["joint_windows"]:
                raise BatchBlocked("existing windows changed", reason="coverage-incomplete")
        return False
    write_json(path, value)
    return True


def prepare_layout(config, films):
    output = Path(config["outputRoot"])
    refuse_preserved(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    shared = output / "shared-document-cache"
    refuse_preserved(shared)
    shared.mkdir(parents=True, exist_ok=True, mode=0o700)
    public_films = []
    for record in films:
        film_dir = output / "films" / record["label"]
        asr = film_dir / "asr"
        embed_run = film_dir / "embed"
        refuse_preserved(film_dir)
        asr.mkdir(parents=True, exist_ok=True, mode=0o700)
        embed_run.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_if_absent(asr / "protocol.json", asr_protocol(record, config))
        protocol = embed_protocol(record, config, output)
        protocol["shared_document_cache"] = {"directory": str(shared.resolve())}
        protocol["asr_run"] = str(asr.resolve())
        write_if_absent(embed_run / "protocol.json", protocol)
        public_films.append({
            "label": record["label"],
            "itemId": record["itemId"],
            "runtimeTicks": record["runtimeTicks"],
            "windows": record["windows"],
        })
    manifest = root_protocol(config, [{
        "label": record["label"],
        "itemId": record["itemId"],
        "source": record["source"],
        "windows": record["windows"],
        "probe": record.get("probe"),
    } for record in films])
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        current = read_json(manifest_path)
        if [film["itemId"] for film in current.get("films", [])] != [film["itemId"] for film in manifest["films"]]:
            raise BatchBlocked("manifest film list changed", reason="source-mismatch")
    else:
        write_json(manifest_path, manifest)
    return public_films


def spans(entries):
    return [(int(entry["start_ticks"]), int(entry["end_ticks"])) for entry in entries]


def reuse_decision(film_protocol, film_entry, prior_protocol, prior_entry, prior_state, clip_sha):
    """Classify one stored window. Never writes either run."""
    film_span = (int(film_entry["start_ticks"]), int(film_entry["end_ticks"]))
    prior_span = (int(prior_entry["start_ticks"]), int(prior_entry["end_ticks"]))
    planned = set(plan_film_windows(film_protocol["source"]["runtimeTicks"]))
    record = {
        "reused": False,
        "forgedInputHash": False,
        "inputHashEquivalent": False,
        "boundsMatch": prior_span == film_span and film_span in planned,
        "priorBounds": [prior_span[0], prior_span[1]],
        "filmBounds": [film_span[0], film_span[1]],
        "priorCandidateId": prior_entry.get("candidate_id"),
        "filmCandidateId": film_entry.get("candidate_id"),
    }
    if prior_span not in planned:
        record["reason"] = "tail-boundary-mismatch"
        return record
    if prior_span != film_span:
        record["reason"] = "bounds-mismatch"
        return record
    try:
        if not same_source(film_protocol["source"], prior_protocol["source"]):
            record["reason"] = "source-mismatch"
            return record
    except BatchBlocked:
        record["reason"] = "source-mismatch"
        return record
    try:
        if contract.signature_tuple(film_protocol["signature"]) != contract.signature_tuple(prior_protocol["signature"]):
            record["reason"] = "signature-mismatch"
            return record
    except contract.ContractError:
        record["reason"] = "signature-mismatch"
        return record
    if not isinstance(prior_state, dict) or prior_state.get("status") != "succeeded":
        record["reason"] = "not-succeeded"
        record["status"] = None if not isinstance(prior_state, dict) else prior_state.get("status")
        return record
    prior_hash, _window = embed.embedding_input(prior_protocol, "joint", prior_entry, clip_sha)
    if prior_state.get("input_hash") != prior_hash:
        record["reason"] = "cache-hash-mismatch"
        return record
    film_hash, _window = embed.embedding_input(film_protocol, "joint", film_entry, clip_sha)
    record["priorInputHash"] = prior_hash
    record["filmInputHash"] = film_hash
    record["inputHashEquivalent"] = film_hash == prior_hash
    if film_hash != prior_hash:
        record["reason"] = "input-hash-not-equivalent"
        return record
    record["reason"] = "equivalent-checkpoint"
    return record


def audit_prior_windows(film_protocol, prior_protocol, prior_states, clip_shas):
    planned = {span: entry for span, entry in zip(spans(film_protocol["joint_windows"]), film_protocol["joint_windows"])}
    records = []
    for entry in prior_protocol.get("joint_windows") or []:
        span = (int(entry["start_ticks"]), int(entry["end_ticks"]))
        film_entry = planned.get(span)
        if film_entry is None:
            records.append({
                "reused": False,
                "forgedInputHash": False,
                "inputHashEquivalent": False,
                "boundsMatch": False,
                "reason": "tail-boundary-mismatch",
                "priorBounds": [span[0], span[1]],
                "priorCandidateId": entry.get("candidate_id"),
            })
            continue
        records.append(reuse_decision(
            film_protocol, film_entry, prior_protocol, entry,
            prior_states.get(entry["candidate_id"]), clip_shas.get(entry["candidate_id"]),
        ))
    return {
        "reused": sum(1 for record in records if record["reused"]),
        "coincidentNotEquivalent": sum(1 for record in records if record["reason"] == "input-hash-not-equivalent"),
        "tailMismatch": sum(1 for record in records if record["reason"] == "tail-boundary-mismatch"),
        "cacheMismatch": sum(1 for record in records if record["reason"] == "cache-hash-mismatch"),
        "records": records,
        "forgedInputHash": False,
    }


def adopt_equivalent(decision, prior_response, dest_run, film_entry):
    """Copy a response only when a caller asks and the two hashes already match.

    preflight, run, resume, status and import do not call this. Audit records
    the decision and leaves reused false.
    """
    if decision.get("reason") != "equivalent-checkpoint" or not decision.get("inputHashEquivalent"):
        return None
    if decision.get("forgedInputHash"):
        raise BatchBlocked("refusing to forge an input hash", reason="forged-input-hash")
    if decision["priorInputHash"] != decision["filmInputHash"]:
        raise BatchBlocked("equivalent reuse disagreed with the hashes", reason="input-hash-not-equivalent")
    dest = Path(dest_run) / "joint"
    refuse_preserved(dest)
    target = dest / (film_entry["candidate_id"] + ".json")
    state_path = target.with_suffix(".state.json")
    payload = Path(prior_response).read_bytes()
    if target.exists() and target.read_bytes() != payload:
        raise BatchBlocked("destination response changed", reason="cache-hash-mismatch")
    if not target.exists():
        dest.mkdir(parents=True, mode=0o700)
        target.write_bytes(payload)
    state = {
        "candidate_id": film_entry["candidate_id"],
        "status": "succeeded",
        "input_hash": decision["filmInputHash"],
        "response_sha256": file_sha256(target),
        "reused_from": Path(prior_response).name,
        "model_input_reused": True,
        "forgedInputHash": False,
        "reason": "equivalent-checkpoint",
    }
    if state["input_hash"] != decision["priorInputHash"]:
        raise BatchBlocked("refusing to store a different hash", reason="forged-input-hash")
    write_json(state_path, state)
    write_json(dest / (film_entry["candidate_id"] + ".reuse.json"), public_value(decision) | {
        "reused": True, "forgedInputHash": False, "reason": "equivalent-checkpoint",
    })
    return state


def read_prior_run(run):
    run = Path(run)
    protocol = read_json(run / "protocol.json")
    states, clips = {}, {}
    for entry in protocol.get("joint_windows") or []:
        candidate = entry["candidate_id"]
        state_path = run / "joint" / (candidate + ".state.json")
        clip_path = run / "clips" / (candidate + ".clip.json")
        if state_path.is_file():
            states[candidate] = read_json(state_path)
        if clip_path.is_file():
            clips[candidate] = read_json(clip_path).get("sha256")
    return protocol, states, clips


def audit_sample_root(output_root, sample_root):
    """Read sample embed runs and write an audit under the new output only.

    This does not copy responses. reused stays zero on this path.
    """
    sample_root = Path(sample_root)
    before = snapshot_tree(sample_root)
    output_root = Path(output_root)
    manifest = read_json(output_root / "manifest.json")
    by_item = {film["itemId"]: film for film in manifest["films"]}
    summary = {"films": 0, "coincidentNotEquivalent": 0, "tailMismatch": 0,
               "cacheMismatch": 0, "reused": 0, "unmatched": 0, "forgedInputHash": False}
    for run in sorted(path for path in sample_root.iterdir() if path.is_dir() and (path / "protocol.json").is_file()):
        protocol, states, clips = read_prior_run(run)
        item = contract.canonical_item(protocol["source"]["itemId"])
        film = by_item.get(item)
        if film is None:
            summary["unmatched"] += 1
            continue
        film_protocol = read_json(output_root / "films" / film["label"] / "embed" / "protocol.json")
        audit = audit_prior_windows(film_protocol, protocol, states, clips)
        write_json(output_root / "films" / film["label"] / "sample-reuse-audit.json", audit)
        summary["films"] += 1
        for key in ("coincidentNotEquivalent", "tailMismatch", "cacheMismatch", "reused"):
            summary[key] += audit[key]
    after = snapshot_tree(sample_root)
    if after != before:
        raise BatchBlocked("sample artifacts changed during audit", reason="preserved-artifact")
    summary["priorUnchanged"] = True
    return summary


def next_transcription_action(checkpoints):
    prepare = checkpoints.get("prepare")
    if prepare is None:
        return {"action": "run", "stage": "prepare"}
    if prepare != "succeeded":
        reason = "interrupted-not-retried" if prepare in UNKNOWN_STATUSES else "transcription-failed"
        return {"action": "blocked", "stage": "prepare", "reason": reason, "status": prepare}
    chunks = list(checkpoints.get("chunks") or [])
    for stage in ("asr", "align"):
        tasks = checkpoints.get(stage)
        if tasks is None:
            return {"action": "run", "stage": stage}
        seen = []
        for task in tasks:
            chunk_id = task.get("chunk_id")
            if chunk_id in seen:
                return {"action": "blocked", "stage": stage, "reason": "duplicate-checkpoint", "status": task.get("status")}
            seen.append(chunk_id)
            status = task.get("status")
            if status != "succeeded":
                if status == "started":
                    reason = "interrupted-not-retried"
                elif status in UNKNOWN_STATUSES:
                    reason = "transcription-unknown"
                else:
                    reason = "transcription-failed"
                return {"action": "blocked", "stage": stage, "reason": reason, "status": status, "chunk": chunk_id}
        if set(seen) != set(chunks):
            return {"action": "run", "stage": stage}
    documents = checkpoints.get("documents")
    if documents is None:
        return {"action": "run", "stage": "documents"}
    if documents != "succeeded":
        reason = "interrupted-not-retried" if documents in UNKNOWN_STATUSES else "transcription-failed"
        return {"action": "blocked", "stage": "documents", "reason": reason, "status": documents}
    return {"action": "done", "stage": "documents"}


def load_checkpoints(asr_run):
    asr_run = Path(asr_run)
    prepare_path = asr_run / "prepare.json"
    chunks_path = asr_run / "chunks.json"
    documents_path = asr_run / "documents-state.json"
    checkpoints = {
        "prepare": read_json(prepare_path).get("status") if prepare_path.is_file() else None,
        "chunks": [row.get("chunk_id") for row in read_json(chunks_path)] if chunks_path.is_file() else [],
        "asr": read_json(asr_run / "asr-tasks.json") if (asr_run / "asr-tasks.json").is_file() else None,
        "align": read_json(asr_run / "align-tasks.json") if (asr_run / "align-tasks.json").is_file() else None,
        "documents": read_json(documents_path).get("status") if documents_path.is_file() else None,
    }
    return checkpoints


def candidate_hash(protocol, kind, entry, clip_sha=None):
    digest, _window = embed.embedding_input(protocol, kind, entry, clip_sha)
    return digest


def joint_clip_sha(embed_run, entry):
    path = Path(embed_run) / "clips" / (entry["candidate_id"] + ".clip.json")
    if not path.is_file():
        return None
    return read_json(path).get("sha256")


def state_for(embed_run, kind, entry):
    path = Path(embed_run) / kind / (entry["candidate_id"] + ".state.json")
    if not path.is_file():
        return None
    return read_json(path)


def dispatch_entries(entries, lookup_state, send, protocol, kind, clip_sha_for):
    sent, reused = [], []
    for entry in entries:
        state = lookup_state(entry)
        if state is None:
            send(entry)
            sent.append(entry["candidate_id"])
            continue
        status = state.get("status")
        if status != "succeeded":
            raise BatchBlocked(
                "unresolved checkpoint; not sending again",
                reason="not-succeeded", status=status, stage=kind,
            )
        expected = candidate_hash(protocol, kind, entry, None if kind != "joint" else clip_sha_for(entry))
        if state.get("input_hash") != expected:
            raise BatchBlocked("cached input hash does not match this candidate", reason="cache-hash-mismatch", stage=kind)
        reused.append(entry["candidate_id"])
    return {"sent": sent, "reused": reused, "resentUnknown": False}


def publish_transcription(asr_run, embed_run):
    asr_run, embed_run = Path(asr_run), Path(embed_run)
    before = snapshot_tree(asr_run)
    protocol = read_json(asr_run / "protocol.json")
    if protocol.get("fullFilm") is not True or protocol.get("transcription_language_policy") != LANGUAGE_POLICY:
        raise BatchBlocked("transcription protocol is not a whole-film auto pass", reason="not-full-film")
    action = next_transcription_action(load_checkpoints(asr_run))
    if action["action"] != "done":
        raise BatchBlocked("transcription is not complete", reason=action.get("reason") or "transcription-failed", status=action.get("status"))
    candidates = read_json(asr_run / "candidates.json")
    if not isinstance(candidates, list):
        raise BatchBlocked("candidates must be a list", reason="transcription-failed")
    for task_file in ("asr-tasks.json", "align-tasks.json"):
        for task in read_json(asr_run / task_file):
            if task.get("status") != "succeeded":
                raise BatchBlocked("failed transcription cannot be published as empty", reason="transcription-failed", status=task.get("status"))
    check = {
        "status": "succeeded",
        "fullFilm": True,
        "coverage": "transcription-complete",
        "candidateCount": len(candidates),
        "successfulEmpty": len(candidates) == 0,
        "itemId": contract.canonical_item(protocol["source"]["itemId"]),
        "contentVersion": protocol["source"]["contentVersion"],
        "runtimeTicks": int(protocol["source"]["runtimeTicks"]),
        "languagePolicy": LANGUAGE_POLICY,
    }
    write_json(embed_run / "candidates.json", candidates)
    write_json(embed_run / "transcription-check.json", check)
    if snapshot_tree(asr_run) != before:
        raise BatchBlocked("publishing changed the ASR run", reason="preserved-artifact")
    return check


def assess_film(film_dir):
    film_dir = Path(film_dir)
    result = {
        "coverage": "pending", "fullFilm": False, "blocked": False, "successfulEmpty": False,
        "reason": None, "status": None, "stage": "prepare",
        "nextAction": {"action": "run", "stage": "prepare"}, "windows": 0, "documents": 0,
    }
    asr_protocol_path = film_dir / "asr" / "protocol.json"
    if not asr_protocol_path.is_file():
        return result
    asr_body = read_json(asr_protocol_path)
    if asr_body.get("transcription_language_policy") != LANGUAGE_POLICY:
        result.update(coverage="failed", reason="language-policy", stage="asr", blocked=True)
        return result
    if asr_body.get("fullFilm") is not True:
        result.update(coverage="failed", reason="not-full-film", stage="asr", blocked=True)
        return result
    action = next_transcription_action(load_checkpoints(film_dir / "asr"))
    if action["action"] == "blocked":
        coverage = "failed" if action["reason"] == "transcription-failed" else "blocked"
        result.update(coverage=coverage, blocked=True, reason=action["reason"], status=action.get("status"),
                      stage=action["stage"], nextAction={"action": "blocked", "stage": action["stage"]})
        return result
    if action["action"] == "run":
        result.update(stage=action["stage"], nextAction=action)
        return result
    candidates_path = film_dir / "asr" / "candidates.json"
    if not candidates_path.is_file():
        result.update(stage="documents", reason="transcription-failed", coverage="failed", blocked=True,
                      nextAction={"action": "blocked", "stage": "documents"})
        return result
    candidates = read_json(candidates_path)
    result["documents"] = len(candidates)
    result["successfulEmpty"] = len(candidates) == 0
    embed_protocol_path = film_dir / "embed" / "protocol.json"
    if not embed_protocol_path.is_file():
        result.update(stage="publish-transcription", nextAction={"action": "run", "stage": "publish-transcription"})
        return result
    protocol = read_json(embed_protocol_path)
    if protocol.get("fullFilm") is not True:
        result.update(coverage="failed", reason="not-full-film", stage="embed", blocked=True)
        return result
    windows = protocol.get("joint_windows") or []
    result["windows"] = len(windows)
    try:
        planned = plan_film_windows(protocol["source"]["runtimeTicks"])
    except BatchBlocked as exc:
        result.update(coverage="failed", reason=exc.reason, stage="joint", blocked=True)
        return result
    if spans(windows) != planned:
        result.update(coverage="failed", reason="coverage-incomplete", stage="joint", blocked=True)
        return result
    check_path = film_dir / "embed" / "transcription-check.json"
    if not check_path.is_file():
        result.update(stage="publish-transcription", nextAction={"action": "run", "stage": "publish-transcription"})
        return result
    check = read_json(check_path)
    if check.get("status") != "succeeded" or check.get("fullFilm") is not True or int(check.get("candidateCount", -1)) != len(candidates):
        result.update(coverage="failed", reason="transcription-failed", stage="documents", blocked=True, successfulEmpty=False)
        return result
    if len(candidates) == 0 and check.get("successfulEmpty") is not True:
        result.update(coverage="failed", reason="empty-not-successful", stage="documents", blocked=True, successfulEmpty=False)
        return result
    embed_run = film_dir / "embed"
    for entry in windows:
        state = state_for(embed_run, "joint", entry)
        if state is None:
            result.update(stage="joint", nextAction={"action": "run", "stage": "joint"}, successfulEmpty=len(candidates) == 0)
            return result
        if state.get("status") != "succeeded":
            result.update(coverage="blocked", blocked=True, reason="not-succeeded", status=state.get("status"),
                          stage="joint", nextAction={"action": "blocked", "stage": "joint"}, successfulEmpty=False)
            return result
        clip_sha = joint_clip_sha(embed_run, entry)
        if clip_sha is None or state.get("input_hash") != candidate_hash(protocol, "joint", entry, clip_sha):
            result.update(coverage="blocked", blocked=True, reason="cache-hash-mismatch", stage="joint",
                          nextAction={"action": "blocked", "stage": "joint"}, successfulEmpty=False)
            return result
    if candidates:
        for entry in candidates:
            state = state_for(embed_run, "text", entry)
            if state is None:
                result.update(stage="text", nextAction={"action": "run", "stage": "text"}, successfulEmpty=False)
                return result
            if state.get("status") != "succeeded":
                result.update(coverage="blocked", blocked=True, reason="not-succeeded", status=state.get("status"),
                              stage="text", nextAction={"action": "blocked", "stage": "text"}, successfulEmpty=False)
                return result
            if state.get("input_hash") != candidate_hash(protocol, "text", entry):
                result.update(coverage="blocked", blocked=True, reason="cache-hash-mismatch", stage="text",
                              nextAction={"action": "blocked", "stage": "text"}, successfulEmpty=False)
                return result
    result.update(coverage="full", fullFilm=True, successfulEmpty=len(candidates) == 0,
                  nextAction={"action": "done", "stage": "import"}, stage="import", reason=None)
    return result


def step_film(film_dir, *, execute, send):
    assessed = assess_film(film_dir)
    action = assessed["nextAction"]
    if assessed["coverage"] == "full" or action["action"] != "run":
        return assessed
    stage = action["stage"]
    if stage in TRANSCRIBE_STAGES:
        execute(stage)
        return assess_film(film_dir)
    if stage == "publish-transcription":
        publish_transcription(Path(film_dir) / "asr", Path(film_dir) / "embed")
        return assess_film(film_dir)
    if stage in EMBED_STAGES:
        protocol = read_json(Path(film_dir) / "embed" / "protocol.json")
        if stage == "joint":
            entries = protocol["joint_windows"]
        else:
            entries = read_json(Path(film_dir) / "embed" / "candidates.json")
        dispatch_entries(
            entries,
            lambda entry, kind=stage: state_for(Path(film_dir) / "embed", kind, entry),
            send,
            protocol,
            stage,
            lambda entry: joint_clip_sha(Path(film_dir) / "embed", entry),
        )
        return assess_film(film_dir)
    raise BatchBlocked("unknown stage", reason="config", stage=stage)


def transcribe_command(config, run, stage):
    if stage not in TRANSCRIBE_STAGES:
        raise BatchBlocked("not a transcription stage", reason="config", stage=stage)
    worker = str(config["workerPython"])
    return [worker, str(Path(__file__).with_name("full-film-transcribe.py")), "--run", str(run), "--stage", stage]


def execute_command(command, log_path):
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    completed = subprocess.run(command, check=False, capture_output=True)
    with log_path.open("ab") as handle:
        handle.write(completed.stdout)
        handle.write(completed.stderr)
    return completed.returncode


def default_send(film_dir, stage, entry):
    protocol = read_json(Path(film_dir) / "embed" / "protocol.json")
    if int(protocol.get("max_inflight_embeddings", 0)) != MODEL_CONCURRENCY:
        raise BatchBlocked("model concurrency must stay 1", reason="concurrency")
    if not protocol.get("probe_facts"):
        raise BatchBlocked("probe facts are missing", reason="probe-missing", stage=stage)
    embed.dispatch(Path(film_dir) / "embed", entry, protocol, stage)


def preflight(config, *, check_model=False):
    require_config(config)
    output = Path(config["outputRoot"])
    refuse_preserved(output)
    for key, kind in (("workerPython", "file"), ("asrModel", "dir"), ("alignerModel", "dir"),
                      ("sourceDirectory", "dir"), ("sourceFacts", "file")):
        path = Path(config[key])
        if kind == "file" and not path.is_file() or kind == "dir" and not path.is_dir():
            raise BatchBlocked("required path is missing", reason="config")
    found = match_facts(config["sourceDirectory"], load_facts(config["sourceFacts"]))
    eligible, ineligible = [], []
    for _path, fact in found["matched"]:
        try:
            plan_film_windows(fact["source"]["runtimeTicks"])
        except BatchBlocked:
            ineligible.append(contract.canonical_item(fact["source"]["itemId"]))
            continue
        eligible.append(fact)
    labels = [fact.get("label") for fact in eligible]
    if any(labels) and not all(labels):
        raise BatchBlocked("frozen labels must cover every eligible film", reason="source-mismatch")
    if all(labels):
        eligible.sort(key=lambda fact: int(fact["label"][1:]))
    else:
        eligible.sort(key=lambda fact: contract.canonical_item(fact["source"]["itemId"]))
    films = [build_film_record(index, fact, config) for index, fact in enumerate(eligible, start=1)]
    expected = config.get("expectedFilmCount")
    ready = not found["missingFiles"] and not found["unmatchedFacts"] and not found["drift"] and not ineligible
    if expected is not None and int(expected) != len(films):
        ready = False
    public_films = []
    if ready and films:
        public_films = prepare_layout(config, films)
        if config.get("sampleEmbedRoot"):
            audit = audit_sample_root(config["outputRoot"], config["sampleEmbedRoot"])
        else:
            audit = {"films": 0, "reused": 0, "coincidentNotEquivalent": 0, "tailMismatch": 0,
                     "cacheMismatch": 0, "unmatched": 0, "priorUnchanged": True, "forgedInputHash": False}
    else:
        audit = {"films": 0, "reused": 0, "coincidentNotEquivalent": 0, "tailMismatch": 0,
                 "cacheMismatch": 0, "unmatched": 0, "priorUnchanged": True, "forgedInputHash": False}
    if check_model:
        embed.validate_signature(embed.get(contract.ISOLATED_BASE, "/v1/models")["data"][0], signature_for(config), metadata=True)
    summary = {
        "ready": ready,
        "films": len(films),
        "skippedFiles": found["skipped"],
        "missingFiles": found["missingFiles"],
        "unmatchedFacts": len(found["unmatchedFacts"]),
        "sourceDrift": len(found["drift"]),
        "ineligible": len(ineligible),
        "windows": sum(film["windows"] for film in films),
        "runtimeTicks": sum(int(film["source"]["runtimeTicks"]) for film in films),
        "sampleReuse": public_value(audit),
        "coverage": "pending",
        "modelConcurrency": MODEL_CONCURRENCY,
        "embeddingBaseUrl": contract.ISOLATED_BASE,
        "processingId": EXPECTED_PROCESSING_ID,
        "languagePolicy": LANGUAGE_POLICY,
        "catalog": public_films,
    }
    if ready:
        write_json(Path(config["outputRoot"]) / "preflight.json", summary)
    return summary


def status_summary(output_root):
    output_root = Path(output_root)
    manifest = read_json(output_root / "manifest.json")
    films = []
    for record in manifest["films"]:
        assessed = assess_film(output_root / "films" / record["label"])
        films.append({
            "label": record["label"],
            "itemId": record["itemId"],
            "coverage": assessed["coverage"],
            "fullFilm": assessed["fullFilm"],
            "stage": assessed["stage"],
            "reason": assessed["reason"],
            "status": assessed["status"],
            "windows": assessed["windows"] or record["windows"],
            "documents": assessed["documents"],
            "successfulEmpty": assessed["successfulEmpty"],
        })
    full = sum(1 for film in films if film["coverage"] == "full")
    failed = sum(1 for film in films if film["coverage"] == "failed")
    blocked = sum(1 for film in films if film["coverage"] == "blocked")
    summary = {
        "ready": True,
        "total": len(films),
        "full": full,
        "failed": failed,
        "blocked": blocked,
        "pending": len(films) - full - failed - blocked,
        "coverage": "full" if films and full == len(films) else "pending",
        "films": films,
        "modelConcurrency": MODEL_CONCURRENCY,
    }
    write_json(output_root / "progress.json", summary)
    return summary


def run_batch(config, *, execute=None, send=None):
    summary = preflight(config, check_model=False)
    if not summary["ready"]:
        return 1, summary
    output = Path(config["outputRoot"])
    if execute is None:
        def execute(label, stage):
            film_asr = output / "films" / label / "asr"
            return execute_command(transcribe_command(config, film_asr, stage), output / "films" / label / "logs" / (stage + ".log"))
    if send is None:
        def send(label, stage, entry):
            default_send(output / "films" / label, stage, entry)
    manifest = read_json(output / "manifest.json")
    stopped = None
    for record in manifest["films"]:
        film_dir = output / "films" / record["label"]
        for _step in range(12):
            assessed = assess_film(film_dir)
            action = assessed["nextAction"]
            if assessed["coverage"] == "full" or action["action"] != "run":
                if assessed["blocked"] and assessed["reason"] in {"not-succeeded", "transcription-unknown", "interrupted-not-retried"}:
                    stopped = record["label"]
                break
            stage = action["stage"]
            try:
                if stage in TRANSCRIBE_STAGES:
                    code = execute(record["label"], stage)
                    assessed = assess_film(film_dir)
                    if assessed["reason"] in {"transcription-unknown", "interrupted-not-retried"} or assessed["status"] in UNKNOWN_STATUSES:
                        stopped = record["label"]
                        break
                    if code != 0 or assessed["blocked"] or assessed["nextAction"]["stage"] == stage:
                        break
                elif stage == "publish-transcription":
                    publish_transcription(film_dir / "asr", film_dir / "embed")
                else:
                    protocol = read_json(film_dir / "embed" / "protocol.json")
                    entries = protocol["joint_windows"] if stage == "joint" else read_json(film_dir / "embed" / "candidates.json")
                    dispatch_entries(
                        entries,
                        lambda entry, kind=stage: state_for(film_dir / "embed", kind, entry),
                        lambda entry, label=record["label"], kind=stage: send(label, kind, entry),
                        protocol, stage, lambda entry: joint_clip_sha(film_dir / "embed", entry),
                    )
            except BatchBlocked as exc:
                if exc.reason == "not-succeeded" or exc.status in UNKNOWN_STATUSES or exc.reason in {"transcription-unknown", "interrupted-not-retried"}:
                    stopped = record["label"]
                break
        if stopped:
            break
    progress = status_summary(output)
    if stopped:
        progress["stopped"] = stopped
        write_json(output / "progress.json", progress)
        return 3, progress
    if progress["full"] != progress["total"]:
        return 2, progress
    return 0, progress


def load_film_rows(film_dir, root_signature):
    assessed = assess_film(film_dir)
    if assessed["coverage"] != "full" or assessed["fullFilm"] is not True:
        raise BatchBlocked("film is not a completed whole-film coverage", reason=assessed["reason"] or "coverage-incomplete",
                           status=assessed["status"], stage=assessed["stage"], label=Path(film_dir).name)
    embed_run = Path(film_dir) / "embed"
    before = (embed_run / "protocol.json").read_bytes()
    protocol = read_json(embed_run / "protocol.json")
    try:
        if contract.signature_tuple(protocol["signature"]) != contract.signature_tuple(root_signature):
            raise BatchBlocked("film signature does not match the batch", reason="signature-mismatch", label=protocol.get("label"))
    except contract.ContractError as exc:
        raise BatchBlocked("film signature does not match the batch", reason="signature-mismatch", label=protocol.get("label")) from exc
    try:
        contract.checked_window(protocol["window"])
    except contract.ContractError as exc:
        raise BatchBlocked("film window is not the 12-second contract", reason="window-mismatch", label=protocol.get("label")) from exc
    if spans(protocol["joint_windows"]) != plan_film_windows(protocol["source"]["runtimeTicks"]):
        raise BatchBlocked("joint windows do not cover the runtime", reason="coverage-incomplete")
    check = read_json(embed_run / "transcription-check.json")
    candidates = read_json(embed_run / "candidates.json")
    if check.get("successfulEmpty") is True and candidates:
        raise BatchBlocked("empty transcription claim has text rows", reason="empty-not-successful")
    if not candidates and check.get("successfulEmpty") is not True:
        raise BatchBlocked("failed transcription cannot be imported as empty", reason="transcription-failed")
    rooted = dict(protocol)
    rooted["signature"] = root_signature
    joint_rows = []
    for entry in protocol["joint_windows"]:
        joint_rows.append(film_import.candidate_row(entry, protocol["source"], film_import.verified_vector(embed_run, entry, rooted, "joint")))
    text_rows = []
    reused = 0
    runtime = int(protocol["source"]["runtimeTicks"])
    for entry in candidates:
        start, end = int(entry["start_ticks"]), int(entry["end_ticks"])
        if not 0 <= start < end <= runtime:
            raise BatchBlocked("text candidate is outside the film", reason="coverage-incomplete")
        state = state_for(embed_run, "text", entry)
        if state.get("status") != "succeeded":
            raise BatchBlocked("text checkpoint is not succeeded", reason="not-succeeded", status=state.get("status"))
        reused += int(bool(state.get("model_input_reused")))
        vector = film_import.verified_vector(embed_run, entry, rooted, "text")
        cloned = dict(entry)
        cloned["candidate_id"] = sample_import.index_text_id(protocol["source"]["itemId"], entry["candidate_id"])
        text_rows.append(film_import.candidate_row(cloned, protocol["source"], vector, entry.get("text")))
    if (embed_run / "protocol.json").read_bytes() != before:
        raise BatchBlocked("embed protocol changed during import", reason="preserved-artifact")
    return {"protocol": protocol, "source": protocol["source"], "joint_rows": joint_rows,
            "text_rows": text_rows, "reused_text": reused, "label": protocol["label"]}


def write_index(template, films, references, signature, query_version, output):
    refuse_live_index(template)
    refuse_preserved(output)
    output = Path(output)
    if output.exists():
        raise BatchBlocked("choose a new import output", reason="output-exists")
    try:
        template = sample_import.require_template(template)
    except contract.ContractError as exc:
        raise BatchBlocked(str(exc), reason="import-rejected") from exc
    refuse_live_index(template)
    source_db = sqlite3.connect(f"file:{template}?mode=ro", uri=True)
    try:
        try:
            sample_import.assert_empty_template(source_db)
        except contract.ContractError as exc:
            raise BatchBlocked(str(exc), reason="import-rejected") from exc
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
                if film["text_rows"]:
                    if text_prior is None:
                        text_prior = {
                            "channel": "text", "candidates": [], "model": signature["id"],
                            "revision": signature["revision"], "processingId": signature["processing_id"],
                            "dimension": 768, "normalized": True,
                            "inputVersion": "transcript-document-cache-v1", "embeddingTask": "document",
                        }
                    text_prior, _, _ = film_import.build_bundle(
                        text_prior, film["source"], film["text_rows"], references, query_version, signature)
                joint_prior, _, _ = film_import.build_bundle(
                    joint_prior, film["source"], film["joint_rows"], references, query_version, signature)
                source = film["source"]
                connection.execute("""INSERT INTO media VALUES (?, ?, 1, ?, ?, ?, 'complete', ?, 1, ?)""", (
                    contract.canonical_item(source["itemId"]), source["mediaSourceId"],
                    contract.canonical_item(source["libraryId"]), source["contentVersion"],
                    int(source["runtimeTicks"]), source["sourcePath"], int(source["audioStreamIndex"])))
            if text_prior is None:
                text_prior = sample_import.empty_text_bundle(signature, query_version)
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
                    segment = film_import.native_segment_id(item, source["mediaSourceId"], source["contentVersion"], start, end, 1)
                    blob = struct.pack("<768f", *row["vector"])
                    connection.execute("""INSERT INTO windows
                        (segment_id, item_id, media_source_id, generation, content_version,
                         start_ticks, end_ticks, state, vector, attempt)
                        VALUES (?, ?, ?, 1, ?, ?, ?, 'succeeded', ?, 1)""", (
                        segment, item, source["mediaSourceId"], source["contentVersion"], start, end, blob))
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BatchBlocked("new index failed integrity check", reason="import-rejected")
        if connection.execute("SELECT COUNT(*) FROM media WHERE state!='complete'").fetchone()[0] != 0:
            raise BatchBlocked("imported media must be complete", reason="coverage-incomplete")
        if connection.execute("SELECT window_version, processing_id FROM signatures").fetchone() != (contract.VERSION, signature["processing_id"]):
            raise BatchBlocked("index signature does not match the 12-second model", reason="signature-mismatch")
        evidence = {
            "films": [{
                "label": film["label"],
                "itemId": contract.canonical_item(film["source"]["itemId"]),
                "contentVersion": film["source"]["contentVersion"],
                "runtimeTicks": int(film["source"]["runtimeTicks"]),
                "windows": len(film["joint_rows"]),
                "documents": len(film["text_rows"]),
                "state": "complete",
            } for film in films],
            "fullFilm": True,
            "coverage": "full",
            "templateCopied": True,
            "priorIndexOpened": False,
            "liveHostIndexOpened": False,
            "algorithm": film_import.ALGORITHM,
            "windowVersion": contract.VERSION,
            "embeddingBaseUrl": contract.ISOLATED_BASE,
            "processingId": signature["processing_id"],
            "reusedTextInputs": sum(film["reused_text"] for film in films),
            "newModelInputsDuringImport": 0,
            "manualPause": True,
            "importedCandidatesOnly": True,
            "mixedTenSecondIndex": False,
            "targetName": target.name,
        }
        write_json(output / "import-evidence.json", evidence)
        return evidence
    finally:
        connection.close()


def import_films(output_root, template, references, import_output, *, allow_partial=False):
    output_root = Path(output_root)
    manifest = read_json(output_root / "manifest.json")
    try:
        signature = contract.require_signature(manifest["signature"])
    except contract.ContractError as exc:
        raise BatchBlocked(str(exc), reason="signature-mismatch") from exc
    if signature["processing_id"] != EXPECTED_PROCESSING_ID:
        raise BatchBlocked("batch signature is not the frozen fingerprint", reason="signature-mismatch")
    try:
        contract.checked_window(manifest["window"])
    except contract.ContractError as exc:
        raise BatchBlocked("batch window is not the 12-second contract", reason="window-mismatch") from exc
    try:
        contract.require_isolated_base(manifest.get("embedding_base_url"))
    except contract.ContractError as exc:
        raise BatchBlocked("batch model port is 18784", reason="wrong-port") from exc
    loaded, omitted = [], []
    for record in manifest["films"]:
        film_dir = output_root / "films" / record["label"]
        try:
            if not same_source(record["source"], read_json(film_dir / "embed" / "protocol.json")["source"]):
                raise BatchBlocked("film source does not match the manifest", reason="source-mismatch", label=record["label"])
            loaded.append(load_film_rows(film_dir, signature))
        except BatchBlocked as exc:
            if not allow_partial:
                raise
            omitted.append({"label": record["label"], "itemId": record["itemId"], "reason": exc.reason, "status": exc.status})
    if not loaded:
        raise BatchBlocked("no complete film to import", reason="coverage-incomplete")
    try:
        vectors, query_version = sample_import.load_references(references, signature)
    except contract.ContractError as exc:
        raise BatchBlocked(str(exc), reason="import-rejected") from exc
    evidence = write_index(template, loaded, vectors, signature, query_version, import_output)
    evidence["omitted"] = omitted
    evidence["batchCoverage"] = "full" if not omitted else "partial"
    if omitted:
        evidence["coverage"] = "partial"
        evidence["fullFilm"] = False
    write_json(Path(import_output) / "import-evidence.json", evidence)
    return evidence


def command_preflight(args):
    summary = preflight(load_config(args.config), check_model=args.check_model)
    print(json.dumps(public_value(summary), ensure_ascii=False, indent=2))
    return 0 if summary["ready"] else 1


def command_run(args):
    code, summary = run_batch(load_config(args.config))
    print(json.dumps(public_value(summary), ensure_ascii=False, indent=2))
    return code


def command_status(args):
    config = load_config(args.config)
    summary = status_summary(config["outputRoot"])
    print(json.dumps(public_value(summary), ensure_ascii=False, indent=2))
    return 0 if summary["coverage"] == "full" else 2


def command_import(args):
    config = load_config(args.config)
    destination = args.output or config.get("importOutput")
    if not destination:
        raise BatchBlocked("import output is missing", reason="config")
    evidence = import_films(
        config["outputRoot"], config["template"], config["references"], destination,
        allow_partial=args.allow_partial,
    )
    print(json.dumps(public_value(evidence), ensure_ascii=False, indent=2))
    return 0 if evidence["batchCoverage"] == "full" else 2


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, required=True)
    parser = argparse.ArgumentParser(description="一次性整片 12 秒批处理。不启动队列，不推理除非进入 run。")
    sub = parser.add_subparsers(dest="command", required=True)
    preflight_parser = sub.add_parser("preflight", parents=[common])
    preflight_parser.add_argument("--check-model", action="store_true")
    sub.add_parser("run", parents=[common])
    sub.add_parser("resume", parents=[common])
    sub.add_parser("status", parents=[common])
    import_parser = sub.add_parser("import", parents=[common])
    import_parser.add_argument("--output", type=Path)
    import_parser.add_argument("--allow-partial", action="store_true")
    return parser


def main(argv=None):
    os.umask(0o077)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            return command_preflight(args)
        if args.command in {"run", "resume"}:
            return command_run(args)
        if args.command == "status":
            return command_status(args)
        return command_import(args)
    except BatchBlocked as exc:
        print(json.dumps({"ready": False, "reason": exc.reason, "stage": exc.stage, "status": exc.status},
                         ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
