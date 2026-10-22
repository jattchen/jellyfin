#!/usr/bin/env python3
"""Single-film ASR, alignment and punctuation segmentation.

Stages are separate and serial. Model work runs one chunk per subprocess.
Successful checkpoints are reused. Failed, unknown and truncated chunks stop
the stage and are not retried. This script does not embed, import, download,
or start a service.

Omit transcription_language_policy to keep forced Chinese. Set it to auto-v1
to detect one language per audio chunk. Empty successful text skips alignment.
An unsupported aligner language still stops unless the run explicitly sets
unsupported_align_language_policy to unsupported-align-language-skip-v1.
That skip keeps the original text as a gap and is not an alignment.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
import wave
from pathlib import Path


SCRIPT = Path(__file__).resolve()
SAMPLE_RATE = 16000
TICKS_PER_SECOND = 10_000_000
TICKS_PER_SAMPLE = TICKS_PER_SECOND // SAMPLE_RATE
MAX_CHUNK_SECONDS = 30.0
ASR_MAX_NEW_TOKENS = 1024
CHUNK_TIMEOUT_SECONDS = 600
PRIMARY_DELIMITERS = "。！？!?；;"
OVERLONG_DELIMITERS = "，,、：:"
CONTENT_LIMIT_SECONDS = 8.0
CONTEXT_SECONDS = 1.0
MINIMUM_PLAYBACK_SECONDS = 5.0
MAXIMUM_LOCALIZATION_SECONDS = 10.0
SEGMENTATION_VERSION = "qwen-punctuation-aligned-greedy8-context1-ticks-v2"
CHUNK_FIELDS = (
    "chunk_id",
    "wav_path",
    "start_sample",
    "end_sample",
    "start_ticks",
    "end_ticks",
    "offset_seconds",
    "duration_seconds",
    "wav_sha256",
)
TIMESTAMP_TOLERANCE_SECONDS = 0.001
TAIL_TIME_POLICY = "last-unit-tail-80ms-v1"
ALIGNER_REVISION = "53c8c0e46733eec430e4b53dd6471d0e5dee45f8"
AUTO_LANGUAGE_POLICY = "auto-v1"
UNSUPPORTED_ALIGN_SKIP_POLICY = "unsupported-align-language-skip-v1"
# Allowlists copied from the installed model config support_languages.
# ASR 30: qwen3-asr-1.7b e1f6c266914abc5a46e8756e02580f834a6cf8a7
# Aligner 11: qwen3-forced-aligner-0.6b 53c8c0e46733eec430e4b53dd6471d0e5dee45f8
# Codes follow that aligner file plus mlx_qwen3_asr forced_aligner.tokenize_text.
ASR_LANGUAGE_NAMES = (
    "Chinese", "English", "Cantonese", "Arabic", "German", "French", "Spanish",
    "Portuguese", "Indonesian", "Italian", "Korean", "Russian", "Thai", "Vietnamese",
    "Japanese", "Turkish", "Hindi", "Malay", "Dutch", "Swedish", "Danish", "Finnish",
    "Polish", "Czech", "Filipino", "Persian", "Greek", "Romanian", "Hungarian", "Macedonian",
)
ALIGNER_LANGUAGE_CODES = {
    "Chinese": "zh",
    "English": "en",
    "Cantonese": "yue",
    "French": "fr",
    "German": "de",
    "Italian": "it",
    "Japanese": "ja",
    "Korean": "ko",
    "Portuguese": "pt",
    "Russian": "ru",
    "Spanish": "es",
}
MISSING_DETECTED_LANGUAGE = frozenset({"", "unknown", "none"})
# Space-delimited aligner languages. Chinese and Cantonese stay character-shaped,
# and Japanese/Korean use their own tokenizers. English and Russian also gain '.'.
SPACE_WORD_LANGUAGES = frozenset({
    "English", "Russian", "French", "German", "Spanish", "Italian", "Portuguese",
})
SENTENCE_PERIOD_LANGUAGES = frozenset({"English", "Russian"})


class StageBlocked(RuntimeError):
    """A stage cannot continue. The caller must not retry automatically."""


class UnitBoundsRejected(StageBlocked):
    """An alignment unit is outside its audio. The original units stay intact."""

    def __init__(self, message, retained):
        super().__init__(message)
        self.retained = retained


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def text_sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical(text):
    return "".join(character for character in text if character.isalnum())


def align_clean(text):
    return "".join(
        character
        for character in text
        if character == "'" or unicodedata.category(character).startswith(("L", "N"))
    )


def substantive(text):
    return any(character.isalnum() for character in text)


def _alias_map(names):
    return {name.casefold(): name for name in names}


ASR_LANGUAGE_ALIASES = _alias_map(ASR_LANGUAGE_NAMES)
ALIGNER_LANGUAGE_ALIASES = _alias_map(ALIGNER_LANGUAGE_CODES)
for _name, _code in ALIGNER_LANGUAGE_CODES.items():
    ALIGNER_LANGUAGE_ALIASES[_code] = _name
ALIGNER_LANGUAGE_ALIASES["jp"] = "Japanese"
ALIGNER_LANGUAGE_ALIASES["kr"] = "Korean"


def language_policy(protocol):
    value = protocol.get("transcription_language_policy", None)
    if value is None:
        return None
    if value == AUTO_LANGUAGE_POLICY:
        return value
    raise StageBlocked(f"未知转写语言策略：{value}")


def asr_language_argument(policy):
    """Language passed to Qwen transcribe. None asks the model to detect."""
    if policy is None:
        return "Chinese"
    if policy == AUTO_LANGUAGE_POLICY:
        return None
    raise StageBlocked(f"未知转写语言策略：{policy}")


def aligner_language_name(detected):
    if not isinstance(detected, str):
        return None
    key = detected.strip().casefold().replace("_", "-")
    if key in MISSING_DETECTED_LANGUAGE:
        return None
    return ALIGNER_LANGUAGE_ALIASES.get(key)


def is_cjk_char(character):
    code = ord(character)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x20000 <= code <= 0x2A6DF
        or 0x2A700 <= code <= 0x2B73F
        or 0x2B740 <= code <= 0x2B81F
        or 0x2B820 <= code <= 0x2CEAF
        or 0xF900 <= code <= 0xFAFF
    )


def split_segment_with_cjk(segment):
    tokens = []
    buf = []

    def flush():
        if buf:
            tokens.append("".join(buf))
            buf.clear()

    for character in segment:
        if is_cjk_char(character):
            flush()
            tokens.append(character)
        else:
            buf.append(character)
    flush()
    return tokens


def space_word_tokens(text):
    """Word tokens the aligner keeps for space-delimited text, spaces excluded."""
    tokens = []
    for segment in str(text).split():
        cleaned = align_clean(segment)
        if cleaned:
            tokens.extend(split_segment_with_cjk(cleaned))
    return tokens


def units_match_words(units, text, language):
    if language not in SPACE_WORD_LANGUAGES:
        return True
    actual = [unit.get("text", "") for unit in units]
    return actual == space_word_tokens(text)


def primary_delimiters_for(language):
    if language in SENTENCE_PERIOD_LANGUAGES:
        return PRIMARY_DELIMITERS + "."
    return PRIMARY_DELIMITERS


def detected_language_conflict(record):
    """True when one audio chunk's own ASR pieces name more than one language."""
    raw = record.get("raw_result") or {}
    found = []
    for item in raw.get("chunks") or []:
        if not isinstance(item, dict) or not substantive(item.get("text") or ""):
            continue
        label = str(item.get("language") or "").strip()
        if label.casefold() not in MISSING_DETECTED_LANGUAGE:
            found.append(label.casefold())
    return len(set(found)) > 1


def align_route(record, policy):
    """Return (aligner language or skip label, block reason)."""
    text = ((record or {}).get("raw_result") or {}).get("text")
    if policy != AUTO_LANGUAGE_POLICY:
        return "Chinese", None
    detected = record.get("language_detected") if isinstance(record, dict) else None
    if not isinstance(detected, str):
        if substantive(text or ""):
            return None, "language-missing"
        return "", None
    if substantive(text or "") and detected_language_conflict(record):
        return None, "language-mixed-chunk"
    if not substantive(text or ""):
        return detected, None
    folded = detected.strip().casefold().replace("_", "-")
    if folded in MISSING_DETECTED_LANGUAGE:
        return None, "language-missing"
    name = aligner_language_name(detected)
    if name is None:
        if folded in ASR_LANGUAGE_ALIASES:
            return None, "language-unsupported"
        return None, "language-unknown"
    return name, None


def unsupported_align_policy(protocol):
    value = (protocol or {}).get("unsupported_align_language_policy", None)
    if value in (None, ""):
        return None
    if value == UNSUPPORTED_ALIGN_SKIP_POLICY:
        return value
    raise StageBlocked(f"未知对齐语言跳过策略：{value}")


def unsupported_align_skip_applies(record, protocol):
    """Substantive text the ASR accepts and this aligner does not."""
    if unsupported_align_policy(protocol) != UNSUPPORTED_ALIGN_SKIP_POLICY:
        return False
    if language_policy(protocol) != AUTO_LANGUAGE_POLICY:
        return False
    _routed, error = align_route(record, AUTO_LANGUAGE_POLICY)
    return error == "language-unsupported"


def is_unsupported_skip_task(task, protocol):
    if unsupported_align_policy(protocol) != UNSUPPORTED_ALIGN_SKIP_POLICY:
        return False
    return (
        isinstance(task, dict)
        and task.get("status") == "skipped"
        and task.get("reason") == "language-unsupported"
        and task.get("skip_policy") == UNSUPPORTED_ALIGN_SKIP_POLICY
        and task.get("alignment_executed") is False
    )


def parse_peak_rss(stderr):
    match = re.search(r"^\s*(\d+)\s+maximum resident set size\s*$", stderr, re.MULTILINE)
    return int(match.group(1)) if match else None


def absolute_ticks(start_sample, local_seconds):
    return int(start_sample) * TICKS_PER_SAMPLE + int(round(float(local_seconds) * TICKS_PER_SECOND))


