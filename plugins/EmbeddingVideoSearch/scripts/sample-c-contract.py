#!/usr/bin/env python3
"""12-second sample contract for issue 3 option C.

Each film keeps its real itemId, contentVersion, path and runtime.
The indexed span is one continuous 120-second excerpt at the original
timeline. ASR runs stay on the private 120-second wav and are offset
back with provenance.absoluteOffsetTicks. This script does not embed,
transcribe, or call a model.
"""
import argparse
from decimal import Decimal, ROUND_CEILING, ROUND_UP
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid


VERSION = "12s-10s-4f70-tail-v1"
PROFILE = "jellyfin-12s-4f"
LENGTH_TICKS = 120_000_000
STEP_TICKS = 100_000_000
SAMPLE_TICKS = 1_200_000_000
SAMPLED_SECONDS = [0, 3, 6, 9]
VIDEO_TAIL_POLICY = "video-tail-hold-last-frame-200ms-v1"
VIDEO_TAIL_LIMIT = Decimal("0.200")
MAX_SOFT_TOKENS = 70
MODEL_ID = "google/embeddinggemma-2"
REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
INPUT_VERSION = "joint-x264-veryfast-crf28-yuv420p-aac-16000-mono-v1"
AUDIO_VERSION = "source-default-audio-index-v1"
ISOLATED_BASE = "http://127.0.0.1:18784"
REJECTED_PORTS = (18780, 18783)
PROTECTED_NAMES = {"jellyfin-full-film-20261016", "full-film-20261016", "asr-C2-rejected-audio-v1"}
PLACEHOLDERS = {"", "pending", "tbd", "todo", "unknown", "changeme", "processing_id", "none", "null"}
SOURCE_FIELDS = (
    "itemId", "mediaSourceId", "libraryId", "contentVersion", "runtimeTicks",
    "container", "audioStreamIndex", "sourceItemId", "sourcePath", "sizeBytes", "modifiedTicks",
)


class ContractError(AssertionError):
    pass


def milliseconds(seconds):
    return int(round(float(seconds) * 1000))


def clip_duration_accepted(seconds):
    if seconds != seconds:
        return False
    delta = abs(milliseconds(seconds) - 12_000)
    if delta > 150:
        return False
    return all(milliseconds(seconds) > offset * 1000 for offset in SAMPLED_SECONDS)


def audio_covers_clip(audio_seconds, clip_seconds):
    if audio_seconds != audio_seconds or clip_seconds != clip_seconds:
        return False
    return milliseconds(clip_seconds) - milliseconds(audio_seconds) <= 150


def validate_joint_processing(processing):
    """Accept jellyfin-12s-4f. The response has no fps field."""
    modalities = set(processing.get("modalities") or [])
    if modalities != {"video", "audio"}:
        raise ContractError("12s joint response must contain video and audio")
    video = processing.get("video") or {}
    if "fps" in video:
        raise ContractError("12s metadata has no fps field")
    if video.get("audio_included") is not True or video.get("has_audio") is not True:
        raise ContractError("12s joint keeps the audio track")
    if video.get("sampled_seconds") != SAMPLED_SECONDS or video.get("offsets_seconds") != SAMPLED_SECONDS:
        raise ContractError("12s samples must stay exactly [0, 3, 6, 9]")
    if video.get("frame_count") != 4:
        raise ContractError("12s frame_count must be 4")
    if video.get("profile") != PROFILE or video.get("max_soft_tokens") != MAX_SOFT_TOKENS:
        raise ContractError("12s profile or visual budget mismatch")
    if video.get("add_timestamps") is not False or video.get("overflow_strategy") != "reject":
        raise ContractError("12s timestamps stay off and overflow is rejected")
    if video.get("window_seconds") != 12:
        raise ContractError("12s window_seconds must be 12")
    duration = video.get("duration_seconds")
    if not clip_duration_accepted(duration):
        raise ContractError("clip duration must stay within 12 seconds plus or minus 0.15")
    audio = processing.get("audio") or {}
    if audio.get("sample_rate") != 16000 or audio.get("channels") != 1:
        raise ContractError("decoded audio must be 16 kHz mono")
    if audio.get("truncated") is not False or audio.get("coverage") != "full_track":
        raise ContractError("12s audio must keep the full track")
    heard = audio.get("duration_seconds")
    if not audio_covers_clip(heard, duration):
        raise ContractError("decoded audio is shorter than the clip by more than 0.15 seconds")
    return True


def checked_window(window):
    if not isinstance(window, dict) or window.get("version") != VERSION:
        raise ContractError("window version must be the 12-second profile")
    if window.get("length_ticks") != LENGTH_TICKS or window.get("step_ticks") != STEP_TICKS:
        raise ContractError("12-second length or stride does not match the frozen profile")
    if window.get("sampled_seconds") != SAMPLED_SECONDS or window.get("frame_interval_seconds") != 3:
        raise ContractError("protocol sampling must be four frames at 0, 3, 6 and 9 seconds")
    if "fps" in window:
        raise ContractError("12-second profile has no fps field")
    if window.get("max_soft_tokens") != MAX_SOFT_TOKENS or window.get("add_timestamps") is not False:
        raise ContractError("protocol visual budget or timestamps do not match")
    if window.get("profile") != PROFILE:
        raise ContractError("protocol profile must be jellyfin-12s-4f")
    return window


