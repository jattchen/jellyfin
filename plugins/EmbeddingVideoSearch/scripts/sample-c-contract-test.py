#!/usr/bin/env python3
import hashlib
import importlib.util
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