def pid_alive(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(run):
    path = Path(run) / ".stage.lock"
    if path.exists():
        try:
            holder = int(path.read_text(encoding="utf-8") or "0")
        except ValueError:
            holder = 0
        if pid_alive(holder):
            raise StageBlocked(f"已有阶段进程 {holder} 持有锁，未并行启动")
        path.unlink()
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, str(os.getpid()).encode("ascii"))
    os.close(descriptor)
    return path


def release_lock(path):
    if path is not None and Path(path).exists():
        Path(path).unlink()


def load_protocol(run):
    path = Path(run) / "protocol.json"
    if not path.is_file():
        raise StageBlocked(f"缺少协议文件 {path}")
    protocol = load_json(path)
    source = protocol.get("source") or {}
    for key in ("sourcePath", "audioStreamIndex", "runtimeTicks", "sizeBytes", "sourceItemId", "contentVersion"):
        if key not in source:
            raise StageBlocked(f"协议 source.{key} 缺失")
    if int(source["audioStreamIndex"]) < 0:
        raise StageBlocked("audioStreamIndex 不能为负")
    if int(source["runtimeTicks"]) <= 0:
        raise StageBlocked("runtimeTicks 必须为正")
    return protocol


def require_chunk_policy(protocol):
    if float(protocol.get("asr_max_chunk_seconds", MAX_CHUNK_SECONDS)) != MAX_CHUNK_SECONDS:
        raise StageBlocked("asr_max_chunk_seconds 必须为 30")


def require_asr_budget(protocol):
    if int(protocol.get("asr_max_new_tokens", -1)) != ASR_MAX_NEW_TOKENS:
        raise StageBlocked("asr_max_new_tokens 必须为 1024")


def format_runtime_seconds(runtime_ticks):
    ticks = int(runtime_ticks)
    seconds, remainder = divmod(ticks, TICKS_PER_SECOND)
    return f"{seconds}.{remainder:07d}"


def expected_pcm_samples(runtime_ticks):
    ticks = int(runtime_ticks)
    if ticks <= 0:
        raise StageBlocked("runtimeTicks 必须为正")
    # A source can end between samples. Retain its true runtime, but never
    # invent a final sample extending beyond that source boundary.
    return ticks // TICKS_PER_SAMPLE


def verify_pcm_length(sample_count, runtime_ticks):
    expected = expected_pcm_samples(runtime_ticks)
    if int(sample_count) > expected:
        raise StageBlocked(
            f"PCM 有 {sample_count} 个样本，原片时长只允许 {expected}，多出的尾部不能映射出片外"
        )
    return {
        "expected_pcm_samples": expected,
        "pcm_sample_count": int(sample_count),
        "pcm_minus_expected_samples": int(sample_count) - expected,
    }


def ffmpeg_arguments(source, audio_stream_index, output, runtime_ticks):
    index = int(audio_stream_index)
    if index < 0:
        raise StageBlocked("audioStreamIndex 不能为负")
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        f"0:{index}",
        "-t",
        format_runtime_seconds(expected_pcm_samples(runtime_ticks) * TICKS_PER_SAMPLE),
        "-af",
        f"aresample={SAMPLE_RATE},atrim=end_sample={expected_pcm_samples(runtime_ticks)}",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(output),
    ]


def ffprobe_arguments(source):
    return [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,start_time,duration",
        "-of",
        "json",
        str(source),
    ]


def parse_ffprobe_document(document, audio_index):
    streams = document.get("streams") or []
    audio = next((stream for stream in streams if stream.get("index") == int(audio_index)), None)
    if audio is None:
        raise StageBlocked(f"ffprobe 没有 index={audio_index} 的音轨")
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)

    def number(value):
        if value is None or value == "" or value == "N/A":
            return None
        return float(value)

    audio_duration = number(audio.get("duration"))
    video_duration = number(video.get("duration")) if video else None
    delta = None
    if audio.get("duration") not in (None, "", "N/A") and video and video.get("duration") not in (None, "", "N/A"):
        from decimal import Decimal

        delta = str(Decimal(str(audio["duration"])) - Decimal(str(video["duration"])))
    return {
        "audio_stream_index": int(audio_index),
        "audio_codec_type": audio.get("codec_type"),
        "audio_start_time": number(audio.get("start_time")),
        "audio_start_time_raw": audio.get("start_time"),
        "audio_duration": audio_duration,
        "audio_duration_raw": audio.get("duration"),
        "video_start_time": number(video.get("start_time")) if video else None,
        "video_start_time_raw": video.get("start_time") if video else None,
        "video_duration": video_duration,
        "video_duration_raw": video.get("duration") if video else None,
        "video_audio_duration_delta_seconds": delta,
    }


def probe_with_ffprobe(source, audio_index):
    exe = shutil.which("ffprobe")
    if not exe:
        raise StageBlocked("找不到 ffprobe")
    command = ffprobe_arguments(source)
    command[0] = exe
    completed = subprocess.run(command, check=False, capture_output=True)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace")[-500:]
        raise StageBlocked(f"ffprobe 失败，退出码 {completed.returncode}：{detail}")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise StageBlocked("ffprobe 没有返回可读取的流信息") from exc
    return parse_ffprobe_document(document, audio_index)


def read_pcm16_wav(path):
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        count = handle.getnframes()
        payload = handle.readframes(count)
    if channels != 1 or width != 2 or rate != SAMPLE_RATE:
        raise StageBlocked(f"WAV 必须是 16 kHz 单声道 PCM16：{path}")
    if len(payload) != count * 2:
        raise StageBlocked(f"WAV 样本长度不完整：{path}")
    import numpy as np

    return np.frombuffer(payload, dtype="<i2").copy()