def window_template():
    return {
        "version": VERSION, "length_ticks": LENGTH_TICKS, "step_ticks": STEP_TICKS,
        "sampled_seconds": SAMPLED_SECONDS, "frame_interval_seconds": 3,
        "max_soft_tokens": MAX_SOFT_TOKENS, "add_timestamps": False, "profile": PROFILE,
    }


def require_isolated_base(url):
    text = str(url or "").rstrip("/")
    for port in REJECTED_PORTS:
        if text.endswith(":" + str(port)) or (":" + str(port) + "/") in text + "/":
            raise ContractError("12-second sample must not use port " + str(port))
    if text != ISOLATED_BASE:
        raise ContractError("embedding_base_url must be http://127.0.0.1:18784")
    return text


def require_signature(signature):
    if not isinstance(signature, dict):
        raise ContractError("signature is missing; copy processing_id from the live 18784 service")
    processing = signature.get("processing_id")
    if not isinstance(processing, str) or processing.strip().lower() in PLACEHOLDERS or any(mark in processing for mark in "<> "):
        raise ContractError("processing_id must be copied from the live 18784 /v1/models info")
    if signature.get("id") != MODEL_ID or signature.get("revision") != REVISION:
        raise ContractError("model id or revision does not match the frozen weights")
    if signature.get("dimension") != 768 or signature.get("normalized") is not True:
        raise ContractError("signature must stay 768-dimensional and normalized")
    return signature


def signature_template(processing_id=None):
    if processing_id is not None:
        return require_signature({
            "id": MODEL_ID, "revision": REVISION, "processing_id": processing_id,
            "dimension": 768, "normalized": True,
        })
    return {
        "id": MODEL_ID, "revision": REVISION, "processing_id": None,
        "dimension": 768, "normalized": True,
        "note": "Copy processing_id from the live http://127.0.0.1:18784/v1/models info. An offline report is not authority if ffmpeg changes the id.",
    }


def signature_tuple(signature):
    require_signature(signature)
    return (signature["id"], signature["revision"], signature["processing_id"],
            int(signature["dimension"]), signature["normalized"] is True)


def canonical_item(value):
    return str(uuid.UUID(str(value)))


def same_item(left, right):
    return canonical_item(left) == canonical_item(right)


def require_sample_span(start, end, runtime):
    start, end, runtime = int(start), int(end), int(runtime)
    if end <= start:
        raise ContractError("sample end must be after its start")
    duration = end - start
    if duration < LENGTH_TICKS:
        raise ContractError(
            "sample is " + str(duration) + " ticks, shorter than 12 seconds; choose a sample of at least 12 seconds"
        )
    if duration != SAMPLE_TICKS:
        raise ContractError("this acceptance froze a continuous 120 second sample")
    if start < 0 or end > runtime:
        raise ContractError("sample span is outside the original runtime")
    if runtime <= SAMPLE_TICKS or (start == 0 and end == runtime):
        raise ContractError("refusing to treat the 120 second excerpt as the whole film")
    return start, end


def plan_local(duration):
    duration = int(duration)
    if duration < LENGTH_TICKS:
        raise ContractError("sample is shorter than 12 seconds")
    if duration == LENGTH_TICKS:
        return [(0, duration)]
    windows = []
    start = 0
    while start + LENGTH_TICKS < duration:
        windows.append((start, start + LENGTH_TICKS))
        start += STEP_TICKS
    tail = duration - LENGTH_TICKS
    if not windows or windows[-1][0] != tail:
        windows.append((tail, duration))
    if windows[0] != (0, LENGTH_TICKS) or windows[-1] != (tail, duration):
        raise ContractError("12-second windows do not cover the excerpt")
    for earlier, later in zip(windows, windows[1:]):
        if not earlier[0] < later[0] <= earlier[1]:
            raise ContractError("12-second windows leave a gap or go backwards")
        if later[1] - later[0] != LENGTH_TICKS or earlier[1] - earlier[0] != LENGTH_TICKS:
            raise ContractError("a 12-second window was shortened")
    return windows


def plan_sample(start, end, runtime):
    start, end = require_sample_span(start, end, runtime)
    return [(start + local_start, start + local_end) for local_start, local_end in plan_local(end - start)]


def window_entries(source, start, end):
    item = canonical_item(source["itemId"]).replace("-", "")
    entries = []
    for index, (window_start, window_end) in enumerate(plan_sample(start, end, source["runtimeTicks"])):
        entries.append({
            "candidate_id": f"joint-{item}-{index:04d}",
            "start_ticks": window_start,
            "end_ticks": window_end,
            "item_id": canonical_item(source["itemId"]),
            "content_version": source["contentVersion"],
            "source_path": source["sourcePath"],
            "full_film": False,
        })
    return entries


def ticks_to_seconds(ticks):
    seconds, remainder = divmod(int(ticks), 10_000_000)
    if remainder == 0:
        return str(seconds)
    return f"{seconds}.{remainder:07d}"


