#!/usr/bin/env python3
from decimal import Decimal
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load("sample_c_contract_test", "sample-c-contract.py")
embed = load("sample_c_embed_for_tail", "full-film-embed.py")


def write_excerpt(run, audio_index):
    run.mkdir()
    protocol = {
        "source": {
            "sourcePath": str(run / "sample.wav"),
            "runtimeTicks": contract.SAMPLE_TICKS,
            "contentVersion": "sample-pcm-sha256:abc",
            "audioStreamIndex": audio_index,
        },
        "provenance": {
            "absoluteOffsetTicks": 100_000_000,
            "sampleEndTicks": 1_300_000_000,
            "originalSourcePath": "/media/film.mp4",
            "originalItemId": "11111111-1111-4111-8111-111111111111",
        },
    }
    if audio_index is contract.MISSING:
        del protocol["source"]["audioStreamIndex"]
    (run / "protocol.json").write_text(json.dumps(protocol), encoding="utf-8")


class ExcerptAudioIndex(unittest.TestCase):
    def test_zero_is_the_excerpt_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "asr-C1"
            write_excerpt(run, 0)
            provenance = contract.read_asr_provenance(run)
            self.assertEqual(provenance["absoluteOffsetTicks"], 100_000_000)

    def test_missing_or_null_index_is_rejected_and_one_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            write_excerpt(missing, contract.MISSING)
            with self.assertRaises(contract.ContractError) as caught:
                contract.read_asr_provenance(missing)
            self.assertIn("missing", str(caught.exception))
            null_index = Path(directory) / "null"
            write_excerpt(null_index, None)
            with self.assertRaises(contract.ContractError) as caught:
                contract.read_asr_provenance(null_index)
            self.assertIn("missing", str(caught.exception))
            other = Path(directory) / "other"
            write_excerpt(other, 1)
            with self.assertRaises(contract.ContractError) as caught:
                contract.read_asr_provenance(other)
            self.assertIn("index 0", str(caught.exception))


contract.MISSING = object()


def stream_probe(audio_seconds, video_seconds):
    return {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0.000000", "duration": video_seconds},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": audio_seconds},
    ]}


def gray_frame(path, seconds):
    return subprocess.check_output([
        "/usr/local/bin/ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{seconds:.6f}", "-i", str(path),
        "-frames:v", "1", "-vf", "scale=32:32,format=gray", "-f", "rawvideo", "-",
    ], timeout=20)


def mean_abs(left, right):
    return sum(abs(a - b) for a, b in zip(left, right)) / len(left)