def write_pcm16_wav(path, samples):
    import numpy as np

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    payload = np.asarray(samples, dtype="<i2").tobytes()
    with wave.open(str(temporary), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(payload)
    os.replace(temporary, path)


def load_splitter(worker_python):
    # The venv interpreter is a symlink into the standalone CPython. Keep the
    # path's own parents so site-packages stays next to that symlink.
    venv = Path(worker_python).absolute().parent.parent
    matches = sorted(venv.glob("lib/python*/site-packages/mlx_qwen3_asr/chunking.py"))
    if len(matches) != 1:
        raise StageBlocked(f"找不到唯一的 chunking.py：{venv}")
    spec = importlib.util.spec_from_file_location("full_film_qwen_chunking", matches[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.split_audio_into_chunks


def build_chunk_records(samples, splitter, destination, max_seconds=MAX_CHUNK_SECONDS):
    import numpy as np

    if len(samples) <= 0:
        raise StageBlocked("全片 WAV 没有样本")
    audio = np.asarray(samples, dtype=np.float32) / np.float32(32768.0)
    parts = splitter(audio, sr=SAMPLE_RATE, max_chunk_sec=float(max_seconds))
    records = []
    cursor = 0
    destination = Path(destination)
    for index, (chunk, offset) in enumerate(parts):
        length = int(len(chunk))
        if length <= 0:
            raise StageBlocked("切块结果含空块")
        start = cursor
        end = cursor + length
        duration = length / SAMPLE_RATE
        if duration > float(max_seconds):
            raise StageBlocked(f"切块超过 {max_seconds} 秒：{duration}")
        if abs(float(offset) * SAMPLE_RATE - start) > 1:
            raise StageBlocked(f"切块偏移与样本位置不一致：offset={offset} start={start}")
        chunk_id = f"c{index:04d}"
        wav_path = destination / f"{chunk_id}.wav"
        write_pcm16_wav(wav_path, np.asarray(samples[start:end]))
        records.append(
            {
                "chunk_id": chunk_id,
                "wav_path": str(wav_path),
                "start_sample": start,
                "end_sample": end,
                "start_ticks": start * TICKS_PER_SAMPLE,
                "end_ticks": end * TICKS_PER_SAMPLE,
                "offset_seconds": float(offset),
                "duration_seconds": duration,
                "wav_sha256": file_sha256(wav_path),
            }
        )
        cursor = end
    if cursor != len(samples):
        raise StageBlocked(f"切块样本覆盖 {cursor}，全片为 {len(samples)}，有遗漏或重叠")
    _assert_partition(records, len(samples))
    return records


def _assert_partition(records, sample_count):
    if not records:
        raise StageBlocked("切块列表为空")
    if int(records[0]["start_sample"]) != 0:
        raise StageBlocked("第一块不是从样本 0 开始")
    previous = 0
    for record in records:
        for field in CHUNK_FIELDS:
            if field not in record:
                raise StageBlocked(f"切块缺少 {field}")
        start = int(record["start_sample"])
        end = int(record["end_sample"])
        if start != previous or end <= start:
            raise StageBlocked(f"{record['chunk_id']} 样本范围不连续或重叠")
        duration = (end - start) / SAMPLE_RATE
        if duration > MAX_CHUNK_SECONDS:
            raise StageBlocked(f"{record['chunk_id']} 超过 30 秒")
        if abs(float(record["duration_seconds"]) - duration) > 1e-9:
            raise StageBlocked(f"{record['chunk_id']} 时长与样本数不一致")
        if abs(float(record["offset_seconds"]) * SAMPLE_RATE - start) > 1:
            raise StageBlocked(f"{record['chunk_id']} 偏移与样本位置不一致")
        if type(record["start_ticks"]) is not int or type(record["end_ticks"]) is not int:
            raise StageBlocked(f"{record['chunk_id']} 的起止 tick 必须是整数")
        if record["start_ticks"] != start * TICKS_PER_SAMPLE or record["end_ticks"] != end * TICKS_PER_SAMPLE:
            raise StageBlocked(f"{record['chunk_id']} 的 tick 不是全片 PCM 的绝对位置")
        previous = end
    if previous != int(sample_count):
        raise StageBlocked("切块没有覆盖全部样本")


def validate_chunk_files(run, records, sample_count):
    _assert_partition(records, sample_count)
    for record in records:
        path = Path(record["wav_path"])
        if not path.is_file():
            raise StageBlocked(f"切块 WAV 不存在：{record['chunk_id']}")
        if file_sha256(path) != record["wav_sha256"]:
            raise StageBlocked(f"{record['chunk_id']} 的 WAV 哈希与检查点不一致，未重新转写")
        audio = read_pcm16_wav(path)
        if len(audio) != int(record["end_sample"]) - int(record["start_sample"]):
            raise StageBlocked(f"{record['chunk_id']} 的 WAV 样本数与清单不一致")


def source_identity(protocol, source_path):
    return {
        "source_sha256": file_sha256(source_path),
        "source_size": int(protocol["source"]["sizeBytes"]),
        "audio_stream_index": int(protocol["source"]["audioStreamIndex"]),
        "sample_rate": SAMPLE_RATE,
        "channels": 1,
        "sample_format": "pcm_s16le",
        "max_chunk_seconds": MAX_CHUNK_SECONDS,
        "segmentation_input": "native-low-energy-chunking",
        "runtime_ticks": int(protocol["source"]["runtimeTicks"]),
        "output_limit_seconds": format_runtime_seconds(protocol["source"]["runtimeTicks"]),
    }


def extract_with_ffmpeg(source, audio_stream_index, dest, runtime_ticks):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise StageBlocked("找不到 ffmpeg")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(".wav.partial")
    command = ffmpeg_arguments(source, audio_stream_index, temporary, runtime_ticks)
    command[0] = exe
    completed = subprocess.run(command, check=False, capture_output=True)
    if completed.returncode != 0 or not temporary.is_file():
        if temporary.exists():
            temporary.unlink()
        detail = completed.stderr.decode("utf-8", errors="replace")[-500:]
        raise StageBlocked(f"ffmpeg 提取音频失败，退出码 {completed.returncode}：{detail}")
    os.replace(temporary, dest)


def stage_prepare(run, extract=None, probe=None):
    if extract is None:
        extract = extract_with_ffmpeg
    if probe is None:
        probe = probe_with_ffprobe
    run = Path(run)
    lock = acquire_lock(run)
    try:
        protocol = load_protocol(run)
        require_chunk_policy(protocol)
        worker = protocol.get("worker_python")
        if not worker or not Path(worker).is_file():
            raise StageBlocked("worker_python 不存在，无法加载切块实现")
        source = Path(protocol["source"]["sourcePath"])
        if not source.is_file():
            raise StageBlocked(f"原片不存在：{source}")
        if source.stat().st_size != int(protocol["source"]["sizeBytes"]):
            raise StageBlocked("原片字节数与协议不一致，未提取")
        identity = source_identity(protocol, source)
        state_path = run / "prepare.json"
        state = load_json(state_path) if state_path.exists() else None
        if state is not None:
            status = state.get("status")
            if status == "succeeded":
                if state.get("identity") != identity:
                    raise StageBlocked("准备阶段输入哈希与检查点不同，没有重新提取")
                records = load_json(run / "chunks.json")
                validate_chunk_files(run, records, state["sample_count"])
                if file_sha256(run / state["full_wav"]) != state["full_wav_sha256"]:
                    raise StageBlocked("全片 WAV 哈希与检查点不一致，没有重新提取")
                if "ffprobe" not in state:
                    raise StageBlocked("准备结果缺少音轨探测记录，没有重新提取")
                verify_pcm_length(state["sample_count"], protocol["source"]["runtimeTicks"])
                print(f"prepare reuse chunks={len(records)}", flush=True)
                return records
            raise StageBlocked(f"准备阶段检查点为 {status}，未自动重试")
        save_json(state_path, {"status": "started", "identity": identity})
        try:
            runtime_ticks = int(protocol["source"]["runtimeTicks"])
            audio_index = int(protocol["source"]["audioStreamIndex"])
            probed = probe(source, audio_index)
            wav_path = run / "audio" / "full.wav"
            extract(source, audio_index, wav_path, runtime_ticks)
            samples = read_pcm16_wav(wav_path)
            pcm = verify_pcm_length(len(samples), runtime_ticks)
            records = build_chunk_records(samples, load_splitter(worker), run / "audio" / "chunks")
            if records[-1]["end_ticks"] > runtime_ticks:
                raise StageBlocked("切块结束时间超出原片时长")
            save_json(run / "chunks.json", records)
            save_json(
                state_path,
                {
                    "status": "succeeded",
                    "identity": identity,
                    "sample_rate": SAMPLE_RATE,
                    "sample_count": int(len(samples)),
                    "full_wav": "audio/full.wav",
                    "full_wav_sha256": file_sha256(wav_path),
                    "audio_ticks": int(len(samples)) * TICKS_PER_SAMPLE,
                    "runtime_ticks": runtime_ticks,
                    "runtime_seconds": format_runtime_seconds(runtime_ticks),
                    "ffprobe": probed,
                    **pcm,
                    "chunk_count": len(records),
                },
            )
        except Exception:
            current = load_json(state_path) if state_path.exists() else {}
            if current.get("status") != "succeeded":
                save_json(state_path, {"status": "failed", "identity": identity})
            raise
        print(f"prepare chunks={len(records)} samples={len(samples)}", flush=True)
        return records
    finally:
        release_lock(lock)


def load_chunks(run):
    path = Path(run) / "chunks.json"
    if not path.is_file():
        raise StageBlocked("缺少 chunks.json，先完成 prepare")
    records = load_json(path)
    prepare = load_json(Path(run) / "prepare.json")
    if prepare.get("status") != "succeeded":
        raise StageBlocked("prepare 未成功，不能进入后续阶段")
    validate_chunk_files(run, records, prepare["sample_count"])
    return records


def task_path(run, stage):
    return Path(run) / f"{stage}-tasks.json"


def metric_path(run, stage):
    return Path(run) / f"{stage}-metrics.json"


def load_tasks(run, stage):
    path = task_path(run, stage)
    return load_json(path) if path.exists() else []


def find_task(tasks, chunk_id):
    matches = [task for task in tasks if task.get("chunk_id") == chunk_id]
    if len(matches) > 1:
        raise StageBlocked(f"{chunk_id} 有重复检查点，未自动选择")
    return matches[0] if matches else None


def blocking_task(tasks):
    for task in tasks:
        if task.get("status") != "succeeded":
            return task
    return None


def record_task(run, stage, tasks):
    save_json(task_path(run, stage), tasks)


def record_metric(run, stage, item):
    path = metric_path(run, stage)
    rows = load_json(path) if path.exists() else []
    rows = [row for row in rows if row.get("chunk_id") != item["chunk_id"]]
    rows.append(item)
    save_json(path, rows)


def output_path(run, stage, chunk_id):
    return Path(run) / stage / f"{chunk_id}.json"


def worker_command(protocol, kind, chunk, output, asr_json=None):
    policy = language_policy(protocol)
    if kind == "asr":
        model = protocol["asr_model_path"]
        extra = ["--max-new-tokens", str(ASR_MAX_NEW_TOKENS)]
    else:
        model = protocol["aligner_model_path"]
        extra = ["--asr-json", str(asr_json)]
    if policy == AUTO_LANGUAGE_POLICY:
        extra.extend(["--language-policy", AUTO_LANGUAGE_POLICY])
    return [
        "/usr/bin/time",
        "-l",
        str(protocol["worker_python"]),
        "-B",
        str(SCRIPT),
        "--worker",
        kind,
        "--model",
        str(model),
        "--wav",
        str(chunk["wav_path"]),
        "--output",
        str(output),
        *extra,
    ]


def child_environment(protocol):
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    model = Path(protocol.get("asr_model_path") or protocol.get("aligner_model_path") or ".")
    cache = model.parents[2] / "cache" / "qwen-huggingface" if len(model.parents) >= 3 else None
    if cache is not None and cache.is_dir():
        env["HF_HOME"] = str(cache)
    return env


def launch_real(kind, chunk, output, protocol):
    command = worker_command(protocol, kind, chunk, output, output_path(output.parent.parent, "asr", chunk["chunk_id"]))
    log_dir = output.parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = log_dir / f"{kind}-{chunk['chunk_id']}.stderr"
    stdout_path = log_dir / f"{kind}-{chunk['chunk_id']}.stdout"
    started = time.monotonic()
    process = None
    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(
                command,
                env=child_environment(protocol),
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            returncode = process.wait(timeout=CHUNK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        raise StageBlocked(f"{kind} {chunk['chunk_id']} 超时，完成状态未知，未自动重试") from exc
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    return {
        "returncode": returncode,
        "stderr": stderr_text,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "command": command,
    }


def asr_identity(protocol, chunk, detected_language=None):
    identity = {
        "wav_sha256": chunk["wav_sha256"],
        "model_path": str(protocol["asr_model_path"]),
        "max_new_tokens": ASR_MAX_NEW_TOKENS,
        "language": "Chinese",
        "context": "",
        "dtype": "bfloat16",
        "draft_model": None,
    }
    if language_policy(protocol) != AUTO_LANGUAGE_POLICY:
        return identity
    identity["language"] = None
    identity["transcription_language_policy"] = AUTO_LANGUAGE_POLICY
    identity["detected_language"] = detected_language
    return identity


def align_identity(protocol, chunk, asr_sha, asr_record=None):
    identity = {
        "wav_sha256": chunk["wav_sha256"],
        "asr_raw_sha256": asr_sha,
        "model_path": str(protocol["aligner_model_path"]),
        "language": "Chinese",
        "dtype": "bfloat16",
        "draft_model": None,
    }
    policy = language_policy(protocol)
    if policy != AUTO_LANGUAGE_POLICY:
        return identity
    detected = ""
    if isinstance(asr_record, dict) and isinstance(asr_record.get("language_detected"), str):
        detected = asr_record["language_detected"]
    routed, _blocked = align_route(asr_record or {}, policy)
    identity["language"] = routed if routed is not None else detected
    identity["transcription_language_policy"] = AUTO_LANGUAGE_POLICY
    identity["detected_language"] = detected
    return identity


def parse_asr_record(record, policy=None):
    raw = record.get("raw_result") or {}
    text = raw.get("text")
    truncated = bool(raw.get("truncated")) or raw.get("finish_reason") == "length"
    problems = []
    if policy == AUTO_LANGUAGE_POLICY:
        if record.get("transcription_language_policy") != AUTO_LANGUAGE_POLICY:
            problems.append("language_policy")
        if "language_forced" not in record or record.get("language_forced") is not None:
            problems.append("language_forced")
        if not isinstance(record.get("language_detected"), str):
            problems.append("language_detected")
        if record.get("language_granularity") != "chunk":
            problems.append("language_granularity")
    else:
        if record.get("language_forced") != "Chinese":
            problems.append("language")
        if record.get("transcription_language_policy") not in (None, ""):
            problems.append("language_policy")
    if record.get("context") != "":
        problems.append("context")
    if record.get("max_new_tokens") != ASR_MAX_NEW_TOKENS:
        problems.append("max_new_tokens")
    if record.get("dtype") != "bfloat16":
        problems.append("dtype")
    if record.get("alignment_executed") is not False:
        problems.append("alignment_executed")
    if not isinstance(text, str):
        problems.append("text")
    if not isinstance(record.get("mlx_peak_allocated_bytes"), int):
        problems.append("mlx_peak")
    return text, truncated, problems


def parse_align_record(record, expected_text, sample_count, duration, expected_language="Chinese", policy=None):
    problems = []
    units = record.get("units")
    if record.get("input_text") != expected_text:
        problems.append("input_text")
    if record.get("language") != expected_language or record.get("dtype") != "bfloat16":
        problems.append("language_or_dtype")
    if policy is None:
        if record.get("transcription_language_policy") not in (None, ""):
            problems.append("language_policy")
    elif record.get("transcription_language_policy") != AUTO_LANGUAGE_POLICY or record.get("language_granularity") != "chunk":
        problems.append("language_policy")
    if record.get("alignment_executed") is not True or record.get("asr_inference_executed") is not False:
        problems.append("execution_flags")
    if record.get("audio_samples") != sample_count or record.get("audio_rate") != SAMPLE_RATE:
        problems.append("audio_shape")
    if not isinstance(record.get("mlx_peak_allocated_bytes"), int):
        problems.append("mlx_peak")
    if not isinstance(units, list):
        problems.append("units")
        return problems
    capture = record.get("parser_capture") or {}
    slots = capture.get("raw_slot_predictions_ms")
    if not isinstance(slots, list) or len(slots) != 2 * len(units):
        problems.append("parser_capture")
    if not units_match_words(units, expected_text, expected_language):
        problems.append("text_coverage")
    elif align_clean("".join(unit.get("text", "") for unit in units)) != align_clean(expected_text):
        problems.append("text_coverage")
    overflow = out_of_bounds_units(units, duration)
    if overflow:
        problems.append("last-unit-out-of-bounds" if any(item["is_last"] for item in overflow) else "unit-out-of-bounds")
    previous = 0.0
    for unit in units:
        start = unit.get("start_time")
        end = unit.get("end_time")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            problems.append("timestamps")
            break
        if not (start <= end and start >= previous - TIMESTAMP_TOLERANCE_SECONDS):
            problems.append("timestamps")
            break
        previous = end
    return problems


def out_of_bounds_units(units, duration):
    found = []
    if not isinstance(units, list) or not units:
        return found
    limit = float(duration) + TIMESTAMP_TOLERANCE_SECONDS
    for index, unit in enumerate(units):
        start = unit.get("start_time")
        end = unit.get("end_time")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        if start < -TIMESTAMP_TOLERANCE_SECONDS or end > limit:
            found.append(
                {
                    "unit_index": index,
                    "text": unit.get("text"),
                    "start_time": start,
                    "end_time": end,
                    "is_last": index == len(units) - 1,
                }
            )
    return found


def alignment_view(record, protocol, text, sample_count, duration, expected_language="Chinese"):
    """Derive bounded search times; never edit the model response or its words."""
    policy = protocol.get("alignment_time_policy")
    if policy is None:
        return record, None
    if policy != TAIL_TIME_POLICY:
        raise StageBlocked("未知对齐时间派生规则")
    problems = parse_align_record(record, text, sample_count, duration, expected_language, language_policy(protocol))
    if problems != ["last-unit-out-of-bounds"]:
        return record, None
    units = record["units"]
    overflow = out_of_bounds_units(units, duration)
    if len(overflow) != 1 or not overflow[0]["is_last"]:
        return record, None
    last = units[-1]
    start, end = last["start_time"], last["end_time"]
    if not (0 <= start < duration < end and end - duration <= 0.080 + 1e-12):
        return record, None
    model = protocol.get("aligner_model_path", "")
    if Path(model).name != ALIGNER_REVISION or record.get("model_path") != model:
        raise StageBlocked("尾点派生只适用于已确认的固定对齐模型")
    derived = copy.deepcopy(record)
    derived["units"][-1]["end_time"] = duration
    if parse_align_record(derived, text, sample_count, duration, expected_language, language_policy(protocol)):
        raise StageBlocked("尾点派生后仍未通过原1ms时间范围及原文校验")
    receipt = {
        "version": TAIL_TIME_POLICY,
        "unit_index": len(units) - 1,
        "text": last["text"],
        "raw_start_seconds": start,
        "raw_end_seconds": end,
        "derived_end_seconds": duration,
        "end_retracted_ticks": int(round((end - duration) * TICKS_PER_SECOND)),
        "raw_validation_failure": problems,
        "raw_preserved": True,
        "transcription_accuracy_verified": False,
    }
    return derived, receipt


def reuse_succeeded(task, identity, output):
    if task["identity"] != identity:
        raise StageBlocked(f"{task['chunk_id']} 的输入哈希与成功检查点不同，未重新推理")
    if not output.is_file() or file_sha256(output) != task.get("output_sha256"):
        raise StageBlocked(f"{task['chunk_id']} 的成功输出哈希不一致，未重新推理")
    return load_json(output)


def reuse_unsupported_skip(task, identity, output, text, asr_record):
    """Reuse a recorded language gap. Do not treat it as alignment or empty text."""
    if task.get("identity") != identity:
        raise StageBlocked(f"{task['chunk_id']} 的语言跳过与当前输入不一致，未自动改写")
    if not output.is_file() or file_sha256(output) != task.get("output_sha256"):
        raise StageBlocked(f"{task['chunk_id']} 的语言跳过输出哈希不一致，未自动改写")
    record = load_json(output)
    detected = asr_record.get("language_detected")
    if (
        record.get("alignment_executed") is not False
        or record.get("skipped") is not True
        or record.get("skip_reason") != "language-unsupported"
        or record.get("skip_policy") != UNSUPPORTED_ALIGN_SKIP_POLICY
        or record.get("input_text") != text
        or record.get("units") != []
        or record.get("detected_language") != detected
        or record.get("language") != detected
    ):
        raise StageBlocked(f"{task['chunk_id']} 的语言跳过记录不完整，未当成对齐成功")
    if record.get("language") == "Chinese" and detected != "Chinese":
        raise StageBlocked(f"{task['chunk_id']} 的语言跳过被改写成中文")
    return record


def reject_if_blocked(tasks, allow=None):
    for task in tasks:
        if task.get("status") == "succeeded":
            continue
        if allow is not None and allow(task):
            continue
        raise StageBlocked(
            f"{task.get('chunk_id')} 检查点为 {task.get('status')}（{task.get('reason')}），未自动重试"
        )


def stage_revalidate_tail(run):
    """Offline validation in a new run; preserve the failed source run intact."""
    run = Path(run).resolve()
    lock = acquire_lock(run)
    try:
        protocol = load_protocol(run)
        if protocol.get("alignment_time_policy") != TAIL_TIME_POLICY:
            raise StageBlocked("未启用用户确认的尾点派生规则")
        origin = protocol.get("recovery_origin") or {}
        original = Path(origin.get("run", ".")).resolve()
        ledger = original / "align-tasks.json"
        if original == run or not ledger.is_file() or file_sha256(ledger) != origin.get("align_tasks_sha256"):
            raise StageBlocked("必须在保留原失败记录的新运行目录复核")
        original_tasks = {task["chunk_id"]: task for task in load_json(ledger)}
        tasks = load_tasks(run, "align")
        chunks = {chunk["chunk_id"]: chunk for chunk in load_chunks(run)}
        changes = []
        updated = copy.deepcopy(tasks)
        for task in updated:
            if task.get("status") == "succeeded":
                continue
            source_task = original_tasks.get(task["chunk_id"])
            if task != source_task or task.get("status") != "failed" or task.get("reason") != "validation:last-unit-out-of-bounds":
                raise StageBlocked("只离线复核已知且仅末字越界的原失败；未知或其它失败仍停止")
            chunk = chunks[task["chunk_id"]]
            text, asr_sha = asr_text_for_chunk(run, chunk)
            path = output_path(run, "align", chunk["chunk_id"])
            asr_record = load_json(output_path(run, "asr", chunk["chunk_id"]))
            routed_language, route_error = align_route(asr_record, language_policy(protocol))
            if route_error:
                raise StageBlocked("原ASR语言无法对齐，不能复核尾点")
            record = reuse_succeeded(task, align_identity(protocol, chunk, asr_sha, asr_record), path)
            view, receipt = alignment_view(record, protocol, text,
                int(chunk["end_sample"]) - int(chunk["start_sample"]), float(chunk["duration_seconds"]), routed_language)
            if receipt is None or parse_align_record(view, text, record["audio_samples"], float(chunk["duration_seconds"]), routed_language, language_policy(protocol)):
                raise StageBlocked("原失败不满足有限尾点派生条件，未重新推理")
            task.update(status="succeeded", reason="derived-tail-boundary-validated",
                        original_validation_failure=source_task, time_boundary_derivation=receipt,
                        text_sha256=text_sha256(text))
            changes.append({"chunk_id": task["chunk_id"], "raw_sha256": task["output_sha256"], **receipt})
        if not changes:
            raise StageBlocked("没有可离线复核的原失败，不重复执行恢复")
        save_json(run / "tail-revalidation.json", {
            "version": TAIL_TIME_POLICY, "origin": origin, "changes": changes,
            "new_model_inputs": 0, "original_failures_preserved": True,
        })
        record_task(run, "align", updated)
        if file_sha256(ledger) != origin["align_tasks_sha256"]:
            raise StageBlocked("原失败记录发生变化")
        print(f"tail revalidation derived={len(changes)} new_model_inputs=0", flush=True)
        return changes
    finally:
        release_lock(lock)


def finish_model_task(run, stage, tasks, task, status, reason, outcome, peak, output):
    task.update(
        status=status,
        reason=reason,
        elapsed_seconds=outcome.get("elapsed_seconds"),
        peak_rss_bytes=parse_peak_rss(outcome.get("stderr") or ""),
        mlx_peak_allocated_bytes=peak,
        output_sha256=file_sha256(output) if output.is_file() else None,
    )
    record_task(run, stage, tasks)
    record_metric(
        run,
        stage,
        {
            "stage": stage,
            "chunk_id": task["chunk_id"],
            "status": status,
            "reason": reason,
            "elapsed_seconds": task["elapsed_seconds"],
            "peak_rss_bytes": task["peak_rss_bytes"],
            "mlx_peak_allocated_bytes": peak,
            "skipped_model": False,
        },
    )


def stage_asr(run, launch=None):
    if launch is None:
        launch = launch_real
    run = Path(run)
    lock = acquire_lock(run)
    try:
        protocol = load_protocol(run)
        policy = language_policy(protocol)
        require_asr_budget(protocol)
        if launch is launch_real:
            if not Path(protocol.get("asr_model_path", "")).is_dir():
                raise StageBlocked("asr_model_path 不存在")
            if not Path(protocol.get("worker_python", "")).is_file():
                raise StageBlocked("worker_python 不存在")
        chunks = load_chunks(run)
        tasks = load_tasks(run, "asr")
        reject_if_blocked(tasks)
        for chunk in chunks:
            identity = asr_identity(protocol, chunk)
            destination = output_path(run, "asr", chunk["chunk_id"])
            prior = find_task(tasks, chunk["chunk_id"])
            if prior is not None:
                record = load_json(destination) if destination.is_file() else {}
                identity = asr_identity(protocol, chunk, record.get("language_detected"))
                reuse_succeeded(prior, identity, destination)
                text, truncated, problems = parse_asr_record(record, policy)
                if truncated or problems or text is None:
                    raise StageBlocked(f"{chunk['chunk_id']} 的成功 ASR 记录无法复核，未重新推理")
                print(f"asr {chunk['chunk_id']} reuse", flush=True)
                continue
            if destination.exists():
                raise StageBlocked(f"{chunk['chunk_id']} 存在无检查点的 ASR 输出，未自动重跑")
            task = {"chunk_id": chunk["chunk_id"], "status": "started", "identity": identity, "reason": None}
            tasks.append(task)
            record_task(run, "asr", tasks)
            try:
                outcome = launch("asr", chunk, destination, protocol)
            except StageBlocked:
                task.update(status="unknown", reason="timeout")
                record_task(run, "asr", tasks)
                raise
            except Exception as exc:
                task.update(status="unknown", reason=type(exc).__name__)
                record_task(run, "asr", tasks)
                raise StageBlocked(f"{chunk['chunk_id']} ASR 完成状态未知，未自动重试") from exc
            if not destination.is_file():
                finish_model_task(run, "asr", tasks, task, "unknown", "missing-output", outcome, None, destination)
                raise StageBlocked(f"{chunk['chunk_id']} ASR 没有输出，完成状态未知，未自动重试")
            try:
                record = load_json(destination)
                text, truncated, problems = parse_asr_record(record, policy)
                peak = record.get("mlx_peak_allocated_bytes")
            except Exception as exc:
                finish_model_task(run, "asr", tasks, task, "unknown", "unreadable-output", outcome, None, destination)
                raise StageBlocked(f"{chunk['chunk_id']} ASR 输出无法读取，未自动重试") from exc
            if truncated:
                finish_model_task(run, "asr", tasks, task, "failed", "truncated", outcome, peak, destination)
                raise StageBlocked(f"{chunk['chunk_id']} ASR 截断，未当作完整转录，也未自动重试")
            if outcome["returncode"] != 0 or problems or text is None:
                reason = "process" if outcome["returncode"] != 0 else "validation:" + ",".join(problems)
                status = "unknown" if outcome["returncode"] != 0 else "failed"
                finish_model_task(run, "asr", tasks, task, status, reason, outcome, peak, destination)
                raise StageBlocked(f"{chunk['chunk_id']} ASR {status}（{reason}），未自动重试")
            task["identity"] = asr_identity(protocol, chunk, record.get("language_detected"))
            task["text_sha256"] = text_sha256(text)
            finish_model_task(
                run,
                "asr",
                tasks,
                task,
                "succeeded",
                str(record["raw_result"].get("finish_reason")),
                outcome,
                peak,
                destination,
            )
            print(f"asr {chunk['chunk_id']} succeeded {outcome['elapsed_seconds']}s", flush=True)
        return tasks
    finally:
        release_lock(lock)


def asr_text_for_chunk(run, chunk):
    path = output_path(run, "asr", chunk["chunk_id"])
    if not path.is_file():
        raise StageBlocked(f"{chunk['chunk_id']} 缺少 ASR 输出")
    text, truncated, problems = parse_asr_record(load_json(path), language_policy(load_protocol(run)))
    if truncated or problems or text is None:
        raise StageBlocked(f"{chunk['chunk_id']} 的 ASR 不是可对齐的完整结果")
    return text, file_sha256(path)


def write_unsupported_align_skip(path, asr_sha, text, detected, sample_count, derived_from=None):
    """Keep the ASR text and record a skip. This is not an alignment and not an empty transcript."""
    body = {
        "asr_raw_sha256": asr_sha,
        "input_text": text,
        "input_text_sha256": text_sha256(text),
        "language": detected,
        "detected_language": detected,
        "dtype": "bfloat16",
        "backend": None,
        "audio_samples": sample_count,
        "audio_rate": SAMPLE_RATE,
        "units": [],
        "parser_capture": None,
        "alignment_executed": False,
        "asr_inference_executed": False,
        "skipped": True,
        "skip_reason": "language-unsupported",
        "skip_policy": UNSUPPORTED_ALIGN_SKIP_POLICY,
        "transcription_language_policy": AUTO_LANGUAGE_POLICY,
        "language_granularity": "chunk",
        "mlx_peak_allocated_bytes": None,
    }
    if derived_from is not None:
        body["derived_from"] = derived_from
    save_json(path, body)
    return body


def write_skip_alignment(path, asr_sha, text, reason, sample_count, language="Chinese", policy=None):
    save_json(
        path,
        {
            "asr_raw_sha256": asr_sha,
            "input_text": text,
            "language": language,
            "dtype": "bfloat16",
            "backend": None,
            "audio_samples": sample_count,
            "audio_rate": SAMPLE_RATE,
            "units": [],
            "parser_capture": None,
            "alignment_executed": False,
            "asr_inference_executed": False,
            "skip_reason": reason,
            **({"transcription_language_policy": policy, "language_granularity": "chunk"} if policy else {}),
            "mlx_peak_allocated_bytes": None,
        },
    )


def stage_align(run, launch=None):
    if launch is None:
        launch = launch_real
    run = Path(run)
    lock = acquire_lock(run)
    try:
        protocol = load_protocol(run)
        policy = language_policy(protocol)
        if launch is launch_real:
            if not Path(protocol.get("aligner_model_path", "")).is_dir():
                raise StageBlocked("aligner_model_path 不存在")
            if not Path(protocol.get("worker_python", "")).is_file():
                raise StageBlocked("worker_python 不存在")
        chunks = load_chunks(run)
        asr_tasks = load_tasks(run, "asr")
        reject_if_blocked(asr_tasks)
        if {task["chunk_id"] for task in asr_tasks if task.get("status") == "succeeded"} != {c["chunk_id"] for c in chunks}:
            raise StageBlocked("ASR 尚未全部成功，未开始对齐")
        tasks = load_tasks(run, "align")
        unsupported_align_policy(protocol)
        reject_if_blocked(tasks, allow=lambda task: is_unsupported_skip_task(task, protocol))
        for chunk in chunks:
            text, asr_sha = asr_text_for_chunk(run, chunk)
            asr_record = load_json(output_path(run, "asr", chunk["chunk_id"]))
            routed_language, route_error = align_route(asr_record, policy)
            identity = align_identity(protocol, chunk, asr_sha, asr_record)
            destination = output_path(run, "align", chunk["chunk_id"])
            prior = find_task(tasks, chunk["chunk_id"])
            sample_count = int(chunk["end_sample"]) - int(chunk["start_sample"])
            if prior is not None:
                if is_unsupported_skip_task(prior, protocol):
                    if not unsupported_align_skip_applies(asr_record, protocol):
                        raise StageBlocked(f"{chunk['chunk_id']} 的语言跳过与当前 ASR 不一致，未自动改写")
                    reuse_unsupported_skip(prior, identity, destination, text, asr_record)
                    print(f"align {chunk['chunk_id']} reuse language-unsupported", flush=True)
                    continue
                reuse_succeeded(prior, identity, destination)
                print(f"align {chunk['chunk_id']} reuse", flush=True)
                continue
            if route_error:
                if unsupported_align_skip_applies(asr_record, protocol):
                    if destination.exists():
                        raise StageBlocked(f"{chunk['chunk_id']} 存在无检查点的对齐输出，未自动重跑")
                    write_unsupported_align_skip(
                        destination, asr_sha, text, asr_record.get("language_detected"), sample_count,
                    )
                    task = {
                        "chunk_id": chunk["chunk_id"],
                        "status": "skipped",
                        "identity": identity,
                        "reason": "language-unsupported",
                        "skip_policy": UNSUPPORTED_ALIGN_SKIP_POLICY,
                        "alignment_executed": False,
                        "detected_language": asr_record.get("language_detected"),
                        "elapsed_seconds": 0,
                        "peak_rss_bytes": None,
                        "mlx_peak_allocated_bytes": None,
                        "output_sha256": file_sha256(destination),
                        "text_sha256": text_sha256(text),
                    }
                    tasks.append(task)
                    record_task(run, "align", tasks)
                    record_metric(
                        run,
                        "align",
                        {
                            "stage": "align",
                            "chunk_id": chunk["chunk_id"],
                            "status": "skipped",
                            "reason": "language-unsupported",
                            "elapsed_seconds": 0,
                            "peak_rss_bytes": None,
                            "mlx_peak_allocated_bytes": None,
                            "skipped_model": True,
                        },
                    )
                    print(f"align {chunk['chunk_id']} skip language-unsupported", flush=True)
                    continue
                tasks.append({"chunk_id": chunk["chunk_id"], "status": "failed", "identity": identity,
                              "reason": route_error, "detected_language": asr_record.get("language_detected")})
                record_task(run, "align", tasks)
                raise StageBlocked(f"{chunk['chunk_id']} 对齐语言不可用（{route_error}），未回退中文")
            if destination.exists():
                raise StageBlocked(f"{chunk['chunk_id']} 存在无检查点的对齐输出，未自动重跑")
            if not substantive(text):
                reason = "empty" if text == "" else "non_substantive"
                write_skip_alignment(destination, asr_sha, text, reason, sample_count, routed_language, policy)
                task = {
                    "chunk_id": chunk["chunk_id"],
                    "status": "succeeded",
                    "identity": identity,
                    "reason": reason,
                    "elapsed_seconds": 0,
                    "peak_rss_bytes": None,
                    "mlx_peak_allocated_bytes": None,
                    "output_sha256": file_sha256(destination),
                    "text_sha256": text_sha256(text),
                }
                tasks.append(task)
                record_task(run, "align", tasks)
                record_metric(
                    run,
                    "align",
                    {
                        "stage": "align",
                        "chunk_id": chunk["chunk_id"],
                        "status": "succeeded",
                        "reason": reason,
                        "elapsed_seconds": 0,
                        "peak_rss_bytes": None,
                        "mlx_peak_allocated_bytes": None,
                        "skipped_model": True,
                    },
                )
                print(f"align {chunk['chunk_id']} skip {reason}", flush=True)
                continue
            task = {"chunk_id": chunk["chunk_id"], "status": "started", "identity": identity, "reason": None}
            tasks.append(task)
            record_task(run, "align", tasks)
            try:
                outcome = launch("align", chunk, destination, protocol)
            except StageBlocked:
                task.update(status="unknown", reason="timeout")
                record_task(run, "align", tasks)
                raise
            except Exception as exc:
                task.update(status="unknown", reason=type(exc).__name__)
                record_task(run, "align", tasks)
                raise StageBlocked(f"{chunk['chunk_id']} 对齐完成状态未知，未自动重试") from exc
            if not destination.is_file():
                finish_model_task(run, "align", tasks, task, "unknown", "missing-output", outcome, None, destination)
                raise StageBlocked(f"{chunk['chunk_id']} 对齐没有输出，完成状态未知，未自动重试")
            try:
                record = load_json(destination)
                view, receipt = alignment_view(record, protocol, text, sample_count, float(chunk["duration_seconds"]), routed_language)
                problems = parse_align_record(view, text, sample_count, float(chunk["duration_seconds"]), routed_language, policy)
                if receipt is not None:
                    task["time_boundary_derivation"] = receipt
                peak = record.get("mlx_peak_allocated_bytes")
            except Exception as exc:
                finish_model_task(run, "align", tasks, task, "unknown", "unreadable-output", outcome, None, destination)
                raise StageBlocked(f"{chunk['chunk_id']} 对齐输出无法读取，未自动重试") from exc
            overflow = out_of_bounds_units(record.get("units") or [], float(chunk["duration_seconds"]))
            if overflow:
                task["out_of_bounds_units"] = overflow
                task["retained_units"] = record.get("units")
                task["retained_text"] = record.get("input_text")
            if outcome["returncode"] != 0 or problems:
                reason = "process" if outcome["returncode"] != 0 else "validation:" + ",".join(problems)
                status = "unknown" if outcome["returncode"] != 0 else "failed"
                finish_model_task(run, "align", tasks, task, status, reason, outcome, peak, destination)
                raise StageBlocked(f"{chunk['chunk_id']} 对齐 {status}（{reason}），未自动重试")
            task["text_sha256"] = text_sha256(text)
            finish_model_task(run, "align", tasks, task, "succeeded", "aligned", outcome, peak, destination)
            print(f"align {chunk['chunk_id']} succeeded {outcome['elapsed_seconds']}s", flush=True)
        return tasks
    finally:
        release_lock(lock)


def split_positions(text, start, end, delimiters):
    result = []
    position = start
    pattern = "[" + re.escape(delimiters) + "]+"
    for match in re.finditer(pattern, text[start:end]):
        boundary = start + match.end()
        if canonical(text[position:boundary]):
            result.append((position, boundary))
            position = boundary
    if position < end:
        if canonical(text[position:end]):
            result.append((position, end))
        elif result:
            result[-1] = (result[-1][0], end)
    if "".join(text[left:right] for left, right in result) != text[start:end]:
        raise StageBlocked("标点切分没有保住原文字")
    return result


def playback_bounds(content_start, content_end, total):
    low = max(0.0, content_start - CONTEXT_SECONDS)
    high = min(total, content_end + CONTEXT_SECONDS)
    if high - low < MINIMUM_PLAYBACK_SECONDS:
        middle = (high + low) / 2
        low = middle - MINIMUM_PLAYBACK_SECONDS / 2
        high = middle + MINIMUM_PLAYBACK_SECONDS / 2
        if low < 0:
            high -= low
            low = 0
        if high > total:
            low -= high - total
            high = total
    return round(low, 8), round(high, 8)


def repaired_indices(record):
    units = record.get("units") or []
    capture = record.get("parser_capture") or {}
    slots = capture.get("raw_slot_predictions_ms") or []
    found = []
    if len(slots) != 2 * len(units):
        return found
    for index, unit in enumerate(units):
        delta = max(
            abs(unit["start_time"] - slots[2 * index] / 1000),
            abs(unit["end_time"] - slots[2 * index + 1] / 1000),
        )
        if delta > 0.001:
            found.append(index)
    return found


def candidate_row(chunk, protocol, text, start, end, low, high, provenance):
    runtime = int(protocol["source"]["runtimeTicks"])
    start_ticks = absolute_ticks(chunk["start_sample"], low)
    end_ticks = absolute_ticks(chunk["start_sample"], high)
    chunk_end = int(chunk["end_sample"]) * TICKS_PER_SAMPLE
    if not (0 <= start_ticks < end_ticks <= runtime and end_ticks <= chunk_end):
        raise StageBlocked(
            f"{chunk['chunk_id']} 源时间不合法：{start_ticks}-{end_ticks}，片长 {runtime}，块结束 {chunk_end}"
        )
    duration_ticks = end_ticks - start_ticks
    return {
        "candidate_id": provenance["candidate_id"],
        "text": text[start:end] if end is not None else text,
        "start_ticks": start_ticks,
        "end_ticks": end_ticks,
        "localization_eligible": MINIMUM_PLAYBACK_SECONDS * TICKS_PER_SECOND <= duration_ticks <= MAXIMUM_LOCALIZATION_SECONDS * TICKS_PER_SECOND,
        "overlong_retained": provenance["overlong_retained"],
        "provenance": provenance,
    }


def non_substantive_retention(chunk, asr_text, align_record, asr_sha, align_sha):
    """Keep the original text and skip record without creating an embeddable utterance."""
    if substantive(asr_text):
        return None
    reason = "empty" if asr_text == "" else (align_record.get("skip_reason") or "non_substantive")
    return {
        "chunk_id": chunk["chunk_id"],
        "text": asr_text,
        "valid_utterance": False,
        "embed": False,
        "skip_reason": reason,
        "asr_record": f"asr/{chunk['chunk_id']}.json",
        "asr_raw_sha256": asr_sha,
        "align_record": f"align/{chunk['chunk_id']}.json",
        "align_raw_sha256": align_sha,
        "alignment_executed": bool(align_record.get("alignment_executed")),
    }


def documents_for_chunk(chunk, protocol, asr_text, align_record, asr_sha, align_sha):
    if align_record.get("input_text") != asr_text:
        raise StageBlocked(f"{chunk['chunk_id']} 对齐原文与 ASR 原文不一致")
    if not substantive(asr_text):
        return []
    if align_record.get("alignment_executed") is not True:
        raise StageBlocked(f"{chunk['chunk_id']} 有实质文本但没有对齐")
    policy = language_policy(protocol)
    detected = align_record.get("language", "Chinese")
    view, boundary = alignment_view(
        align_record, protocol, asr_text,
        int(chunk["end_sample"]) - int(chunk["start_sample"]), float(chunk["duration_seconds"]), detected,
    )
    units = view["units"]
    overflow = out_of_bounds_units(units, float(chunk["duration_seconds"]))
    if overflow:
        kind = "最后单元越界" if any(item["is_last"] for item in overflow) else "单元越界"
        kept = "、".join(item.get("text") or "" for item in overflow)
        raise UnitBoundsRejected(
            f"{chunk['chunk_id']} {kind}，已保留原始单元未删词：{kept}",
            {
                "chunk_id": chunk["chunk_id"],
                "text": asr_text,
                "units": units,
                "out_of_bounds_units": overflow,
            },
        )
    positions = [index for index, character in enumerate(asr_text) if character.isalnum()]
    if canonical(asr_text) != "".join(canonical(unit["text"]) for unit in units):
        raise StageBlocked(f"{chunk['chunk_id']} 对齐单元没有覆盖 ASR 原文")
    spans = []
    offset = 0
    for unit in units:
        size = len(canonical(unit["text"]))
        if size <= 0:
            raise StageBlocked(f"{chunk['chunk_id']} 存在没有文字的对齐单元")
        spans.append((positions[offset], positions[offset + size - 1] + 1))
        offset += size
    if offset != len(positions):
        raise StageBlocked(f"{chunk['chunk_id']} 对齐单元与原文字符没有对齐")

    def info(start, end):
        chosen = [index for index, (span_start, span_end) in enumerate(spans) if span_start < end and span_end > start]
        if not chosen:
            raise StageBlocked(f"{chunk['chunk_id']} 有文字段找不到对齐单元")
        return chosen, units[chosen[0]]["start_time"], units[chosen[-1]]["end_time"]

    atoms = []
    for start, end in split_positions(asr_text, 0, len(asr_text), primary_delimiters_for(detected) if policy else PRIMARY_DELIMITERS):
        _ids, content_start, content_end = info(start, end)
        if content_end - content_start > CONTENT_LIMIT_SECONDS:
            atoms.extend(split_positions(asr_text, start, end, OVERLONG_DELIMITERS))
        else:
            atoms.append((start, end))
    packed = []
    for start, end in atoms:
        if packed:
            _ids, content_start, content_end = info(packed[-1][0], end)
            if content_end - content_start <= CONTENT_LIMIT_SECONDS:
                packed[-1] = (packed[-1][0], end)
                continue
        packed.append((start, end))
    if "".join(asr_text[start:end] for start, end in packed) != asr_text:
        raise StageBlocked(f"{chunk['chunk_id']} 分段后原文字丢失")
    total = float(chunk["duration_seconds"])
    repairs = set(repaired_indices(align_record))
    rows = []
    for number, (start, end) in enumerate(packed, 1):
        ids, content_start, content_end = info(start, end)
        low, high = playback_bounds(content_start, content_end, total)
        provenance = _provenance(
            chunk,
            protocol,
            asr_sha,
            align_sha,
            asr_text,
            start,
            end,
            ids,
            True,
            None,
            content_start,
            content_end,
            low,
            high,
            content_end - content_start > CONTENT_LIMIT_SECONDS,
            sorted(repairs.intersection(ids)),
            [index for index in ids if units[index]["start_time"] == units[index]["end_time"]],
            number,
        )
        if policy:
            provenance["transcription_language_policy"] = policy
            provenance["detected_language"] = detected
            provenance["language_granularity"] = "chunk"
        if protocol.get("alignment_time_policy"):
            provenance["alignment_time_policy"] = protocol["alignment_time_policy"]
        if boundary is not None and boundary["unit_index"] in ids:
            provenance["time_boundary_derivation"] = boundary
        rows.append(candidate_row(chunk, protocol, asr_text, start, end, low, high, provenance))
    return rows


def _provenance(
    chunk,
    protocol,
    asr_sha,
    align_sha,
    asr_text,
    start,
    end,
    unit_indices,
    executed,
    skip_reason,
    content_start,
    content_end,
    playback_start,
    playback_end,
    overlong,
    repaired,
    zero_duration,
    number,
):
    return {
        "candidate_id": f"{chunk['chunk_id']}-U{number:02d}",
        "source_item_id": protocol["source"]["sourceItemId"],
        "content_version": protocol["source"]["contentVersion"],
        "runtime_ticks": int(protocol["source"]["runtimeTicks"]),
        "chunk_id": chunk["chunk_id"],
        "start_sample": int(chunk["start_sample"]),
        "end_sample": int(chunk["end_sample"]),
        "asr_record": f"asr/{chunk['chunk_id']}.json",
        "asr_raw_sha256": asr_sha,
        "align_record": f"align/{chunk['chunk_id']}.json",
        "align_raw_sha256": align_sha,
        "original_char_start": start,
        "original_char_end": end,
        "unit_indices": list(unit_indices),
        "input_text_sha256": text_sha256(asr_text),
        "alignment_executed": executed,
        "skip_reason": skip_reason,
        "content_start_seconds": content_start,
        "content_end_seconds": content_end,
        "playback_start_seconds": playback_start,
        "playback_end_seconds": playback_end,
        "library_repaired_unit_indices": list(repaired),
        "zero_duration_unit_indices": list(zero_duration),
        "segmentation_version": SEGMENTATION_VERSION,
        "overlong_retained": overlong,
    }


def language_gap_record(chunk, asr_text, align_record, asr_sha, align_sha):
    """A substantive chunk the aligner cannot time. The text stays; it is not a candidate."""
    detected = align_record.get("detected_language")
    if align_record.get("input_text") != asr_text or not substantive(asr_text):
        raise StageBlocked(f"{chunk['chunk_id']} 的语言缺口没有保住原文字")
    if (
        align_record.get("alignment_executed") is not False
        or align_record.get("skipped") is not True
        or align_record.get("skip_reason") != "language-unsupported"
        or align_record.get("skip_policy") != UNSUPPORTED_ALIGN_SKIP_POLICY
        or align_record.get("units") != []
        or align_record.get("language") != detected
        or detected in (None, "")
    ):
        raise StageBlocked(f"{chunk['chunk_id']} 的语言缺口不像一次明确跳过")
    if align_record.get("language") == "Chinese" and detected != "Chinese":
        raise StageBlocked(f"{chunk['chunk_id']} 的语言缺口被改写成中文")
    if "start_ticks" not in chunk or "end_ticks" not in chunk:
        raise StageBlocked(f"{chunk['chunk_id']} 缺少时间范围，不能记语言缺口")
    return {
        "chunk_id": chunk["chunk_id"],
        "text": asr_text,
        "start_ticks": int(chunk["start_ticks"]),
        "end_ticks": int(chunk["end_ticks"]),
        "reason": "language-unsupported",
        "detected_language": detected,
        "skip_policy": UNSUPPORTED_ALIGN_SKIP_POLICY,
        "alignment_executed": False,
        "embed": False,
        "valid_utterance": False,
        "text_retained": True,
        "input_text_sha256": text_sha256(asr_text),
        "asr_record": f"asr/{chunk['chunk_id']}.json",
        "asr_raw_sha256": asr_sha,
        "align_record": f"align/{chunk['chunk_id']}.json",
        "align_raw_sha256": align_sha,
    }


def documents_identity(run, protocol, chunks):
    parts = [
        SEGMENTATION_VERSION,
        "non-substantive-retained-not-embedded",
        str(protocol["source"]["runtimeTicks"]),
        str(protocol["source"]["contentVersion"]),
    ]
    if language_policy(protocol):
        parts.append(language_policy(protocol))
    if protocol.get("alignment_time_policy"):
        parts.append(protocol["alignment_time_policy"])
    skip_policy = unsupported_align_policy(protocol)
    if skip_policy:
        parts.append(skip_policy)
    for chunk in chunks:
        parts.append(chunk["chunk_id"])
        parts.append(chunk["wav_sha256"])
        parts.append(file_sha256(output_path(run, "asr", chunk["chunk_id"])))
        parts.append(file_sha256(output_path(run, "align", chunk["chunk_id"])))
    return text_sha256("\n".join(parts))


def stage_documents(run):
    run = Path(run)
    lock = acquire_lock(run)
    state_path = run / "documents-state.json"
    try:
        protocol = load_protocol(run)
        chunks = load_chunks(run)
        skip_policy = unsupported_align_policy(protocol)
        for stage in ("asr", "align"):
            tasks = load_tasks(run, stage)
            if stage == "align":
                reject_if_blocked(tasks, allow=lambda task: is_unsupported_skip_task(task, protocol))
                settled = {
                    task["chunk_id"]
                    for task in tasks
                    if task.get("status") == "succeeded" or is_unsupported_skip_task(task, protocol)
                }
            else:
                reject_if_blocked(tasks)
                settled = {task["chunk_id"] for task in tasks if task.get("status") == "succeeded"}
            if settled != {c["chunk_id"] for c in chunks}:
                raise StageBlocked(f"{stage} 尚未全部成功，未分段")
        identity = documents_identity(run, protocol, chunks)
        candidates_path = run / "candidates.json"
        retained_path = run / "non-substantive.json"
        gaps_path = run / "align-language-gaps.json"
        if state_path.exists():
            state = load_json(state_path)
            if state.get("status") == "succeeded" and state.get("identity") == identity:
                retained_ok = retained_path.is_file() and file_sha256(retained_path) == state.get("non_substantive_sha256")
                candidates_ok = candidates_path.is_file() and file_sha256(candidates_path) == state.get("candidates_sha256")
                if skip_policy:
                    gaps_ok = gaps_path.is_file() and file_sha256(gaps_path) == state.get("gaps_sha256")
                elif gaps_path.exists():
                    raise StageBlocked("没有跳过策略时不应存在语言缺口文件")
                else:
                    gaps_ok = True
                if candidates_ok and retained_ok and gaps_ok:
                    print(f"documents reuse candidates={state.get('candidate_count')}", flush=True)
                    return load_json(candidates_path)
                raise StageBlocked("分段检查点与 candidates.json 不一致，未重新改写")
            raise StageBlocked(f"分段检查点为 {state.get('status')}，未自动重试")
        if gaps_path.exists():
            raise StageBlocked("语言缺口文件没有分段检查点，未自动重跑")
        save_json(state_path, {"status": "started", "identity": identity})
        try:
            rows = []
            retained = []
            gaps = []
            originals = []
            align_tasks = {task["chunk_id"]: task for task in load_tasks(run, "align")}
            for chunk in chunks:
                asr_text, asr_sha = asr_text_for_chunk(run, chunk)
                originals.append(asr_text)
                align_path = output_path(run, "align", chunk["chunk_id"])
                align_record = load_json(align_path)
                align_sha = file_sha256(align_path)
                task = align_tasks.get(chunk["chunk_id"])
                if is_unsupported_skip_task(task, protocol):
                    gaps.append(language_gap_record(chunk, asr_text, align_record, asr_sha, align_sha))
                    continue
                kept = non_substantive_retention(chunk, asr_text, align_record, asr_sha, align_sha)
                own = documents_for_chunk(chunk, protocol, asr_text, align_record, asr_sha, align_sha)
                if kept is None:
                    if "".join(row["text"] for row in own) != asr_text:
                        raise StageBlocked(f"{chunk['chunk_id']} 有效话语没有保住全部 ASR 字符")
                    if any(row.get("embed") is False for row in own):
                        raise StageBlocked(f"{chunk['chunk_id']} 把非实质文本送进了候选")
                else:
                    if own:
                        raise StageBlocked(f"{chunk['chunk_id']} 的非实质文本不应生成文字候选")
                    if kept["text"] != asr_text or kept["valid_utterance"] or kept["embed"]:
                        raise StageBlocked(f"{chunk['chunk_id']} 的非实质保留记录不完整")
                    retained.append(kept)
                rows.extend(own)
            covered = []
            gap_text = {gap["chunk_id"]: gap["text"] for gap in gaps}
            for chunk in chunks:
                covered.extend(row["text"] for row in rows if row["provenance"]["chunk_id"] == chunk["chunk_id"])
                covered.extend(row["text"] for row in retained if row["chunk_id"] == chunk["chunk_id"])
                if chunk["chunk_id"] in gap_text:
                    covered.append(gap_text[chunk["chunk_id"]])
            if "".join(covered) != "".join(originals):
                raise StageBlocked("有效话语、非实质保留和语言缺口没有覆盖全部 ASR 原文")
            if any(not substantive(row["text"]) for row in rows):
                raise StageBlocked("candidates.json 含标点或空白，不能送给文字 embedding")
            if any(gap["chunk_id"] in {row["provenance"]["chunk_id"] for row in rows} for gap in gaps):
                raise StageBlocked("语言缺口又生成了文字候选")
            save_json(candidates_path, rows)
            save_json(retained_path, retained)
            if skip_policy:
                save_json(gaps_path, gaps)
            elif gaps:
                raise StageBlocked("没有跳过策略时不能记录语言缺口")
            state_body = {
                "status": "succeeded",
                "identity": identity,
                "candidate_count": len(rows),
                "candidates_sha256": file_sha256(candidates_path),
                "non_substantive_count": len(retained),
                "non_substantive_sha256": file_sha256(retained_path),
                "segmentation_version": SEGMENTATION_VERSION,
                "per_clip_document_cap": None,
            }
            if skip_policy:
                state_body.update(
                    unsupported_align_language_policy=skip_policy,
                    text_coverage="gapped" if gaps else "full",
                    gap_count=len(gaps),
                    gaps_sha256=file_sha256(gaps_path),
                )
            save_json(state_path, state_body)
        except UnitBoundsRejected as exc:
            current = load_json(state_path) if state_path.exists() else {}
            if current.get("status") != "succeeded":
                save_json(
                    state_path,
                    {"status": "failed", "identity": identity, "reason": str(exc), "retained": exc.retained},
                )
            raise
        except Exception as exc:
            current = load_json(state_path) if state_path.exists() else {}
            if current.get("status") != "succeeded":
                save_json(state_path, {"status": "failed", "identity": identity, "reason": str(exc)})
            raise
        print(f"documents candidates={len(rows)} retained={len(retained)} gaps={len(gaps)}", flush=True)
        return rows
    finally:
        release_lock(lock)


def worker_asr(model, wav, output, max_new_tokens, policy=None):
    import importlib.metadata

    import mlx.core as mx
    from mlx_qwen3_asr import transcribe
    from mlx_qwen3_asr.load_models import load_model

    if int(max_new_tokens) != ASR_MAX_NEW_TOKENS:
        raise SystemExit("ASR cap 必须是 1024")
    if platform.machine() != "arm64" or not mx.metal.is_available() or mx.default_device() != mx.gpu:
        raise SystemExit("ASR 需要 arm64 Metal GPU")
    mx.reset_peak_memory()
    started = time.monotonic()
    loaded, _config = load_model(model, dtype=mx.bfloat16)
    load_seconds = time.monotonic() - started
    result = transcribe(
        wav,
        model=loaded,
        dtype=mx.bfloat16,
        language=asr_language_argument(policy),
        context="",
        max_new_tokens=ASR_MAX_NEW_TOKENS,
        return_timestamps=False,
        return_chunks=True,
        diarize=False,
        draft_model=None,
        verbose=False,
    )
    mx.synchronize()
    record = {
        "raw_result": dataclasses.asdict(result),
        "language_forced": asr_language_argument(policy),
        "context": "",
        "max_new_tokens": ASR_MAX_NEW_TOKENS,
        "dtype": "bfloat16",
        "model_path": model,
        "load_seconds": load_seconds,
        "load_and_transcribe_seconds": time.monotonic() - started,
        "mlx_peak_allocated_bytes": int(mx.get_peak_memory()),
        "architecture": platform.machine(),
        "device": str(mx.default_device()),
        "worker_version": importlib.metadata.version("mlx-qwen3-asr"),
        "mlx_version": importlib.metadata.version("mlx"),
        "alignment_executed": False,
    }
    if policy:
        record.update(transcription_language_policy=policy, language_detected=result.language or "",
                      language_granularity="chunk")
    save_json(Path(output), record)
    if result.truncated or result.finish_reason == "length":
        raise SystemExit(3)


def worker_align(model, wav, asr_json, output, policy=None):
    import importlib.metadata

    import mlx.core as mx
    from mlx_qwen3_asr.audio import load_audio_np
    from mlx_qwen3_asr.forced_aligner import ForcedAlignTextProcessor, ForcedAligner

    if platform.machine() != "arm64" or not mx.metal.is_available() or mx.default_device() != mx.gpu:
        raise SystemExit("对齐需要 arm64 Metal GPU")
    asr_path = Path(asr_json)
    asr_bytes = asr_path.read_bytes()
    asr = json.loads(asr_bytes)
    text, truncated, problems = parse_asr_record(asr, policy)
    routed_language, route_error = align_route(asr, policy)
    if truncated or problems or route_error:
        raise StageBlocked("对齐输入语言或ASR记录无效，未回退中文")
    captures = []
    original = ForcedAlignTextProcessor.parse_timestamp_ms.__func__

    def capture_parser(cls, words, timestamp_ms):
        parsed = original(cls, words, timestamp_ms)
        captures.append(
            {
                "words": list(words),
                "raw_slot_predictions_ms": timestamp_ms.tolist(),
                "default_returned_units": [dataclasses.asdict(item) for item in parsed],
            }
        )
        return parsed

    ForcedAlignTextProcessor.parse_timestamp_ms = classmethod(capture_parser)
    audio = load_audio_np(wav, sr=SAMPLE_RATE)
    mx.reset_peak_memory()
    started = time.monotonic()
    aligner = ForcedAligner(model, dtype=mx.bfloat16, backend="mlx")
    units = aligner.align(audio, text, routed_language)
    mx.synchronize()
    if len(captures) != 1:
        raise SystemExit("对齐解析次数异常")
    config = aligner._backend.config
    record = {
        "asr_raw_sha256": hashlib.sha256(asr_bytes).hexdigest(),
        "input_text": text,
        "language": routed_language,
        "dtype": "bfloat16",
        "backend": "mlx",
        "model_path": model,
        "audio_samples": int(len(audio)),
        "audio_rate": SAMPLE_RATE,
        "timestamp_segment_time_ms": config.timestamp_segment_time,
        "classifier_classes": config.classify_num,
        "units": [dataclasses.asdict(unit) for unit in units],
        "parser_capture": captures[0],
        "alignment_seconds_including_load": time.monotonic() - started,
        "mlx_peak_allocated_bytes": int(mx.get_peak_memory()),
        "architecture": platform.machine(),
        "device": str(mx.default_device()),
        "worker_version": importlib.metadata.version("mlx-qwen3-asr"),
        "mlx_version": importlib.metadata.version("mlx"),
        "alignment_executed": True,
        "asr_inference_executed": False,
    }
    if policy:
        record.update(transcription_language_policy=policy, language_granularity="chunk",
                      detected_language=asr.get("language_detected"))
    save_json(Path(output), record)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="单片转写、对齐和标点分段")
    parser.add_argument("--run", help="含 protocol.json 的运行目录")
    parser.add_argument("--stage", choices=("prepare", "asr", "align", "documents", "revalidate-tail"))
    parser.add_argument("--worker", choices=("asr", "align"), help=argparse.SUPPRESS)
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--wav", help=argparse.SUPPRESS)
    parser.add_argument("--output", help=argparse.SUPPRESS)
    parser.add_argument("--asr-json", help=argparse.SUPPRESS)
    parser.add_argument("--max-new-tokens", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--language-policy", choices=(AUTO_LANGUAGE_POLICY,), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        if not args.model or not args.wav or not args.output:
            parser.error("worker 需要 --model --wav --output")
        if args.worker == "asr" and args.max_new_tokens is None:
            parser.error("asr worker 需要 --max-new-tokens")
        if args.worker == "align" and not args.asr_json:
            parser.error("align worker 需要 --asr-json")
    elif not args.run or not args.stage:
        parser.error("需要 --run 和 --stage")
    return args


def main(argv=None):
    os.umask(0o077)
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.worker == "asr":
            worker_asr(args.model, args.wav, args.output, args.max_new_tokens, args.language_policy)
        elif args.worker == "align":
            worker_align(args.model, args.wav, args.asr_json, args.output, args.language_policy)
        elif args.stage == "prepare":
            stage_prepare(args.run)
        elif args.stage == "asr":
            stage_asr(args.run)
        elif args.stage == "align":
            stage_align(args.run)
        elif args.stage == "revalidate-tail":
            stage_revalidate_tail(args.run)
        else:
            stage_documents(args.run)
    except StageBlocked as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