def _is_attached_picture(stream):
    if not isinstance(stream, dict):
        return False
    disposition = stream.get("disposition") or {}
    if not isinstance(disposition, dict):
        disposition = {}
    if disposition.get("attached_pic") in (1, True):
        return True
    return stream.get("codec_name") == "mjpeg"


def _program_video(probe):
    """The one real picture, plus cover-art indexes. Covers are not pictures."""
    pictures = []
    attached = []
    for stream in list((probe or {}).get("streams") or []):
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        if _is_attached_picture(stream):
            try:
                attached.append(int(stream["index"]))
            except (TypeError, ValueError, KeyError) as exc:
                raise ContractError("attached picture stream is missing") from exc
            continue
        pictures.append(stream)
    if not pictures:
        raise ContractError("program video stream is missing")
    if len(pictures) != 1:
        raise ContractError("more than one program video stream")
    video = pictures[0]
    if video.get("codec_name") in (None, ""):
        raise ContractError("program video codec is missing")
    try:
        video_index = int(video["index"])
    except (TypeError, ValueError, KeyError) as exc:
        raise ContractError("program video stream is missing") from exc
    return video, video_index, attached


def check_source_probe(probe, audio_stream_index):
    video, video_index, attached = _program_video(probe)
    try:
        audio_index = int(audio_stream_index)
    except (TypeError, ValueError) as exc:
        raise ContractError("audio map must be the absolute audio stream") from exc
    streams = list((probe or {}).get("streams") or [])
    audio = next((stream for stream in streams if stream.get("index") == audio_index), None)
    if audio is None or audio.get("codec_type") != "audio":
        raise ContractError("audio map must be the absolute audio stream")
    # Index 0 keeps the historical map so existing clip identities still match.
    video_map = "0:v:0" if video_index == 0 else f"0:{video_index}"
    return {
        "video_map": video_map,
        "video_stream_index": video_index,
        "audio_map": f"0:{audio_index}",
        "video_start_time_raw": video.get("start_time"),
        "video_start_added": False,
        "time_origin": "container",
        "attached_picture_stream_indexes": attached,
    }


def film_source(sample):
    source = sample.get("source")
    if not isinstance(source, dict):
        raise ContractError("sample.source from admin/source-facts is missing")
    for key in SOURCE_FIELDS:
        if key not in source:
            raise ContractError("source-facts field missing: " + key)
    if str(source["contentVersion"]).startswith("sample-pcm-"):
        raise ContractError("film contentVersion must come from admin/source-facts, not the excerpt wav")
    if not same_item(source["itemId"], source["sourceItemId"]):
        raise ContractError("itemId and sourceItemId differ")
    if str(source["sourcePath"]) != str(sample["path"]):
        raise ContractError("sourcePath does not match the selected sample path")
    runtime = int(source["runtimeTicks"])
    start, end = require_sample_span(sample["sampleStartTicks"], sample["sampleEndTicks"], runtime)
    matches = sample.get("databaseMatches") or []
    if matches:
        match = matches[0]
        if not same_item(match["id"], source["itemId"]) or str(match["path"]) != str(source["sourcePath"]):
            raise ContractError("database match does not agree with source-facts")
        if int(match["runtimeTicks"]) != runtime:
            raise ContractError("database runtime does not agree with source-facts")
    label = str(sample.get("label") or "")
    if re.fullmatch(r"C\d+", label) is None:
        raise ContractError("sample label must look like C1")
    check_source_probe(sample.get("probe"), source["audioStreamIndex"])
    return {
        "label": label,
        "itemId": canonical_item(source["itemId"]),
        "mediaSourceId": str(source["mediaSourceId"]),
        "libraryId": canonical_item(source["libraryId"]),
        "contentVersion": str(source["contentVersion"]),
        "runtimeTicks": runtime,
        "container": str(source["container"]),
        "audioStreamIndex": int(source["audioStreamIndex"]),
        "sourceItemId": canonical_item(source["sourceItemId"]),
        "sourcePath": str(source["sourcePath"]),
        "sizeBytes": int(source["sizeBytes"]),
        "modifiedTicks": int(source["modifiedTicks"]),
        "sampleStartTicks": start,
        "sampleEndTicks": end,
        "presence": source.get("presence"),
    }


def read_asr_provenance(asr_run):
    protocol_path = Path(asr_run) / "protocol.json"
    if not protocol_path.is_file():
        raise ContractError("ASR protocol is missing: " + str(protocol_path))
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    provenance = protocol.get("provenance") or {}
    for key in ("absoluteOffsetTicks", "sampleEndTicks", "originalSourcePath", "originalItemId"):
        if key not in provenance:
            raise ContractError("ASR provenance." + key + " is missing")
    excerpt = protocol.get("source") or {}
    if int(excerpt.get("runtimeTicks") or 0) != SAMPLE_TICKS:
        raise ContractError("ASR runtimeTicks must stay the 120 second excerpt")
    if not str(excerpt.get("contentVersion") or "").startswith("sample-pcm-sha256:"):
        raise ContractError("ASR contentVersion must stay the excerpt hash")
    if "audioStreamIndex" not in excerpt or excerpt["audioStreamIndex"] is None:
        raise ContractError("excerpt wav audioStreamIndex is missing")
    if int(excerpt["audioStreamIndex"]) != 0:
        raise ContractError("excerpt wav audio stream must be index 0")
    return provenance