class VideoTailHold(unittest.TestCase):
    def test_known_short_tail_needs_five_frames_and_the_old_check_fails(self):
        probe = stream_probe("245.783220", "245.633333")
        self.assertFalse(contract.clip_duration_accepted(11.833333))
        self.assertTrue(contract.clip_duration_accepted(11.850))
        plain = contract.classify_video_tail(probe, 1, 2_457_832_200)
        self.assertEqual(plain["status"], "within-bound")
        self.assertEqual(plain["gapSeconds"], "0.149887")
        self.assertNotIn("extensionSeconds", plain)
        decided = contract.classify_video_tail(probe, 1, 2_457_832_200, frame_rate="30/1")
        self.assertEqual(decided["status"], "within-bound")
        self.assertEqual(decided["extensionSeconds"], "0.166667")
        self.assertEqual(decided["frameRate"], "30/1")

    def test_plain_arguments_stay_unchanged_until_a_hold_is_required(self):
        probe = stream_probe("12.000000", "11.833333")
        plain, _facts = contract.clip_ffmpeg_arguments("/media/film.mp4", 0, 1, "/tmp/out.mp4", probe)
        self.assertEqual(plain, [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-accurate_seek",
            "-ss", "0", "-i", "/media/film.mp4",
            "-t", "12", "-map", "0:v:0", "-map", "0:1",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-threads", "2", "-c:a", "aac", "-ac", "1", "-ar", "16000", "-sn", "-dn", "/tmp/out.mp4",
        ])
        held, _facts = contract.clip_ffmpeg_arguments("/media/film.mp4", 0, 1, "/tmp/out.mp4", probe, "0.166667")
        self.assertNotIn("0:v:0", held)
        self.assertIn("[v]", held)
        self.assertIn("[0:v:0]tpad=stop_mode=clone:stop_duration=0.166667[v]", held)
        self.assertIn("0:1", held)
        self.assertNotIn("apad", held)

    def test_over_bound_unknown_and_middle_windows_do_not_pad(self):
        over = stream_probe("12.000000", "11.700000")
        with self.assertRaises(contract.ContractError) as caught:
            contract.hold_last_frame(over, 1, 120_000_000, 120_000_000, contract.VIDEO_TAIL_POLICY)
        self.assertIn("200 milliseconds", str(caught.exception))
        self.assertIsNone(contract.hold_last_frame(over, 1, 120_000_000, 120_000_000, None))
        self.assertIsNone(contract.hold_last_frame(over, 1, 120_000_000, 120_000_000, ""))
        within = stream_probe("20.000000", "19.850113")
        self.assertIsNone(contract.hold_last_frame(
            within, 1, 200_000_000, 120_000_000, contract.VIDEO_TAIL_POLICY))
        unknown = {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}]}
        with self.assertRaises(contract.ContractError) as caught:
            contract.hold_last_frame(unknown, 1, 120_000_000, 120_000_000, contract.VIDEO_TAIL_POLICY)
        self.assertIn("unknown", str(caught.exception))
        with self.assertRaises(contract.ContractError):
            contract.hold_last_frame(within, 1, 200_000_000, 200_000_000, "hold-everything")

    def test_cached_clip_without_the_new_fields_is_reused(self):
        probe = stream_probe("12.000000", "11.833333")
        source = {
            "itemId": "11111111-1111-4111-8111-111111111111",
            "contentVersion": "content-fixed", "runtimeTicks": 120_000_000,
            "audioStreamIndex": 1, "sourcePath": "/media/film.mp4",
        }
        facts = {"real_path": "/media/film.mp4", "size": 4, "mtime_ns": 1}
        identity = {
            "source": facts, "start_ticks": 0, "end_ticks": 120_000_000,
            "profile": contract.INPUT_VERSION, "threads": 2, "audio_stream_index": 1,
            "window_version": contract.VERSION, "video_map": "0:v:0", "audio_map": "0:1",
            "time_origin": "container", "video_start_added": False,
            "video_start_time_raw": "0.000000",
            "attached_picture_stream_indexes": [],
            "item_id": contract.canonical_item(source["itemId"]),
            "content_version": "content-fixed", "source_path": "/media/film.mp4", "full_film": True,
        }
        entry = {"candidate_id": "joint-cached", "start_ticks": 0, "end_ticks": 120_000_000, "full_film": True}
        calls = []
        original = contract.probe_frame_rate
        contract.probe_frame_rate = lambda _path: calls.append("probe") or "30/1"
        try:
            with tempfile.TemporaryDirectory() as directory:
                run = Path(directory)
                clip = run / "clips" / "joint-cached.mp4"
                clip.parent.mkdir(parents=True)
                clip.write_bytes(b"cached-clip")
                digest = hashlib.sha256(b"cached-clip").hexdigest()
                contract.save_json(clip.with_suffix(".clip.json"), {"identity": identity, "sha256": digest})
                path, state = contract.clip_window(
                    run, entry, source, contract.window_template(), probe,
                    embed.sha, embed.save, embed.read, lambda _source: facts,
                    video_tail_policy=contract.VIDEO_TAIL_POLICY,
                )
                self.assertEqual(path, clip)
                self.assertEqual(state["identity"], identity)
                self.assertNotIn("videoTail", state)
        finally:
            contract.probe_frame_rate = original
        self.assertEqual(calls, [])

    def test_short_video_fails_until_the_last_frame_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.mp4"
            subprocess.run([
                "/usr/local/bin/ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30",
                "-frames:v", "355", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "28", "-pix_fmt", "yuv420p", "-g", "15", str(root / "video.mp4"),
            ], check=True, timeout=60)
            subprocess.run([
                "/usr/local/bin/ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(root / "video.mp4"),
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=12",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-c:a", "aac", "-ac", "1", "-ar", "16000", "-t", "12", str(source_path),
            ], check=True, timeout=60)
            probed = json.loads(subprocess.check_output([
                "/usr/local/bin/ffprobe", "-v", "error",
                "-show_entries", "stream=index,codec_type,codec_name,start_time,duration,r_frame_rate",
                "-of", "json", str(source_path),
            ], timeout=20))
            video = next(stream for stream in probed["streams"] if stream["codec_type"] == "video")
            audio = next(stream for stream in probed["streams"] if stream["codec_type"] == "audio")
            self.assertFalse(contract.clip_duration_accepted(float(video["duration"])))
            self.assertTrue(contract.clip_duration_accepted(float(audio["duration"])))
            stat = source_path.stat()
            source = {
                "itemId": "11111111-1111-4111-8111-111111111111",
                "mediaSourceId": "mediasource",
                "libraryId": "22222222-2222-4222-8222-222222222222",
                "contentVersion": "content-fixed", "runtimeTicks": 120_000_000,
                "container": "mp4", "audioStreamIndex": 1,
                "sourceItemId": "11111111-1111-4111-8111-111111111111",
                "sourcePath": str(source_path), "sizeBytes": stat.st_size,
                "modifiedTicks": stat.st_mtime_ns // 100 + 621355968000000000,
            }
            entry = {
                "candidate_id": "joint-synthetic", "start_ticks": 0, "end_ticks": 120_000_000,
                "full_film": True,
            }
            failed = root / "failed"
            with self.assertRaises(contract.ContractError) as caught:
                contract.clip_window(
                    failed, entry, source, contract.window_template(), probed,
                    embed.sha, embed.save, embed.read, embed.source_facts,
                )
            self.assertIn("0.15", str(caught.exception))
            partial = failed / "clips" / "joint-synthetic.partial.mp4"
            self.assertTrue(partial.is_file())
            self.assertFalse((failed / "clips" / "joint-synthetic.mp4").exists())
            held_run = root / "held"
            _path, state = contract.clip_window(
                held_run, entry, source, contract.window_template(), probed,
                embed.sha, embed.save, embed.read, embed.source_facts,
                video_tail_policy=contract.VIDEO_TAIL_POLICY,
            )
            self.assertEqual(state["videoTail"]["hold"], "last-frame")
            self.assertEqual(state["videoTail"]["policy"], contract.VIDEO_TAIL_POLICY)
            self.assertLessEqual(float(state["videoTail"]["extensionSeconds"]), 0.200)
            streams = {stream["codec_type"]: stream for stream in state["probe"]["streams"]}
            self.assertTrue(contract.clip_duration_accepted(float(streams["video"]["duration"])))
            self.assertTrue(contract.clip_duration_accepted(float(streams["audio"]["duration"])))
            self.assertTrue(contract.audio_covers_clip(
                float(streams["audio"]["duration"]), float(streams["video"]["duration"])))
            clip = held_run / "clips" / "joint-synthetic.mp4"
            for offset in (0, 3, 6, 9):
                correct = mean_abs(gray_frame(clip, offset), gray_frame(source_path, offset))
                shifted = mean_abs(gray_frame(clip, offset), gray_frame(source_path, offset + 0.5))
                self.assertLess(correct, shifted)
                self.assertLess(correct, 25)
            pad = gray_frame(clip, 11.95)
            last = gray_frame(source_path, 11.80)
            opening = gray_frame(source_path, 0)
            self.assertLess(mean_abs(pad, last), mean_abs(pad, opening))

    def test_index_zero_picture_keeps_the_historical_map(self):
        facts = contract.check_source_probe(stream_probe("12.000000", "12.000000"), 1)
        self.assertEqual(facts["video_map"], "0:v:0")
        self.assertEqual(facts["video_stream_index"], 0)
        self.assertEqual(facts["audio_map"], "0:1")

    def test_audio_before_the_picture_selects_that_picture(self):
        probe = {"streams": [
            {"index": 0, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": "139.923447"},
            {"index": 1, "codec_type": "video", "codec_name": "h264", "start_time": "0.000000", "duration": "139.866667",
             "width": 160, "height": 120},
        ]}
        facts = contract.check_source_probe(probe, 0)
        self.assertEqual(facts["video_map"], "0:1")
        self.assertEqual(facts["video_stream_index"], 1)
        self.assertEqual(facts["audio_map"], "0:0")
        self.assertEqual(facts["attached_picture_stream_indexes"], [])
        decision = contract.classify_video_tail(probe, 0, 1_399_234_470)
        self.assertEqual(decision["status"], "within-bound")
        self.assertEqual(decision["gapSeconds"], "0.056780")
        plain, _facts = contract.clip_ffmpeg_arguments("/media/film.mp4", 0, 0, "/tmp/out.mp4", probe)
        self.assertEqual(plain[plain.index("-map") + 1], "0:1")
        self.assertIn("0:0", plain)
        self.assertNotIn("0:v:0", plain)
        held, _facts = contract.clip_ffmpeg_arguments("/media/film.mp4", 0, 0, "/tmp/out.mp4", probe, "0.066667")
        self.assertIn("[0:1]tpad=stop_mode=clone:stop_duration=0.066667[v]", held)
        self.assertNotIn("0:v:0", held)

    def test_cover_duplicate_and_missing_audio_are_refused(self):
        cover_first = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1},
             "duration": "0.040000"},
            {"index": 1, "codec_type": "video", "codec_name": "h264", "start_time": "0.000000", "duration": "12.000000"},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "duration": "12.000000"},
        ]}
        facts = contract.check_source_probe(cover_first, 2)
        self.assertEqual(facts["video_map"], "0:1")
        self.assertEqual(facts["audio_map"], "0:2")
        self.assertEqual(facts["attached_picture_stream_indexes"], [0])
        plain, _facts = contract.clip_ffmpeg_arguments("/media/film.mp4", 0, 2, "/tmp/out.mp4", cover_first)
        self.assertIn("0:1", plain)
        self.assertIn("0:2", plain)
        self.assertNotIn("0:v:0", plain)
        self.assertNotIn("0:0", plain)
        duplicate = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "duration": "12.000000"},
            {"index": 1, "codec_type": "video", "codec_name": "hevc", "duration": "12.000000"},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "duration": "12.000000"},
        ]}
        with self.assertRaises(contract.ContractError) as caught:
            contract.check_source_probe(duplicate, 2)
        self.assertIn("more than one", str(caught.exception))
        cover_only = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "duration": "12.000000"},
        ]}
        with self.assertRaises(contract.ContractError) as caught:
            contract.check_source_probe(cover_only, 1)
        self.assertIn("missing", str(caught.exception))
        with self.assertRaises(contract.ContractError) as caught:
            contract.check_source_probe(stream_probe("12.000000", "12.000000"), 3)
        self.assertIn("audio", str(caught.exception))

    def test_longer_picture_is_not_a_hold_and_short_audio_still_fails(self):
        probe = stream_probe("11.850000", "12.000000")
        decision = contract.classify_video_tail(probe, 1, 120_000_000)
        self.assertEqual(decision["status"], "none")
        self.assertNotIn("extensionSeconds", decision)
        source = {"audioStreamIndex": 1, "runtimeTicks": 120_000_000, "sourcePath": "/media/film.mp4"}
        calls = []
        original = contract.probe_frame_rate
        contract.probe_frame_rate = lambda *_args: calls.append("probe") or "30/1"
        try:
            self.assertIsNone(contract.decide_video_tail_hold(
                source, probe, 120_000_000, contract.VIDEO_TAIL_POLICY))
        finally:
            contract.probe_frame_rate = original
        self.assertEqual(calls, [])
        self.assertTrue(contract.clip_duration_accepted(11.985))
        self.assertTrue(contract.audio_covers_clip(11.985, 12.0))
        self.assertFalse(contract.clip_duration_accepted(11.833333))
        self.assertFalse(contract.audio_covers_clip(11.833333, 12.0))
        mismatched = stream_probe("11.700000", "11.800000")
        self.assertEqual(contract.classify_video_tail(mismatched, 1, 120_000_000)["status"], "unknown")
        with self.assertRaises(contract.ContractError) as caught:
            contract.hold_last_frame(mismatched, 1, 120_000_000, 120_000_000, contract.VIDEO_TAIL_POLICY)
        self.assertIn("unknown", str(caught.exception))

    def test_nonzero_start_uses_the_container_endpoint_without_shifting_seek(self):
        reached = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0.023222", "duration": "425.320000"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": "425.296689"},
        ]}
        runtime = 4_253_432_220
        decision = contract.classify_video_tail(reached, 1, runtime)
        self.assertEqual(decision["status"], "none")
        self.assertEqual(decision["videoEndSeconds"], "425.343222")
        self.assertEqual(decision["audioEndSeconds"], "425.296689")
        later = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0.040000", "duration": "887.600000"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": "887.582766"},
        ]}
        later_decision = contract.classify_video_tail(later, 1, 8_876_400_000)
        self.assertEqual(later_decision["status"], "none")
        self.assertEqual(later_decision["videoEndSeconds"], "887.640000")
        source = {"audioStreamIndex": 1, "runtimeTicks": runtime, "sourcePath": "/media/film.mp4"}
        calls = []
        original = contract.probe_frame_rate
        contract.probe_frame_rate = lambda *_args: calls.append("probe") or "30/1"
        try:
            self.assertIsNone(contract.decide_video_tail_hold(
                source, reached, runtime, contract.VIDEO_TAIL_POLICY))
        finally:
            contract.probe_frame_rate = original
        self.assertEqual(calls, [])
        start = runtime - contract.LENGTH_TICKS
        arguments, _facts = contract.clip_ffmpeg_arguments("/media/film.mp4", start, 1, "/tmp/out.mp4", reached)
        self.assertEqual(arguments[arguments.index("-ss") + 1], contract.ticks_to_seconds(start))
        self.assertNotIn("0.023222", arguments)
        self.assertIn("0:v:0", arguments)
        short = {"streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "start_time": "0.023222", "duration": "11.826778"},
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": "12.000000"},
        ]}
        short_decision = contract.classify_video_tail(short, 1, 120_000_000, frame_rate="30/1")
        self.assertEqual(short_decision["status"], "within-bound")
        self.assertEqual(short_decision["gapSeconds"], "0.150000")
        self.assertEqual(short_decision["videoEndSeconds"], "11.850000")
        self.assertEqual(short_decision["extensionSeconds"], "0.166667")
        held, _facts = contract.clip_ffmpeg_arguments("/media/film.mp4", 0, 1, "/tmp/out.mp4", short, "0.166667")
        self.assertEqual(held[held.index("-ss") + 1], "0")
        self.assertNotIn("0.023222", held)
        self.assertIn("[0:v:0]tpad=stop_mode=clone:stop_duration=0.166667[v]", held)

    def test_missing_or_illegal_timestamps_stay_unknown(self):
        def probe(start, duration):
            video = {"index": 0, "codec_type": "video", "codec_name": "h264", "duration": duration}
            if start is not None:
                video["start_time"] = start
            return {"streams": [
                video,
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "start_time": "0.000000", "duration": "12.000000"},
            ]}
        for start, duration in (
            (None, "11.900000"),
            ("", "11.900000"),
            ("N/A", "11.900000"),
            ("nan", "11.900000"),
            ("inf", "11.900000"),
            ("-0.010000", "11.900000"),
            ("0.000000", None),
            ("0.000000", "-1.000000"),
            ("0.000000", "nan"),
        ):
            decision = contract.classify_video_tail(probe(start, duration), 1, 120_000_000)
            self.assertEqual(decision["status"], "unknown", msg=repr((start, duration)))
        with self.assertRaises(contract.ContractError) as caught:
            contract.hold_last_frame(probe(None, "11.900000"), 1, 120_000_000, 120_000_000, contract.VIDEO_TAIL_POLICY)
        self.assertIn("unknown", str(caught.exception))

    def test_audio_before_picture_file_keeps_the_real_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.mp4"
            subprocess.run([
                "/usr/local/bin/ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=12",
                "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=12",
                "-map", "0:a:0", "-map", "1:v:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "1", "-ar", "16000", "-t", "12", str(source_path),
            ], check=True, timeout=60)
            probed = json.loads(subprocess.check_output([
                "/usr/local/bin/ffprobe", "-v", "error",
                "-show_entries", "stream=index,codec_type,codec_name,start_time,duration",
                "-of", "json", str(source_path),
            ], timeout=20))
            self.assertEqual(probed["streams"][0]["codec_type"], "audio")
            self.assertEqual(probed["streams"][1]["codec_type"], "video")
            stat = source_path.stat()
            source = {
                "itemId": "11111111-1111-4111-8111-111111111111",
                "mediaSourceId": "mediasource",
                "libraryId": "22222222-2222-4222-8222-222222222222",
                "contentVersion": "content-fixed", "runtimeTicks": 120_000_000,
                "container": "mp4", "audioStreamIndex": 0,
                "sourceItemId": "11111111-1111-4111-8111-111111111111",
                "sourcePath": str(source_path), "sizeBytes": stat.st_size,
                "modifiedTicks": stat.st_mtime_ns // 100 + 621355968000000000,
            }
            entry = {
                "candidate_id": "joint-audio-first", "start_ticks": 0, "end_ticks": 120_000_000,
                "full_film": True,
            }
            _path, state = contract.clip_window(
                root / "cut", entry, source, contract.window_template(), probed,
                embed.sha, embed.save, embed.read, embed.source_facts,
            )
            self.assertEqual(state["identity"]["video_map"], "0:1")
            self.assertEqual(state["identity"]["audio_map"], "0:0")
            self.assertNotIn("videoTail", state)
            clip = root / "cut" / "clips" / "joint-audio-first.mp4"
            for offset in (0, 3, 6, 9):
                correct = mean_abs(gray_frame(clip, offset), gray_frame(source_path, offset))
                shifted = mean_abs(gray_frame(clip, offset), gray_frame(source_path, offset + 0.5))
                self.assertLess(correct, shifted)


