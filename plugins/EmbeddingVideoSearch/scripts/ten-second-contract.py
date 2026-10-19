#!/usr/bin/env python3
"""10-second joint windows for a separate run. The 6-second embed path is unchanged.

Films are bound by itemId, sourcePath and contentVersion. A run label such as
E2 is only a directory name and is not a web episode number.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


VERSION = "10s-8s-5f70-tail-v1"
LENGTH_TICKS = 100_000_000
STEP_TICKS = 80_000_000
SAMPLED_SECONDS = [0, 2, 4, 6, 8]
PROFILE = "jellyfin-10s-5f"
MAX_SOFT_TOKENS = 70
MODEL_ID = "google/embeddinggemma-2"
REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"
INPUT_VERSION = "joint-x264-veryfast-crf28-yuv420p-aac-16000-mono-v1"
AUDIO_VERSION = "source-default-audio-index-v1"
PROTECTED_NAMES = {"jellyfin-full-film-20261016", "full-film-20261016"}
PLACEHOLDERS = {"", "pending", "tbd", "todo", "unknown", "changeme", "processing_id", "none", "null"}


class ContractError(AssertionError):
    pass


def milliseconds(seconds):
    return int(round(float(seconds) * 1000))


def clip_duration_accepted(seconds):
    if seconds != seconds:
        return False
    delta = abs(milliseconds(seconds) - 10_000)
    if delta > 150:
        return False
    return all(milliseconds(seconds) > offset * 1000 for offset in SAMPLED_SECONDS)


def audio_covers_clip(audio_seconds, clip_seconds):
    if audio_seconds != audio_seconds or clip_seconds != clip_seconds:
        return False
    return milliseconds(clip_seconds) - milliseconds(audio_seconds) <= 150


def validate_joint_processing(processing):
    """Accept the jellyfin-10s-5f response. It has no fps field; do not apply the 6-second fps check."""
    modalities = set(processing.get("modalities") or [])
    if modalities != {"video", "audio"}:
        raise ContractError("10s joint response must contain video and audio")
    video = processing.get("video") or {}
    if "fps" in video:
        raise ContractError("10s metadata has no fps field; fps=1 is the 6-second sampler")
    if video.get("audio_included") is not True or video.get("has_audio") is not True:
        raise ContractError("10s joint keeps the audio track")
    if video.get("sampled_seconds") != SAMPLED_SECONDS or video.get("offsets_seconds") != SAMPLED_SECONDS:
        raise ContractError("10s samples must stay exactly [0, 2, 4, 6, 8]")
    if video.get("frame_count") != 5:
        raise ContractError("10s frame_count must be 5")
    if video.get("profile") != PROFILE or video.get("max_soft_tokens") != MAX_SOFT_TOKENS:
        raise ContractError("10s profile or visual budget mismatch")
    if video.get("add_timestamps") is not False or video.get("overflow_strategy") != "reject":
        raise ContractError("10s timestamps stay off and overflow is rejected")
    if video.get("window_seconds") != 10:
        raise ContractError("10s window_seconds must be 10")
    duration = video.get("duration_seconds")
    if not clip_duration_accepted(duration):
        raise ContractError("clip duration must stay within 10 seconds plus or minus 0.15")
    audio = processing.get("audio") or {}
    if audio.get("sample_rate") != 16000 or audio.get("channels") != 1:
        raise ContractError("decoded audio must be 16 kHz mono")
    if audio.get("truncated") is not False or audio.get("coverage") != "full_track":
        raise ContractError("10s audio must keep the full track")
    heard = audio.get("duration_seconds")
    if not audio_covers_clip(heard, duration):
        raise ContractError("decoded audio is shorter than the clip by more than 0.15 seconds")
    return True


def checked_window(window):
    if not isinstance(window, dict) or window.get("version") != VERSION:
        raise ContractError("window version must be the 10-second profile")
    if window.get("length_ticks") != LENGTH_TICKS or window.get("step_ticks") != STEP_TICKS:
        raise ContractError("10-second length or stride does not match the frozen profile")
    if window.get("sampled_seconds") != SAMPLED_SECONDS or window.get("fps") != 0.5:
        raise ContractError("protocol sampling must be five frames at 0.5 fps spacing")
    if window.get("max_soft_tokens") != MAX_SOFT_TOKENS or window.get("add_timestamps") is not False:
        raise ContractError("protocol visual budget or timestamps do not match")
    if window.get("profile") != PROFILE:
        raise ContractError("protocol profile must be jellyfin-10s-5f")
    return window


ISOLATED_BASE = "http://127.0.0.1:18783"
SHARED_BASE = "http://127.0.0.1:18780"


def require_isolated_base(url):
    text = str(url or "").rstrip("/")
    if text == SHARED_BASE or text.endswith(":18780"):
        raise ContractError("10-second index must not use the shared 18780 service")
    if text != ISOLATED_BASE:
        raise ContractError("10-second embedding_base_url must be http://127.0.0.1:18783")
    return text


def signature_tuple(signature):
    require_signature(signature)
    return (signature["id"], signature["revision"], signature["processing_id"],
            int(signature["dimension"]), signature["normalized"] is True)


def require_same_signature(film_signature, root_signature):
    if signature_tuple(film_signature) != signature_tuple(root_signature):
        raise ContractError("film signature does not match the root signature")
    return root_signature


def require_same_window(film_window, root_window):
    keys = ("version", "length_ticks", "step_ticks", "sampled_seconds", "fps",
            "max_soft_tokens", "add_timestamps", "profile")
    left, right = checked_window(film_window), checked_window(root_window)
    if any(left.get(key) != right.get(key) for key in keys):
        raise ContractError("film window does not match the root window")
    return right


def require_signature(signature):
    if not isinstance(signature, dict):
        raise ContractError("signature is missing; copy processing_id from the live service")
    processing = signature.get("processing_id")
    if not isinstance(processing, str) or processing.strip().lower() in PLACEHOLDERS or any(mark in processing for mark in "<> "):
        raise ContractError("processing_id must be copied from the live service and is not invented here")
    if signature.get("id") != MODEL_ID or signature.get("revision") != REVISION:
        raise ContractError("model id or revision does not match the frozen weights")
    if signature.get("dimension") != 768 or signature.get("normalized") is not True:
        raise ContractError("signature must stay 768-dimensional and normalized")
    return signature


def canonical_item(value):
    return str(uuid.UUID(str(value)))


def same_item(left, right):
    return canonical_item(left) == canonical_item(right)


def bind_source(expected, observed):
    """Identity is itemId + sourcePath + contentVersion. runLabel is ignored."""
    for key in ("itemId", "sourcePath", "contentVersion"):
        if key not in expected or key not in observed:
            raise ContractError("source identity requires " + key)
    if not same_item(expected["itemId"], observed["itemId"]):
        raise ContractError("itemId does not match")
    if str(expected["sourcePath"]) != str(observed["sourcePath"]):
        raise ContractError("sourcePath does not match")
    if str(expected["contentVersion"]) != str(observed["contentVersion"]):
        raise ContractError("contentVersion does not match")
    if int(expected["runtimeTicks"]) != int(observed["runtimeTicks"]):
        raise ContractError("runtimeTicks must stay the real source runtime")
    return {
        "itemId": canonical_item(expected["itemId"]),
        "sourcePath": str(expected["sourcePath"]),
        "contentVersion": str(expected["contentVersion"]),
        "runtimeTicks": int(expected["runtimeTicks"]),
        "runLabel": expected.get("runLabel"),
        "runLabelIsEpisodeNumber": False,
    }


def film_record(film):
    source = film["source"]
    record = {
        "runLabel": film.get("runLabel") or film.get("film"),
        "itemId": canonical_item(source["itemId"]),
        "sourcePath": source["sourcePath"],
        "contentVersion": source["contentVersion"],
        "runtimeTicks": int(source["runtimeTicks"]),
        "runLabelIsEpisodeNumber": False,
    }
    display = film.get("display")
    if isinstance(display, dict) and "episodeNumber" in display:
        record["displayEpisodeNumber"] = display["episodeNumber"]
        record["displayName"] = display.get("displayName")
    return record


def resolve_film(films, item_id):
    matches = [film for film in films if same_item(film["source"]["itemId"], item_id)]
    if len(matches) != 1:
        raise ContractError("film identity must match exactly one itemId")
    return matches[0]


def plan_windows(runtime_ticks, length=LENGTH_TICKS, step=STEP_TICKS):
    runtime = int(runtime_ticks)
    if runtime <= 0:
        raise ContractError("runtimeTicks must be positive")
    if runtime <= length:
        return [(0, runtime)]
    windows = []
    start = 0
    while start + length < runtime:
        windows.append((start, start + length))
        start += step
    tail = runtime - length
    if not windows or windows[-1][0] != tail:
        windows.append((tail, runtime))
    if windows[0][0] != 0 or windows[-1][1] != runtime:
        raise ContractError("windows do not cover the source runtime")
    for earlier, later in zip(windows, windows[1:]):
        if not earlier[0] < later[0] <= earlier[1]:
            raise ContractError("windows leave a gap or go backwards")
        if later[1] - later[0] != length or earlier[1] - earlier[0] != length:
            raise ContractError("a 10-second window was shortened")
    return windows


def window_entries(source):
    item = canonical_item(source["itemId"]).replace("-", "")
    entries = []
    for index, (start, end) in enumerate(plan_windows(source["runtimeTicks"])):
        entries.append({
            "candidate_id": f"joint-{item}-{index:04d}",
            "start_ticks": start,
            "end_ticks": end,
            "item_id": canonical_item(source["itemId"]),
            "content_version": source["contentVersion"],
            "source_path": source["sourcePath"],
        })
    return entries


def ticks_to_seconds(ticks):
    seconds, remainder = divmod(int(ticks), 10_000_000)
    if remainder == 0:
        return str(seconds)
    return f"{seconds}.{remainder:07d}"


def check_source_probe(probe, audio_stream_index):
    streams = list((probe or {}).get("streams") or [])
    video = next((stream for stream in streams if stream.get("index") == 0), None)
    if video is None or video.get("codec_type") != "video" or video.get("codec_name") != "hevc":
        raise ContractError("stream 0 must be the HEVC program")
    if int(video.get("width") or 0) != 3840 or int(video.get("height") or 0) != 2160:
        raise ContractError("program frame size must be 3840x2160")
    if (video.get("disposition") or {}).get("attached_pic") in (1, True):
        raise ContractError("stream 0 is an attached picture")
    audio_index = int(audio_stream_index)
    audio = next((stream for stream in streams if stream.get("index") == audio_index), None)
    if audio is None or audio.get("codec_type") != "audio":
        raise ContractError("audio map must be the absolute audio stream")
    attached = []
    for stream in streams:
        if stream.get("codec_type") != "video" or stream.get("index") == 0:
            continue
        is_attached = stream.get("codec_name") == "mjpeg" or (stream.get("disposition") or {}).get("attached_pic") in (1, True)
        if not is_attached:
            raise ContractError("an extra video stream is not an attached picture")
        attached.append(int(stream["index"]))
    return {
        "video_map": "0:v:0",
        "audio_map": f"0:{audio_index}",
        "video_start_time_raw": video.get("start_time"),
        "video_start_added": False,
        "time_origin": "container",
        "attached_picture_stream_indexes": attached,
    }


def clip_ffmpeg_arguments(source, start_ticks, audio_stream_index, output, probe):
    facts = check_source_probe(probe, audio_stream_index)
    start = ticks_to_seconds(int(start_ticks))
    arguments = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-accurate_seek",
        "-ss", start, "-i", str(source), "-t", "10",
        "-map", "0:v:0", "-map", facts["audio_map"],
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
        "-threads", "2", "-c:a", "aac", "-ac", "1", "-ar", "16000", "-sn", "-dn", str(output),
    ]
    if facts["video_start_time_raw"] and facts["video_start_time_raw"] not in {"0", "0.000000", "0.0"}:
        if facts["video_start_time_raw"] in arguments:
            raise ContractError("video start was added to the clip time")
    for index in facts["attached_picture_stream_indexes"]:
        if f"0:{index}" in arguments or f"0:v:{index}" in arguments:
            raise ContractError("attached picture was mapped")
    return arguments, facts


def refuse_write_target(path):
    path = Path(path)
    if path.name.startswith("asr-") or path.name in PROTECTED_NAMES:
        raise ContractError("refusing to write into an ASR run or an accepted 6-second directory: " + path.name)
    for parent in [path, *path.parents]:
        if parent.name in PROTECTED_NAMES:
            raise ContractError("refusing to write inside accepted 6-second artifacts")


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def write_film_runs(root_protocol, dest_root):
    refuse_write_target(dest_root)
    dest_root = Path(dest_root)
    if dest_root.exists() and any(dest_root.iterdir()):
        raise ContractError("embed destination must be a new directory")
    checked_window(root_protocol["window"])
    seen = set()
    written = []
    for film in root_protocol["films"]:
        label = str(film.get("runLabel") or film.get("film") or "")
        if not label or label.startswith("asr-"):
            raise ContractError("embed directory cannot reuse an ASR run label")
        item = canonical_item(film["source"]["itemId"])
        if item in seen:
            raise ContractError("duplicate itemId")
        seen.add(item)
        check_source_probe(film.get("probe_facts"), film["source"]["audioStreamIndex"])
        windows = window_entries(film["source"])
        if film.get("expected_windows") is not None and len(windows) != int(film["expected_windows"]):
            raise ContractError("expected window count does not match this item's runtime")
        dest = dest_root / f"embed-{label}"
        refuse_write_target(dest)
        protocol = {
            "version": "three-film-10s-film-v1",
            "film_identity": film_record(film),
            "window": checked_window(root_protocol["window"]),
            "embedding_base_url": root_protocol.get("embedding_base_url"),
            "max_inflight_embeddings": 1,
            "signature": root_protocol.get("signature"),
            "source": film["source"],
            "probe_facts": film["probe_facts"],
            "joint_windows": windows,
            "transcription_run": film.get("transcription_run"),
            "expected_documents": film.get("expected_documents"),
        }
        dest.mkdir(parents=True, mode=0o700)
        save_json(dest / "protocol.json", protocol)
        written.append({**film_record(film), "windows": len(windows), "directory": dest.name})
    return written


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _place(source, dest):
    source, dest = Path(source), Path(dest)
    if not source.is_file():
        raise ContractError("missing transcription file " + source.name)
    digest = _sha(source)
    if dest.exists():
        if _sha(dest) != digest:
            raise ContractError("refusing to overwrite " + str(dest))
        return
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(source.read_bytes())


def require_closed(status, label):
    if status != "succeeded":
        raise ContractError(f"{label} is {status}; failed or unknown work cannot be imported or reused")


def _succeeded(tasks, expected, label):
    if len(tasks) != int(expected):
        raise ContractError(f"{label} checkpoint count does not match")
    for task in tasks:
        require_closed(task.get("status"), f"{label} {task.get('chunk_id')}")


def adopt_transcription(source_run, dest_run, chunks, documents, identity=None):
    source_run, dest_run = Path(source_run).resolve(), Path(dest_run).resolve()
    if source_run == dest_run:
        raise ContractError("transcription source and destination are the same directory")
    refuse_write_target(dest_run)
    if identity is not None:
        observed = json.loads((source_run / "protocol.json").read_text(encoding="utf-8"))["source"]
        bind_source(identity, observed)
    chunk_rows = json.loads((source_run / "chunks.json").read_text(encoding="utf-8"))
    if len(chunk_rows) != int(chunks):
        raise ContractError("chunk count does not match the reused transcription")
    prepare = json.loads((source_run / "prepare.json").read_text(encoding="utf-8"))
    documents_state = json.loads((source_run / "documents-state.json").read_text(encoding="utf-8"))
    require_closed(prepare.get("status"), "prepare")
    require_closed(documents_state.get("status"), "documents")
    for stage in ("asr", "align"):
        tasks = json.loads((source_run / f"{stage}-tasks.json").read_text(encoding="utf-8"))
        _succeeded(tasks, int(chunks), stage)
    candidates = json.loads((source_run / "candidates.json").read_text(encoding="utf-8"))
    if len(candidates) != int(documents):
        raise ContractError("document count does not match the reused transcription")
    names = ["chunks.json", "prepare.json", "candidates.json", "non-substantive.json",
             "documents-state.json", "asr-tasks.json", "align-tasks.json", "asr-metrics.json", "align-metrics.json"]
    for name in names:
        path = source_run / name
        if path.is_file():
            _place(path, dest_run / name)
    for folder in ("asr", "align"):
        for path in sorted((source_run / folder).glob("*.json")):
            _place(path, dest_run / folder / path.name)
    for banned in ("joint", "text", "clips", "audio"):
        if (dest_run / banned).exists():
            raise ContractError("destination already contains embedding artifacts")
    return {"chunks": len(chunk_rows), "documents": len(candidates), "copiedEmbeddings": False,
            "runLabelIsEpisodeNumber": False}


def interface_template():
    return {
        "version": "three-film-10s-v1",
        "window": {
            "version": VERSION, "length_ticks": LENGTH_TICKS, "step_ticks": STEP_TICKS,
            "sampled_seconds": SAMPLED_SECONDS, "fps": 0.5, "max_soft_tokens": MAX_SOFT_TOKENS,
            "add_timestamps": False, "profile": PROFILE,
        },
        "embedding_base_url": "http://127.0.0.1:18783",
        "max_inflight_embeddings": 1,
        "signature": {
            "id": MODEL_ID, "revision": REVISION, "processing_id": None,
            "dimension": 768, "normalized": True,
            "note": "Model agent reported eg2-0b01d27c8c20b7d920e5. Confirm it from the live /v1/models info before writing it. This script does not fill it in.",
        },
        "identity": "Bind every film by itemId, sourcePath and contentVersion. runLabel is not a web episode number.",
        "window_counts_by_runtime_ticks": {"12088100000": 151, "11567786670": 145, "17101752380": 214},
    }


def run_embedding(embed, args, run, protocol):
    refuse_write_target(run)
    require_isolated_base(protocol.get("embedding_base_url"))
    require_signature(protocol.get("signature"))
    window = checked_window(protocol.get("window"))
    probe = protocol.get("probe_facts")
    if int(protocol.get("max_inflight_embeddings", 0)) != 1:
        raise ContractError("max_inflight_embeddings must be 1")
    source = protocol["source"]
    bind_source(source, source)
    check_source_probe(protocol.get("probe_facts"), source["audioStreamIndex"])
    if args.stage == "joint":
        entries = protocol["joint_windows"]
        planned = plan_windows(source["runtimeTicks"])
        if [(int(entry["start_ticks"]), int(entry["end_ticks"])) for entry in entries] != planned:
            raise ContractError("joint windows do not match this item's runtime")
    else:
        entries = embed.read(Path(run) / "candidates.json")
        if protocol.get("expected_documents") is not None and len(entries) != int(protocol["expected_documents"]):
            raise ContractError("document count does not match the frozen transcription")
    if not entries or len({entry["candidate_id"] for entry in entries}) != len(entries):
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
                run, entries, source, args.prefetch_clips, window, probe)) as jobs:
            while args.max_new_jobs is None or new_jobs < args.max_new_jobs:
                entry = next(jobs, None)
                if entry is None:
                    break
                fresh, state = embed.dispatch(run, entry, protocol, args.stage)
                completed += 1
                new_jobs += int(fresh)
                if fresh:
                    print(json.dumps({
                        "stage": args.stage, "candidate": entry["candidate_id"], "itemId": canonical_item(source["itemId"]),
                        "contentVersion": source["contentVersion"], "completed": completed, "total": len(entries),
                        "new_inputs": new_jobs, "seconds": round(state["seconds"], 3),
                    }, ensure_ascii=False), flush=True)
        print(json.dumps({"stage": args.stage, "itemId": canonical_item(source["itemId"]),
                          "completed": completed, "total": len(entries), "new_inputs": new_jobs,
                          "elapsed_seconds": time.monotonic() - started}, ensure_ascii=False), flush=True)
    finally:
        lock.unlink()


def clip_window(run, entry, source, window, probe, sha, save, read, source_facts):
    checked_window(window)
    facts = check_source_probe(probe, source["audioStreamIndex"])
    start, end = int(entry["start_ticks"]), int(entry["end_ticks"])
    if end - start != LENGTH_TICKS or end > int(source["runtimeTicks"]):
        raise ContractError("clip is not a full 10-second window inside the source")
    path = Path(run) / "clips" / (entry["candidate_id"] + ".mp4")
    state_path = path.with_suffix(".clip.json")
    identity = {
        "source": source_facts(source), "start_ticks": start, "end_ticks": end,
        "profile": INPUT_VERSION, "threads": 2, "audio_stream_index": source["audioStreamIndex"],
        "window_version": VERSION, "video_map": "0:v:0", "audio_map": facts["audio_map"],
        "time_origin": "container", "video_start_added": False,
        "video_start_time_raw": facts["video_start_time_raw"],
        "attached_picture_stream_indexes": facts["attached_picture_stream_indexes"],
        "item_id": canonical_item(source["itemId"]), "content_version": source["contentVersion"],
        "source_path": source["sourcePath"],
    }
    if state_path.exists():
        state = read(state_path)
        if state.get("identity") != identity or sha(path) != state.get("sha256"):
            raise ContractError("Cached clip changed")
        return path, state
    if path.exists():
        raise ContractError("Unverified clip already exists")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.stem + ".partial.mp4")
    if temporary.exists():
        raise ContractError("Interrupted clip needs inspection")
    arguments, _ = clip_ffmpeg_arguments(source["sourcePath"], start, source["audioStreamIndex"], temporary, probe)
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
                raise ContractError("clipped container is outside 10 seconds plus or minus 0.15")
    source_facts(source)
    temporary.rename(path)
    state = {"identity": identity, "sha256": sha(path), "probe": probe_out,
             "clip_seconds": time.monotonic() - started, "bytes": path.stat().st_size}
    save(state_path, state)
    return path, state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("interface")
    plan = sub.add_parser("plan")
    plan.add_argument("--protocol", type=Path, required=True)
    plan.add_argument("--dest-root", type=Path, required=True)
    adopt = sub.add_parser("adopt-transcription")
    adopt.add_argument("--from-run", type=Path, required=True)
    adopt.add_argument("--dest", type=Path, required=True)
    adopt.add_argument("--chunks", type=int, required=True)
    adopt.add_argument("--documents", type=int, required=True)
    args = parser.parse_args(argv)
    if args.command == "interface":
        print(json.dumps(interface_template(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "plan":
        protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
        print(json.dumps(write_film_runs(protocol, args.dest_root), ensure_ascii=False, indent=2))
        return 0
    result = adopt_transcription(args.from_run, args.dest, args.chunks, args.documents)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