def refuse_write_target(path):
    path = Path(path)
    if path.name.startswith("asr-") or path.name in PROTECTED_NAMES:
        raise ContractError("refusing to write into an ASR run or an accepted cache: " + path.name)
    for parent in [path, *path.parents]:
        if parent.name in PROTECTED_NAMES or parent.name.startswith("asr-"):
            raise ContractError("refusing to write inside " + parent.name)


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def interface_template():
    return {
        "version": "issue3-c-sample-v1",
        "fullFilm": False,
        "coverage": "partial",
        "window": window_template(),
        "embedding_base_url": ISOLATED_BASE,
        "max_inflight_embeddings": 1,
        "signature": signature_template(None),
        "samples": "selected-samples.json with source from admin/source-facts",
        "asrProvenance": "protocol.provenance.absoluteOffsetTicks on the private 120 second wav",
        "confirmedSpans": {
            "C1": [100_000_000, 1_300_000_000],
            "C2": [6_500_000_000, 7_700_000_000],
            "C3": [36_700_000_000, 37_900_000_000],
        },
        "windowsPerFilm": 12,
        "referenceQueries": 16,
        "algorithm": "reference16-logistic-tail-f64-v1",
    }


def prepare_runs(samples_path, asr_root, dest_root, processing_id=None):
    refuse_write_target(dest_root)
    dest_root = Path(dest_root)
    if dest_root.exists():
        raise ContractError("embed destination must be a new directory")
    samples = json.loads(Path(samples_path).read_text(encoding="utf-8"))
    if not isinstance(samples, list) or not samples:
        raise ContractError("selected samples must be a non-empty list")
    signature = signature_template(processing_id)
    window = checked_window(window_template())
    seen = set()
    films = []
    written = []
    for sample in samples:
        film = film_source(sample)
        if film["itemId"] in seen or film["label"] in {item["label"] for item in films}:
            raise ContractError("duplicate sample")
        seen.add(film["itemId"])
        asr_run = Path(asr_root) / ("asr-" + film["label"])
        provenance = read_asr_provenance(asr_run)
        if int(provenance["absoluteOffsetTicks"]) != film["sampleStartTicks"]:
            raise ContractError(film["label"] + " absoluteOffsetTicks does not match sampleStartTicks")
        if int(provenance["sampleEndTicks"]) != film["sampleEndTicks"]:
            raise ContractError(film["label"] + " ASR sample end does not match the frozen span")
        if not same_item(provenance["originalItemId"], film["itemId"]):
            raise ContractError(film["label"] + " ASR item does not match source-facts")
        if str(provenance["originalSourcePath"]) != film["sourcePath"]:
            raise ContractError(film["label"] + " ASR path does not match source-facts")
        source = {key: film[key] for key in SOURCE_FIELDS}
        windows = window_entries(source, film["sampleStartTicks"], film["sampleEndTicks"])
        if len(windows) != 12:
            raise ContractError("a 120 second excerpt must plan 12 joint windows")
        protocol = {
            "version": "issue3-c-sample-film-v1",
            "fullFilm": False,
            "coverage": "partial",
            "label": film["label"],
            "window": window,
            "embedding_base_url": ISOLATED_BASE,
            "max_inflight_embeddings": 1,
            "signature": signature,
            "source": source,
            "sample": {
                "sampleStartTicks": film["sampleStartTicks"],
                "sampleEndTicks": film["sampleEndTicks"],
                "absoluteOffsetTicks": film["sampleStartTicks"],
                "excerptRuntimeTicks": SAMPLE_TICKS,
            },
            "asr_run": str(asr_run),
            "probe_facts": sample.get("probe"),
            "joint_windows": windows,
        }
        dest = dest_root / ("embed-" + film["label"])
        refuse_write_target(dest)
        films.append({
            "label": film["label"], "source": source, "sample": protocol["sample"],
            "asr_run": str(asr_run), "windows": len(windows),
        })
        written.append((dest, protocol))
    dest_root.mkdir(parents=True, mode=0o700)
    root = {
        "version": "issue3-c-sample-v1",
        "fullFilm": False,
        "coverage": "partial",
        "window": window,
        "embedding_base_url": ISOLATED_BASE,
        "max_inflight_embeddings": 1,
        "signature": signature,
        "films": films,
    }
    save_json(dest_root / "protocol.json", root)
    for dest, protocol in written:
        dest.mkdir(parents=True, mode=0o700)
        save_json(dest / "protocol.json", protocol)
    return {
        "destination": str(dest_root),
        "fullFilm": False,
        "films": [{"label": film["label"], "itemId": film["source"]["itemId"],
                   "windows": film["windows"], "directory": "embed-" + film["label"]} for film in films],
        "processingIdBound": processing_id is not None,
    }