def pcm_tone(silence_at=None):
    samples = []
    for index in range(192000):
        second = index / 16000
        quiet = silence_at is not None and silence_at[0] <= second < silence_at[1]
        samples.append(0.0 if quiet else 0.2 * math.sin(2 * math.pi * 440 * second))
    return struct.pack("<" + str(len(samples)) + "f", *samples)


def frame_at(pts, samples=160):
    return {"pts_time": pts, "nb_samples": samples}


class AudioGapFill(unittest.TestCase):
    def test_one_millisecond_is_continuous_and_illegal_timestamps_stop(self):
        continuous = [frame_at("0.000000"), frame_at("0.011000")]
        self.assertEqual(contract.classify_audio_gaps(continuous, 0, 12, 16000), [])
        negative = [frame_at("0.000000"), frame_at("0.009000")]
        self.assertEqual(contract.classify_audio_gaps(negative, 0, 12, 16000), [])
        gaps = contract.classify_audio_gaps([frame_at("0.000000"), frame_at("0.011001")], 0, 12, 16000)
        self.assertEqual(len(gaps), 1)
        self.assertGreater(gaps[0][1] - gaps[0][0], Decimal("0.001"))
        clipped = contract.classify_audio_gaps(
            [frame_at("11.940000"), frame_at("12.100000")], 0, 12, 16000)
        self.assertEqual(clipped, [(Decimal("11.95"), Decimal("12"))])
        self.assertEqual(contract.classify_audio_gaps([frame_at("0.050000")], 0, 12, 16000), [])
        self.assertEqual(
            contract.classify_audio_gaps([frame_at("12.000000"), frame_at("12.200000")], 0, 12, 16000), [])
        with self.assertRaises(contract.ContractError) as backwards:
            contract.classify_audio_gaps([frame_at("1.000000"), frame_at("0.900000")], 0, 12, 16000)
        self.assertIn("backwards", str(backwards.exception))
        with self.assertRaises(contract.ContractError) as overlap:
            contract.classify_audio_gaps([frame_at("0.000000"), frame_at("0.008999")], 0, 12, 16000)
        self.assertIn("overlap", str(overlap.exception))
        for broken in (
            [{"pts_time": "0.000000"}],
            [{"pts_time": "nan", "nb_samples": 160}],
            [{"pts_time": "inf", "nb_samples": 160}],
            [{"nb_samples": 160}],
            [],
        ):
            with self.assertRaises(contract.ContractError) as missing:
                contract.classify_audio_gaps(broken, 0, 12, 16000)
            self.assertIn("cannot be located", str(missing.exception))
        decision = contract.finalize_audio_gaps([(Decimal("0"), Decimal("0.200"))], 0)
        self.assertEqual(decision["status"], "gaps")
        self.assertEqual(decision["total_seconds"], "0.200000")
        with self.assertRaises(contract.ContractError) as limited:
            contract.finalize_audio_gaps([(Decimal("0"), Decimal("0.2000001"))], 0)
        self.assertIn("200 milliseconds", str(limited.exception))
        shifted = contract.clip_timeline_frames(
            [frame_at("470.144263", 1024), frame_at("470.250181", 1024)], "0.060000")
        moved = contract.classify_audio_gaps(shifted, 470, 482, 44100)
        self.assertEqual(len(moved), 1)
        self.assertAlmostEqual(float(moved[0][0] - Decimal(470)), 0.107483, places=5)
        parked = contract.clip_timeline_frames(
            [frame_at("470.144263", 1024), frame_at("470.250181", 1024)], "0")
        stayed = contract.classify_audio_gaps(parked, 470, 482, 44100)
        self.assertGreater(stayed[0][0], moved[0][0])
        for missing_start in (None, "nan", "inf"):
            with self.assertRaises(contract.ContractError) as lost:
                contract.clip_timeline_frames([frame_at("1.000000")], missing_start)
            self.assertIn("cannot be located", str(lost.exception))

    def test_decode_gate_and_silence_use_the_measured_hole(self):
        short = b"\x00\x00\x00\x00" * 189498
        with self.assertRaises(contract.ContractError) as caught:
            contract.require_decoded_coverage(short, 12.0)
        self.assertIn("0.15", str(caught.exception))
        contract.require_decoded_coverage(b"\x00\x00\x00\x00" * 192000, 11.966667)
        gaps = [{"start_seconds": "0.256000", "end_seconds": "0.336000", "seconds": "0.080000"}]
        contract.require_gap_silence(pcm_tone((0.256, 0.336)), gaps)
        with self.assertRaises(contract.ContractError) as misplaced:
            contract.require_gap_silence(pcm_tone((11.80, 12.0)), gaps)
        self.assertIn("original position", str(misplaced.exception))

    def test_filter_is_explicit_and_plain_arguments_stay_the_same(self):
        probe = stream_probe("12.000000", "12.000000")
        plain, _facts = contract.clip_ffmpeg_arguments("/media/film.mp4", 0, 1, "/tmp/out.mp4", probe)
        same, _facts = contract.clip_ffmpeg_arguments(
            "/media/film.mp4", 0, 1, "/tmp/out.mp4", probe, fill_audio_gap=False)
        self.assertEqual(plain, same)
        self.assertNotIn(contract.AUDIO_GAP_FILTER, plain)
        filled, _facts = contract.clip_ffmpeg_arguments(
            "/media/film.mp4", 4_700_000_000, 1, "/tmp/out.mp4", probe, fill_audio_gap=True)
        self.assertEqual(filled[filled.index("-ss") + 1], "470")
        self.assertEqual(filled[filled.index("-af") + 1], contract.AUDIO_GAP_FILTER)
        self.assertEqual(filled.count("-map"), 2)
        self.assertIn("0:v:0", filled)
        self.assertIn("0:1", filled)
        both, _facts = contract.clip_ffmpeg_arguments(
            "/media/film.mp4", 0, 1, "/tmp/out.mp4", probe, "0.166667", fill_audio_gap=True)
        self.assertNotIn("-af", both)
        self.assertNotIn("0:v:0", both)
        self.assertNotIn("0:1", both)
        self.assertIn("[v]", both)
        self.assertIn("[a]", both)
        graph = next(item for item in both if contract.AUDIO_GAP_FILTER in item)
        self.assertIn("tpad=stop_mode=clone:stop_duration=0.166667", graph)
        self.assertEqual(graph.count(contract.AUDIO_GAP_FILTER), 1)

    def test_input_hash_changes_only_when_a_gap_is_recorded(self):
        protocol = {"signature": {"id": "model"}, "window": contract.window_template()}
        entry = {"candidate_id": "joint-1", "start_ticks": 0, "end_ticks": contract.LENGTH_TICKS}
        plain = embed.embedding_input(protocol, "joint", entry, "abc")[0]
        self.assertEqual(plain, embed.embedding_input(protocol, "joint", entry, "abc", None)[0])
        gap = contract.audio_gap_record(contract.identity_with_audio_gap({}, {
            "status": "gaps",
            "total_seconds": "0.080000",
            "count": 1,
            "gaps": [{"start_seconds": "0.256000", "end_seconds": "0.336000", "seconds": "0.080000"}],
        }))
        self.assertNotEqual(plain, embed.embedding_input(protocol, "joint", entry, "abc", gap)[0])

    def test_cached_success_without_gap_fields_is_not_measured_again(self):
        probe = stream_probe("12.000000", "12.000000")
        source = {
            "itemId": "11111111-1111-4111-8111-111111111111",
            "contentVersion": "content-fixed", "runtimeTicks": 120_000_000,
            "audioStreamIndex": 1, "sourcePath": "/media/film.mp4",
        }
        facts = {"real_path": "/media/film.mp4", "size": 4, "mtime_ns": 1}
        identity = {
            "source": facts, "start_ticks": 0, "end_ticks": 120_000_000,
            "profile": contract.INPUT_VERSION, "threads": 2, "audio_stream_index": 1,
            "window_version": contract.VERSION, "video_map": "0:v:0", "audio_map": "0:1",
            "time_origin": "container", "video_start_added": False,
            "video_start_time_raw": "0.000000", "attached_picture_stream_indexes": [],
            "item_id": contract.canonical_item(source["itemId"]),
            "content_version": "content-fixed", "source_path": "/media/film.mp4", "full_film": True,
        }
        entry = {"candidate_id": "joint-cached", "start_ticks": 0, "end_ticks": 120_000_000, "full_film": True}
        calls = []
        original = contract.measure_audio_gaps
        contract.measure_audio_gaps = lambda *_args, **_kwargs: calls.append("measure")
        try:
            with tempfile.TemporaryDirectory() as directory:
                run = Path(directory)
                clip = run / "clips" / "joint-cached.mp4"
                clip.parent.mkdir(parents=True)
                clip.write_bytes(b"cached-clip")
                digest = hashlib.sha256(b"cached-clip").hexdigest()
                contract.save_json(clip.with_suffix(".clip.json"), {"identity": identity, "sha256": digest})
                _path, state = contract.clip_window(
                    run, entry, source, contract.window_template(), probe,
                    embed.sha, embed.save, embed.read, lambda _source: facts,
                    audio_gap_policy=contract.AUDIO_GAP_POLICY,
                )
                self.assertEqual(state["identity"], identity)
                self.assertNotIn("audio_gap_policy", state["identity"])
        finally:
            contract.measure_audio_gaps = original
        self.assertEqual(calls, [])

    def _source(self, path, runtime=120_000_000):
        probed = json.loads(subprocess.check_output([
            "/usr/local/bin/ffprobe", "-v", "error",
            "-show_entries", "stream=index,codec_type,codec_name,start_time,duration",
            "-of", "json", str(path),
        ], timeout=20))
        stat = path.stat()
        source = {
            "itemId": "11111111-1111-4111-8111-111111111111",
            "mediaSourceId": "mediasource",
            "libraryId": "22222222-2222-4222-8222-222222222222",
            "contentVersion": "content-fixed", "runtimeTicks": runtime,
            "container": "mp4", "audioStreamIndex": 1,
            "sourceItemId": "11111111-1111-4111-8111-111111111111",
            "sourcePath": str(path), "sizeBytes": stat.st_size,
            "modifiedTicks": stat.st_mtime_ns // 100 + 621355968000000000,
        }
        return probed, source

    def _gapped(self, root, hole):
        first = 0.20
        rest = 12.0 - first - hole
        a_path = root / "a.wav"
        b_path = root / "b.wav"
        subprocess.run([
            "/usr/local/bin/ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=" + format(first, ".2f"),
            str(a_path),
        ], check=True, timeout=30)
        subprocess.run([
            "/usr/local/bin/ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=16000:duration=" + format(rest, ".2f"),
            str(b_path),
        ], check=True, timeout=30)
        script = root / "in.ffconcat"
        script.write_text(
            "ffconcat version 1.0\nfile '" + str(a_path) + "'\nduration "
            + format(first + hole, ".6f") + "\nfile '" + str(b_path) + "'\n",
            encoding="utf-8",
        )
        source = root / "source.mp4"
        subprocess.run([
            "/usr/local/bin/ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(script),
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=12",
            "-map", "1:v:0", "-map", "0:a:0",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ac", "1", "-ar", "16000", "-t", "12", str(source),
        ], check=True, timeout=60)
        return source

    def test_continuous_audio_keeps_the_old_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = root / "source.mp4"
            subprocess.run([
                "/usr/local/bin/ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=12",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=12",
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ac", "1", "-ar", "16000", "-t", "12", str(source_path),
            ], check=True, timeout=60)
            probed, source = self._source(source_path)
            entry = {
                "candidate_id": "joint-continuous", "start_ticks": 0, "end_ticks": 120_000_000,
                "full_film": True,
            }
            recorded = []
            original = contract.clip_ffmpeg_arguments

            def spy(*args, **kwargs):
                result = original(*args, **kwargs)
                recorded.append(result[0])
                return result

            contract.clip_ffmpeg_arguments = spy
            try:
                _path, state = contract.clip_window(
                    root / "cut", entry, source, contract.window_template(), probed,
                    embed.sha, embed.save, embed.read, embed.source_facts,
                    audio_gap_policy=contract.AUDIO_GAP_POLICY,
                )
            finally:
                contract.clip_ffmpeg_arguments = original
            self.assertEqual(len(recorded), 1)
            self.assertNotIn(contract.AUDIO_GAP_FILTER, recorded[0])
            self.assertNotIn("-af", recorded[0])
            self.assertNotIn("audio_gap_policy", state["identity"])
            self.assertNotIn("audioGap", state)
            pcm = contract.read_decoded_pcm(root / "cut" / "clips" / "joint-continuous.mp4")
            self.assertEqual(len(pcm) // 4, 192000)

    def test_real_gap_is_filled_at_the_measured_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = self._gapped(root, 0.17)
            probed, source = self._source(source_path)
            entry = {
                "candidate_id": "joint-gap", "start_ticks": 0, "end_ticks": 120_000_000,
                "full_film": True,
            }
            refused = root / "plain"
            with self.assertRaises(contract.ContractError) as caught:
                contract.clip_window(
                    refused, entry, source, contract.window_template(), probed,
                    embed.sha, embed.save, embed.read, embed.source_facts,
                )
            self.assertIn("decoded audio does not cover", str(caught.exception))
            self.assertFalse((refused / "clips" / "joint-gap.mp4").exists())
            recorded = []
            original = contract.clip_ffmpeg_arguments

            def spy(*args, **kwargs):
                result = original(*args, **kwargs)
                recorded.append(result[0])
                return result

            contract.clip_ffmpeg_arguments = spy
            try:
                _path, state = contract.clip_window(
                    root / "filled", entry, source, contract.window_template(), probed,
                    embed.sha, embed.save, embed.read, embed.source_facts,
                    audio_gap_policy=contract.AUDIO_GAP_POLICY,
                )
            finally:
                contract.clip_ffmpeg_arguments = original
            self.assertEqual(recorded[0][recorded[0].index("-af") + 1], contract.AUDIO_GAP_FILTER)
            self.assertEqual(recorded[0][recorded[0].index("-ss") + 1], "0")
            identity = state["identity"]
            self.assertEqual(identity["audio_gap_policy"], contract.AUDIO_GAP_POLICY)
            self.assertGreaterEqual(identity["audio_gap_count"], 1)
            self.assertLessEqual(Decimal(identity["audio_gap_total_seconds"]), Decimal("0.200"))
            self.assertLess(Decimal(identity["audio_gaps"][0]["start_seconds"]), Decimal("2"))
            clip = root / "filled" / "clips" / "joint-gap.mp4"
            pcm = contract.read_decoded_pcm(clip)
            self.assertEqual(len(pcm) // 4, 192000)
            contract.require_gap_silence(pcm, identity["audio_gaps"])
            self.assertGreater(contract._pcm_mean_abs(pcm, Decimal("1.0"), Decimal("1.1")), 0.05)
            for offset in (0, 3, 6, 9):
                correct = mean_abs(gray_frame(clip, offset), gray_frame(source_path, offset))
                shifted = mean_abs(gray_frame(clip, offset), gray_frame(source_path, offset + 0.5))
                self.assertLess(correct, shifted)

    def test_gap_over_200_milliseconds_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_path = self._gapped(root, 0.30)
            probed, source = self._source(source_path)
            entry = {
                "candidate_id": "joint-large", "start_ticks": 0, "end_ticks": 120_000_000,
                "full_film": True,
            }
            with self.assertRaises(contract.ContractError) as caught:
                contract.clip_window(
                    root / "cut", entry, source, contract.window_template(), probed,
                    embed.sha, embed.save, embed.read, embed.source_facts,
                    audio_gap_policy=contract.AUDIO_GAP_POLICY,
                )
            self.assertIn("200 milliseconds", str(caught.exception))
            self.assertFalse((root / "cut" / "clips" / "joint-large.mp4").exists())
            self.assertFalse((root / "cut" / "clips" / "joint-large.partial.mp4").exists())

    def test_unknown_policy_refuses_before_a_clip(self):
        probe = stream_probe("12.000000", "12.000000")
        source = {
            "itemId": "11111111-1111-4111-8111-111111111111",
            "contentVersion": "content-fixed", "runtimeTicks": 120_000_000,
            "audioStreamIndex": 1, "sourcePath": "/media/film.mp4",
        }
        entry = {"candidate_id": "joint-bad", "start_ticks": 0, "end_ticks": 120_000_000, "full_film": True}
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "cut"
            with self.assertRaises(contract.ContractError) as caught:
                contract.clip_window(
                    run, entry, source, contract.window_template(), probe,
                    embed.sha, embed.save, embed.read, lambda _source: {},
                    audio_gap_policy="fill-everything",
                )
            self.assertIn("unknown audio gap policy", str(caught.exception))
            self.assertFalse((run / "clips").exists())


if __name__ == "__main__":
    unittest.main()
