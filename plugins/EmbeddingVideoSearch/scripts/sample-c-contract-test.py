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


if __name__ == "__main__":
    unittest.main()