def bind_signature(dest_root, processing_id):
    dest_root = Path(dest_root)
    signature = signature_template(processing_id)
    root_path = dest_root / "protocol.json"
    root = json.loads(root_path.read_text(encoding="utf-8"))
    checked_window(root.get("window"))
    require_isolated_base(root.get("embedding_base_url"))
    current = (root.get("signature") or {}).get("processing_id")
    if current not in (None, "") and current != signature["processing_id"]:
        raise ContractError("processing_id is already bound to a different live service")
    for folder in sorted(path for path in dest_root.iterdir() if path.is_dir() and path.name.startswith("embed-")):
        if (folder / "joint").exists() or (folder / "text").exists():
            raise ContractError("refusing to change the signature after embedding started")
    root["signature"] = signature
    save_json(root_path, root)
    bound = []
    for film in root["films"]:
        folder = dest_root / ("embed-" + film["label"])
        protocol_path = folder / "protocol.json"
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        protocol["signature"] = signature
        save_json(protocol_path, protocol)
        bound.append(film["label"])
    return {"processingIdBound": True, "labels": bound}


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_transcription_succeeded(run):
    run = Path(run)
    prepare = json.loads((run / "prepare.json").read_text(encoding="utf-8"))
    documents = json.loads((run / "documents-state.json").read_text(encoding="utf-8"))
    if prepare.get("status") != "succeeded":
        raise ContractError("prepare is " + str(prepare.get("status")) + "; failed or unknown work cannot be offset")
    if documents.get("status") != "succeeded":
        raise ContractError("documents is " + str(documents.get("status")) + "; failed or unknown work cannot be offset")
    chunks = json.loads((run / "chunks.json").read_text(encoding="utf-8"))
    for stage in ("asr", "align"):
        tasks = json.loads((run / f"{stage}-tasks.json").read_text(encoding="utf-8"))
        if len(tasks) != len(chunks):
            raise ContractError(stage + " checkpoint count does not match")
        for task in tasks:
            if task.get("status") != "succeeded":
                raise ContractError(stage + " is " + str(task.get("status")) + "; failed or unknown work cannot be offset")
    candidates = json.loads((run / "candidates.json").read_text(encoding="utf-8"))
    if not isinstance(candidates, list):
        raise ContractError("candidates.json must be a list")
    return candidates


def offset_transcription(source_run, dest_run):
    source_run, dest_run = Path(source_run).resolve(), Path(dest_run).resolve()
    if source_run == dest_run or dest_run.name.startswith("asr-"):
        raise ContractError("offset destination must be the embed directory, not the ASR run")
    refuse_write_target(dest_run)
    protocol = json.loads((dest_run / "protocol.json").read_text(encoding="utf-8"))
    if protocol.get("fullFilm") is not False:
        raise ContractError("embed protocol must stay a partial excerpt")
    provenance = read_asr_provenance(source_run)
    sample = protocol["sample"]
    source = protocol["source"]
    if int(provenance["absoluteOffsetTicks"]) != int(sample["absoluteOffsetTicks"]):
        raise ContractError("ASR offset does not match the embed sample")
    if int(provenance["sampleEndTicks"]) != int(sample["sampleEndTicks"]):
        raise ContractError("ASR sample end does not match the embed sample")
    if str(Path(protocol["asr_run"]).resolve()) != str(source_run):
        raise ContractError("embed protocol points at a different ASR run")
    offset = int(sample["absoluteOffsetTicks"])
    sample_end = int(sample["sampleEndTicks"])
    runtime = int(source["runtimeTicks"])
    watched = ["protocol.json", "prepare.json", "documents-state.json", "chunks.json",
               "candidates.json", "asr-tasks.json", "align-tasks.json"]
    before = {name: _sha(source_run / name) for name in watched}
    candidates = require_transcription_succeeded(source_run)
    shifted = []
    seen = set()
    for entry in candidates:
        local_start, local_end = int(entry["start_ticks"]), int(entry["end_ticks"])
        if not 0 <= local_start < local_end <= SAMPLE_TICKS:
            raise ContractError("excerpt candidate is outside the 120 second wav")
        start, end = local_start + offset, local_end + offset
        if not sample["sampleStartTicks"] <= start < end <= sample_end <= runtime:
            raise ContractError("offset candidate leaves the frozen sample or the original film")
        candidate_id = str(entry["candidate_id"])
        if candidate_id in seen:
            raise ContractError("duplicate transcription candidate")
        seen.add(candidate_id)
        cloned = dict(entry)
        cloned["start_ticks"] = start
        cloned["end_ticks"] = end
        cloned["excerpt_start_ticks"] = local_start
        cloned["excerpt_end_ticks"] = local_end
        cloned["absolute_offset_ticks"] = offset
        cloned["full_film"] = False
        shifted.append(cloned)
    check = {
        "status": "succeeded",
        "fullFilm": False,
        "absoluteOffsetTicks": offset,
        "sampleStartTicks": int(sample["sampleStartTicks"]),
        "sampleEndTicks": sample_end,
        "excerptRuntimeTicks": SAMPLE_TICKS,
        "originalRuntimeTicks": runtime,
        "candidateCount": len(shifted),
        "itemId": canonical_item(source["itemId"]),
        "contentVersion": source["contentVersion"],
        "sourcePath": source["sourcePath"],
        "asrRun": str(source_run),
    }
    candidates_path = dest_run / "candidates.json"
    encoded = json.dumps(shifted, ensure_ascii=False, indent=2) + "\n"
    if candidates_path.exists() and candidates_path.read_text(encoding="utf-8") != encoded:
        raise ContractError("refusing to overwrite offset candidates")
    save_json(candidates_path, shifted)
    save_json(dest_run / "transcription-check.json", check)
    after = {name: _sha(source_run / name) for name in watched}
    if after != before:
        raise ContractError("ASR run changed while offsetting")
    return {"label": protocol["label"], "candidates": len(shifted), "fullFilm": False,
            "absoluteOffsetTicks": offset, "asrUnchanged": True}


def _finite_seconds(raw):
    if isinstance(raw, bool) or raw in (None, "", "N/A"):
        return None
    try:
        value = Decimal(str(raw))
    except Exception:
        return None
    if not value.is_finite():
        return None
    return value


def _stream_endpoint(stream):
    """Container-timeline end: start plus duration. Illegal timestamps stay unknown."""
    if not isinstance(stream, dict):
        return None
    start = _finite_seconds(stream.get("start_time"))
    duration = _finite_seconds(stream.get("duration"))
    if start is None or duration is None or start < 0 or duration < 0:
        return None
    return start + duration


def _frame_duration(rate):
    if not isinstance(rate, str) or "/" not in rate:
        return None
    numerator, denominator = rate.split("/", 1)
    try:
        top, bottom = Decimal(numerator), Decimal(denominator)
    except Exception:
        return None
    if top <= 0 or bottom <= 0:
        return None
    return bottom / top


def _seconds_text(value):
    return format(value, "f")


def classify_video_tail(probe, audio_stream_index, runtime_ticks, frame_rate=None):
    """Classify a frozen source end. This does not open the media."""
    try:
        video, _video_index, _attached = _program_video(probe)
    except ContractError:
        return {"status": "unknown"}
    streams = list((probe or {}).get("streams") or [])
    try:
        audio_index = int(audio_stream_index)
    except (TypeError, ValueError):
        audio_index = None
    audio = None if audio_index is None else next((stream for stream in streams if stream.get("index") == audio_index), None)
    video_end = _stream_endpoint(video)
    audio_end = _stream_endpoint(audio)
    if audio is None or audio.get("codec_type") != "audio" or video_end is None or audio_end is None:
        return {"status": "unknown"}
    runtime = Decimal(int(runtime_ticks)) / Decimal(10_000_000)
    gap = audio_end - video_end
    record = {
        "gapSeconds": _seconds_text(gap),
        "videoEndSeconds": _seconds_text(video_end),
        "audioEndSeconds": _seconds_text(audio_end),
    }
    # The picture already reaches the container. Seek stays on the window, with no hold.
    if video_end >= runtime or (video_end + Decimal("0.001") >= runtime and gap <= 0):
        return {"status": "none", **record}
    if abs(audio_end - runtime) > Decimal("0.001"):
        return {"status": "unknown"}
    if gap <= 0:
        return {"status": "none", **record}
    if gap > VIDEO_TAIL_LIMIT:
        return {"status": "over-bound", **record}
    record["status"] = "within-bound"
    if frame_rate is None:
        return record
    frame = _frame_duration(frame_rate)
    if frame is None or frame <= 0 or frame > VIDEO_TAIL_LIMIT:
        return {"status": "unknown", **record}
    frames = (gap / frame).to_integral_value(rounding=ROUND_CEILING)
    if frames <= 0:
        frames = Decimal(1)
    extension = frames * frame
    quantum = Decimal("0.000001")
    extension = (extension / quantum).to_integral_value(rounding=ROUND_UP) * quantum
    if extension > VIDEO_TAIL_LIMIT:
        return {"status": "over-bound", **record, "extensionSeconds": _seconds_text(extension)}
    record["frameRate"] = frame_rate
    record["extensionSeconds"] = _seconds_text(extension)
    return record


def hold_last_frame(probe, audio_stream_index, runtime_ticks, end_ticks, policy, frame_rate=None):
    """Return the last-window hold, or None when this window must be cut unchanged."""
    if policy in (None, ""):
        return None
    if policy != VIDEO_TAIL_POLICY:
        raise ContractError("unknown video tail policy")
    if int(end_ticks) != int(runtime_ticks):
        return None
    decision = classify_video_tail(probe, audio_stream_index, runtime_ticks, frame_rate=frame_rate)
    status = decision["status"]
    if status == "none":
        return None
    if status == "over-bound":
        raise ContractError("video tail is longer than 200 milliseconds")
    if status != "within-bound":
        raise ContractError("video tail source duration is unknown")
    if frame_rate is None:
        decision["needsFrameRate"] = True
        return decision
    if "extensionSeconds" not in decision:
        raise ContractError("video tail source duration is unknown")
    return decision


def probe_frame_rate(source_path, video_stream_index=0):
    try:
        selector = str(int(video_stream_index))
    except (TypeError, ValueError) as exc:
        raise ContractError("video tail frame rate is unknown") from exc
    if int(selector) < 0:
        raise ContractError("video tail frame rate is unknown")
    completed = subprocess.run(
        ["/usr/local/bin/ffprobe", "-v", "error", "-select_streams", selector,
         "-show_entries", "stream=r_frame_rate", "-of", "json", str(source_path)],
        check=False, capture_output=True, timeout=20,
    )
    if completed.returncode != 0:
        raise ContractError("video tail frame rate is unknown")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ContractError("video tail frame rate is unknown") from exc
    streams = document.get("streams") or []
    rate = streams[0].get("r_frame_rate") if streams else None
    if _frame_duration(rate) is None:
        raise ContractError("video tail frame rate is unknown")
    return rate


def clip_ffmpeg_arguments(source, start_ticks, audio_stream_index, output, probe, video_tail_extension=None):
    facts = check_source_probe(probe, audio_stream_index)
    video_map = facts["video_map"]
    start = ticks_to_seconds(int(start_ticks))
    arguments = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-accurate_seek",
        "-ss", start, "-i", str(source),
    ]
    if video_tail_extension:
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", str(video_tail_extension)):
            raise ContractError("video tail extension is not a duration")
        arguments.extend([
            "-filter_complex",
            "[" + video_map + "]tpad=stop_mode=clone:stop_duration=" + str(video_tail_extension) + "[v]",
            "-map", "[v]",
        ])
    arguments.extend(["-t", "12"])
    if not video_tail_extension:
        arguments.extend(["-map", video_map])
    arguments.extend([
        "-map", facts["audio_map"],
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-threads", "2", "-c:a", "aac", "-ac", "1", "-ar", "16000", "-sn", "-dn", str(output),
    ])
    raw_start = facts["video_start_time_raw"]
    if raw_start and raw_start not in {"0", "0.000000", "0.0"} and raw_start in arguments:
        raise ContractError("video start was added to the clip time")
    for index in facts["attached_picture_stream_indexes"]:
        if f"0:{index}" in arguments or f"0:v:{index}" in arguments:
            raise ContractError("attached picture was mapped")
    return arguments, facts


def entry_full_film(entry):
    """Excerpt rows stay false. A whole-film row must say so itself."""
    return bool(isinstance(entry, dict) and entry.get("full_film") is True)


def decide_video_tail_hold(source, probe, end, policy):
    preliminary = hold_last_frame(
        probe, source["audioStreamIndex"], source["runtimeTicks"], end, policy)
    if not preliminary or not preliminary.get("needsFrameRate"):
        return preliminary
    facts = check_source_probe(probe, source["audioStreamIndex"])
    rate = probe_frame_rate(source["sourcePath"], facts["video_stream_index"])
    return hold_last_frame(
        probe, source["audioStreamIndex"], source["runtimeTicks"], end, policy, frame_rate=rate)


def identity_with_hold(identity, hold):
    if not hold:
        return identity
    extended = dict(identity)
    extended["video_tail_policy"] = VIDEO_TAIL_POLICY
    extended["video_tail_hold"] = "last-frame"
    extended["video_tail_extension_seconds"] = hold["extensionSeconds"]
    return extended


def clip_window(run, entry, source, window, probe, sha, save, read, source_facts, video_tail_policy=None):
    checked_window(window)
    facts = check_source_probe(probe, source["audioStreamIndex"])
    start, end = int(entry["start_ticks"]), int(entry["end_ticks"])
    if end - start != LENGTH_TICKS or end > int(source["runtimeTicks"]):
        raise ContractError("clip is not a full 12-second window inside the original film")
    path = Path(run) / "clips" / (entry["candidate_id"] + ".mp4")
    state_path = path.with_suffix(".clip.json")
    identity = {
        "source": source_facts(source), "start_ticks": start, "end_ticks": end,
        "profile": INPUT_VERSION, "threads": 2, "audio_stream_index": source["audioStreamIndex"],
        "window_version": VERSION, "video_map": facts["video_map"], "audio_map": facts["audio_map"],
        "time_origin": "container", "video_start_added": False,
        "video_start_time_raw": facts["video_start_time_raw"],
        "attached_picture_stream_indexes": facts["attached_picture_stream_indexes"],
        "item_id": canonical_item(source["itemId"]), "content_version": source["contentVersion"],
        "source_path": source["sourcePath"], "full_film": entry_full_film(entry),
    }
    if state_path.exists():
        state = read(state_path)
        stored = state.get("identity")
        digest_ok = sha(path) == state.get("sha256")
        if stored == identity and digest_ok:
            return path, state
        extended = identity_with_hold(identity, decide_video_tail_hold(source, probe, end, video_tail_policy))
        if stored == extended and digest_ok:
            return path, state
        raise ContractError("Cached clip changed")
    hold = decide_video_tail_hold(source, probe, end, video_tail_policy)
    identity = identity_with_hold(identity, hold)
    if path.exists():
        raise ContractError("Unverified clip already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.stem + ".partial.mp4")
    if temporary.exists():
        raise ContractError("Interrupted clip needs inspection")
    extension = None if not hold else hold["extensionSeconds"]
    arguments, _ = clip_ffmpeg_arguments(
        source["sourcePath"], start, source["audioStreamIndex"], temporary, probe, extension)
    arguments[0] = "/usr/local/bin/ffmpeg"
    started = time.monotonic()
    subprocess.run(arguments, check=True, timeout=180)
    probe_out = json.loads(subprocess.check_output([
        "/usr/local/bin/ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time,duration",
        "-of", "json", str(temporary)], timeout=20))
    kinds = {stream["codec_type"] for stream in probe_out["streams"]}
    if not {"video", "audio"} <= kinds or len([stream for stream in probe_out["streams"] if stream["codec_type"] == "video"]) != 1:
        raise ContractError("clip does not contain exactly one video stream and one audio stream")
    for stream in probe_out["streams"]:
        if stream["codec_type"] in {"video", "audio"}:
            if abs(float(stream.get("start_time", "0"))) > 0.15 or not clip_duration_accepted(float(stream["duration"])):
                raise ContractError("clipped container is outside 12 seconds plus or minus 0.15")
    source_facts(source)
    temporary.rename(path)
    state = {"identity": identity, "sha256": sha(path), "probe": probe_out,
             "clip_seconds": time.monotonic() - started, "bytes": path.stat().st_size}
    if hold:
        state["videoTail"] = {
            "policy": VIDEO_TAIL_POLICY,
            "hold": "last-frame",
            "gapSeconds": hold["gapSeconds"],
            "extensionSeconds": hold["extensionSeconds"],
            "frameRate": hold["frameRate"],
            "videoEndSeconds": hold["videoEndSeconds"],
            "audioEndSeconds": hold["audioEndSeconds"],
        }
    save(state_path, state)
    return path, state


def run_embedding(embed, args, run, protocol):
    refuse_write_target(run)
    require_isolated_base(protocol.get("embedding_base_url"))
    require_signature(protocol.get("signature"))
    window = checked_window(protocol.get("window"))
    if protocol.get("fullFilm") is not False or int(protocol.get("max_inflight_embeddings", 0)) != 1:
        raise ContractError("12-second embed stays a single partial excerpt")
    source = protocol["source"]
    require_sample_span(protocol["sample"]["sampleStartTicks"], protocol["sample"]["sampleEndTicks"], source["runtimeTicks"])
    check_source_probe(protocol.get("probe_facts"), source["audioStreamIndex"])
    if args.stage == "joint":
        entries = protocol["joint_windows"]
        planned = plan_sample(protocol["sample"]["sampleStartTicks"], protocol["sample"]["sampleEndTicks"], source["runtimeTicks"])
        if [(int(entry["start_ticks"]), int(entry["end_ticks"])) for entry in entries] != planned:
            raise ContractError("joint windows do not match the frozen excerpt")
    else:
        check_path = Path(run) / "transcription-check.json"
        if not check_path.is_file():
            raise ContractError("text stage requires the offset transcription check")
        check = json.loads(check_path.read_text(encoding="utf-8"))
        if check.get("status") != "succeeded" or check.get("fullFilm") is not False:
            raise ContractError("transcription check did not succeed for this excerpt")
        entries = json.loads((Path(run) / "candidates.json").read_text(encoding="utf-8"))
        if len(entries) != int(check["candidateCount"]):
            raise ContractError("candidate count does not match the transcription check")
        if not entries:
            print(json.dumps({"stage": "text", "candidates": 0, "dispatched": 0, "fullFilm": False}, ensure_ascii=False), flush=True)
            return
    if len({entry["candidate_id"] for entry in entries}) != len(entries):
        raise ContractError("candidate ids are missing or duplicated")
    if args.prefetch_clips and args.stage != "joint":
        raise ContractError("text stage cannot prefetch clips")
    lock = Path(run) / "embedding.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    started = time.monotonic()
    completed = new_jobs = 0
    try:
        with embed.closing(embed.prepared_entries(
                run, entries, source, args.prefetch_clips, window, protocol.get("probe_facts"),
                protocol.get("video_tail_policy"))) as jobs:
            while args.max_new_jobs is None or new_jobs < args.max_new_jobs:
                entry = next(jobs, None)
                if entry is None:
                    break
                fresh, state = embed.dispatch(run, entry, protocol, args.stage)
                completed += 1
                new_jobs += int(fresh)
                if fresh:
                    print(json.dumps({
                        "stage": args.stage, "candidate": entry["candidate_id"],
                        "itemId": canonical_item(source["itemId"]), "completed": completed,
                        "total": len(entries), "new_inputs": new_jobs, "seconds": round(state["seconds"], 3),
                    }, ensure_ascii=False), flush=True)
        print(json.dumps({"stage": args.stage, "itemId": canonical_item(source["itemId"]),
                          "completed": completed, "total": len(entries), "new_inputs": new_jobs,
                          "elapsed_seconds": time.monotonic() - started}, ensure_ascii=False), flush=True)
    finally:
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("interface")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--samples", type=Path, required=True)
    prepare.add_argument("--asr-root", type=Path, required=True)
    prepare.add_argument("--dest-root", type=Path, required=True)
    prepare.add_argument("--processing-id")
    bind = sub.add_parser("bind-signature")
    bind.add_argument("--dest-root", type=Path, required=True)
    bind.add_argument("--processing-id", required=True)
    offset = sub.add_parser("offset-transcription")
    offset.add_argument("--from-run", type=Path, required=True)
    offset.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "interface":
        print(json.dumps(interface_template(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "prepare":
        print(json.dumps(prepare_runs(args.samples, args.asr_root, args.dest_root, args.processing_id),
                         ensure_ascii=False, indent=2))
        return 0
    if args.command == "bind-signature":
        print(json.dumps(bind_signature(args.dest_root, args.processing_id), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(offset_transcription(args.from_run, args.dest), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
