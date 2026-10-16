#!/usr/bin/env python3
"""Synthetic checks for the single-film transcription driver. No model and no media."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np


DRIVER_PATH = Path(__file__).with_name("full-film-transcribe.py")
WORKER_PYTHON = Path(
    "/Volumes/Turbo/Library/transcription-worker/releases/qwen3-asr-mlx-0.4.4/.venv/bin/python"
)
SPEC = importlib.util.spec_from_file_location("full_film_transcribe", DRIVER_PATH)
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def write_silence(path, count):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(driver.SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * count)


def protocol(runtime=120_881_00000):
    return {
        "version": "full-film-pilot-v1",
        "source": {
            "sourceItemId": "item-1",
            "sourcePath": "unused",
            "audioStreamIndex": 1,
            "runtimeTicks": runtime,
            "sizeBytes": 3,
            "contentVersion": "content-v",
        },
        "worker_python": str(WORKER_PYTHON),
        "asr_model_path": "/tmp/asr-model-not-loaded",
        "aligner_model_path": "/tmp/aligner-model-not-loaded",
        "asr_max_new_tokens": 1024,
        "asr_max_chunk_seconds": 30,
    }


def save_protocol(run, value):
    (run / "protocol.json").write_text(json.dumps(value), encoding="utf-8")


def add_chunk(run, records, chunk_id, count):
    start = 0 if not records else int(records[-1]["end_sample"])
    end = start + count
    path = run / "audio" / "chunks" / f"{chunk_id}.wav"
    write_silence(path, count)
    records.append(
        {
            "chunk_id": chunk_id,
            "wav_path": str(path),
            "start_sample": start,
            "end_sample": end,
            "start_ticks": int(start * driver.TICKS_PER_SAMPLE),
            "end_ticks": int(end * driver.TICKS_PER_SAMPLE),
            "offset_seconds": start / driver.SAMPLE_RATE,
            "duration_seconds": count / driver.SAMPLE_RATE,
            "wav_sha256": driver.file_sha256(path),
        }
    )


def write_layout(run, lengths, runtime=120_881_00000):
    body = protocol(runtime)
    body["source"]["sourcePath"] = str(run / "source.bin")
    save_protocol(run, body)
    records = []
    for index, count in enumerate(lengths):
        add_chunk(run, records, f"c{index:04d}", count)
    driver.save_json(run / "chunks.json", records)
    driver.save_json(
        run / "prepare.json",
        {"status": "succeeded", "sample_count": records[-1]["end_sample"], "chunk_count": len(records)},
    )
    return records


def asr_record(text, truncated=False, finish="eos"):
    return {
        "raw_result": {"text": text, "truncated": truncated, "finish_reason": finish, "segments": None},
        "language_forced": "Chinese",
        "context": "",
        "max_new_tokens": 1024,
        "dtype": "bfloat16",
        "alignment_executed": False,
        "mlx_peak_allocated_bytes": 42,
    }


def write_asr(run, chunk, text, truncated=False):
    driver.save_json(driver.output_path(run, "asr", chunk["chunk_id"]), asr_record(text, truncated))


def mark_succeeded(run, stage, chunks):
    driver.save_json(
        driver.task_path(run, stage),
        [{"chunk_id": chunk["chunk_id"], "status": "succeeded"} for chunk in chunks],
    )


def align_record(chunk, text, units):
    count = int(chunk["end_sample"]) - int(chunk["start_sample"])
    slots = []
    for unit in units:
        slots.extend([unit["start_time"] * 1000, unit["end_time"] * 1000])
    return {
        "input_text": text,
        "language": "Chinese",
        "dtype": "bfloat16",
        "alignment_executed": True,
        "asr_inference_executed": False,
        "audio_samples": count,
        "audio_rate": 16000,
        "mlx_peak_allocated_bytes": 24,
        "units": units,
        "parser_capture": {"words": [unit["text"] for unit in units], "raw_slot_predictions_ms": slots},
    }


class DriverTests(unittest.TestCase):
    def test_ffmpeg_targets_protocol_audio_stream(self):
        command = driver.ffmpeg_arguments("/media/film.mp4", 1, "/tmp/full.wav", 12_088_100_000)
        self.assertEqual(command[0], "ffmpeg")
        self.assertEqual(command[command.index("-map") + 1], "0:1")
        self.assertGreater(command.index("-t"), command.index("-i"))
        self.assertEqual(command[command.index("-t") + 1], "1208.8100000")
        self.assertIn("pcm_s16le", command)
        self.assertEqual(command[command.index("-ar") + 1], "16000")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertIn("-f", command)
        self.assertEqual(command[-1], "/tmp/full.wav")

    def test_film_probe_records_duration_delta_and_pcm_limit(self):
        facts = driver.parse_ffprobe_document(
            {
                "streams": [
                    {"index": 0, "codec_type": "video", "start_time": "0.000000", "duration": "1208.800000"},
                    {"index": 1, "codec_type": "audio", "start_time": "0.000000", "duration": "1208.810000"},
                ]
            },
            1,
        )
        self.assertEqual(facts["video_duration_raw"], "1208.800000")
        self.assertEqual(facts["audio_duration_raw"], "1208.810000")
        self.assertEqual(facts["video_start_time_raw"], "0.000000")
        self.assertEqual(facts["audio_start_time_raw"], "0.000000")
        self.assertEqual(facts["video_audio_duration_delta_seconds"], "0.010000")
        self.assertEqual(driver.format_runtime_seconds(12_088_100_000), "1208.8100000")
        self.assertEqual(driver.expected_pcm_samples(12_088_100_000), 19_340_960)
        self.assertEqual(driver.verify_pcm_length(19_340_960, 12_088_100_000)["pcm_minus_expected_samples"], 0)
        with self.assertRaises(driver.StageBlocked) as caught:
            driver.verify_pcm_length(19_340_961, 12_088_100_000)
        self.assertIn("不能映射出片外", str(caught.exception))

    def test_worker_command_uses_cap_1024_and_no_draft(self):
        body = protocol()
        chunk = {"chunk_id": "c0000", "wav_path": "/tmp/c0000.wav"}
        command = driver.worker_command(body, "asr", chunk, Path("/tmp/out.json"))
        self.assertEqual(command[:4], ["/usr/bin/time", "-l", body["worker_python"], "-B"])
        self.assertIn("--worker", command)
        self.assertEqual(command[command.index("--worker") + 1], "asr")
        self.assertEqual(command[command.index("--max-new-tokens") + 1], "1024")
        self.assertNotIn("draft", " ".join(command))
        align = driver.worker_command(body, "align", chunk, Path("/tmp/align.json"), Path("/tmp/asr.json"))
        self.assertEqual(align[align.index("--worker") + 1], "align")
        self.assertIn("--asr-json", align)
        self.assertNotIn("--max-new-tokens", align)

    def test_rss_parser_and_playback_window(self):
        stderr = "        1.20 real\n           456789  maximum resident set size\n"
        self.assertEqual(driver.parse_peak_rss(stderr), 456789)
        self.assertIsNone(driver.parse_peak_rss("no resident line"))
        self.assertEqual(driver.playback_bounds(1.0, 1.4, 12), (0.0, 5.0))
        self.assertEqual(driver.absolute_ticks(16000, 0), 10_000_000)
        self.assertEqual(driver.absolute_ticks(0, 5), 50_000_000)

    def test_native_low_energy_chunks_cover_without_overlap(self):
        count = 45 * driver.SAMPLE_RATE
        timeline = np.arange(count)
        samples = (np.sin(2 * np.pi * 3 * timeline / driver.SAMPLE_RATE) * 8000).astype(np.int16)
        splitter = driver.load_splitter(WORKER_PYTHON)
        with tempfile.TemporaryDirectory() as temporary:
            records = driver.build_chunk_records(samples, splitter, Path(temporary))
        self.assertGreater(len(records), 1)
        cursor = 0
        for record in records:
            self.assertLessEqual(record["duration_seconds"], 30)
            self.assertEqual(record["start_sample"], cursor)
            self.assertGreater(record["end_sample"], record["start_sample"])
            self.assertEqual(record["start_ticks"], record["start_sample"] * driver.TICKS_PER_SAMPLE)
            self.assertEqual(record["end_ticks"], record["end_sample"] * driver.TICKS_PER_SAMPLE)
            self.assertIsInstance(record["start_ticks"], int)
            cursor = record["end_sample"]
            self.assertLessEqual(abs(record["offset_seconds"] * driver.SAMPLE_RATE - record["start_sample"]), 1)
        self.assertEqual(cursor, count)
        with tempfile.TemporaryDirectory() as temporary:
            records = driver.build_chunk_records(samples, splitter, Path(temporary))
            restored = np.concatenate([driver.read_pcm16_wav(item["wav_path"]) for item in records])
        self.assertTrue(np.array_equal(restored, samples))

    def test_gap_in_splitter_is_rejected(self):
        def drop(audio, sr=16000, max_chunk_sec=30.0):
            return [(audio[:1000], 0.0)]

        samples = np.zeros(3000, dtype=np.int16)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(driver.StageBlocked):
                driver.build_chunk_records(samples, drop, Path(temporary))

    def test_prepare_reuses_hash_and_does_not_reextract(self):
        calls = []

        runtime = 2 * driver.SAMPLE_RATE * driver.TICKS_PER_SAMPLE

        def fake_extract(source, index, dest, runtime_ticks):
            calls.append((Path(source).name, index, runtime_ticks))
            write_silence(Path(dest), 2 * driver.SAMPLE_RATE)

        def fake_probe(source, audio_index):
            return driver.parse_ffprobe_document(
                {
                    "streams": [
                        {"index": 0, "codec_type": "video", "start_time": "0.000000", "duration": "2.000000"},
                        {"index": audio_index, "codec_type": "audio", "start_time": "0.000000", "duration": "2.000000"},
                    ]
                },
                audio_index,
            )

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            source = run / "source.bin"
            source.write_bytes(b"abc")
            body = protocol(runtime)
            body["source"]["sourcePath"] = str(source)
            body["source"]["sizeBytes"] = 3
            save_protocol(run, body)
            driver.stage_prepare(run, extract=fake_extract, probe=fake_probe)
            driver.stage_prepare(run, extract=fake_extract, probe=fake_probe)
            self.assertEqual(calls, [("source.bin", 1, runtime)])
            prepared = driver.load_json(run / "prepare.json")
            self.assertEqual(prepared["ffprobe"]["audio_start_time_raw"], "0.000000")
            self.assertEqual(prepared["pcm_minus_expected_samples"], 0)
            self.assertEqual(prepared["runtime_seconds"], driver.format_runtime_seconds(runtime))
            records = driver.load_json(run / "chunks.json")
            self.assertEqual(sum(item["end_sample"] - item["start_sample"] for item in records), 2 * driver.SAMPLE_RATE)
            self.assertEqual(records[0]["start_sample"], 0)
            self.assertEqual(records[-1]["end_sample"], 2 * driver.SAMPLE_RATE)
            source.write_bytes(b"abd")
            with self.assertRaises(driver.StageBlocked) as caught:
                driver.stage_prepare(run, extract=fake_extract)
            self.assertIn("没有重新提取", str(caught.exception))
            self.assertEqual(len(calls), 1)

    def test_failed_prepare_is_not_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            source = run / "source.bin"
            source.write_bytes(b"abc")
            body = protocol()
            body["source"]["sourcePath"] = str(source)
            body["source"]["sizeBytes"] = 3
            save_protocol(run, body)
            driver.save_json(run / "prepare.json", {"status": "failed", "identity": {}})
            with self.assertRaises(driver.StageBlocked) as caught:
                driver.stage_prepare(run, extract=lambda *_args: (_ for _ in ()).throw(AssertionError("extracted")))
            self.assertIn("未自动重试", str(caught.exception))

    def test_truncation_stops_and_is_not_retried(self):
        calls = []

        def launch(kind, chunk, output, protocol_body):
            calls.append(chunk["chunk_id"])
            driver.save_json(output, asr_record("半句", truncated=True, finish="length"))
            return {"returncode": 3, "stderr": "           111  maximum resident set size\n", "elapsed_seconds": 0.2}

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            write_layout(run, [driver.SAMPLE_RATE, driver.SAMPLE_RATE])
            with self.assertRaises(driver.StageBlocked) as caught:
                driver.stage_asr(run, launch=launch)
            self.assertIn("截断", str(caught.exception))
            with self.assertRaises(driver.StageBlocked) as again:
                driver.stage_asr(run, launch=launch)
            self.assertIn("未自动重试", str(again.exception))
            self.assertEqual(calls, ["c0000"])
            metrics = driver.load_json(run / "asr-metrics.json")
            self.assertEqual(metrics[0]["peak_rss_bytes"], 111)
            self.assertEqual(metrics[0]["mlx_peak_allocated_bytes"], 42)
            self.assertEqual(driver.load_json(run / "asr-tasks.json")[0]["status"], "failed")

    def test_unknown_stop_is_not_retried(self):
        calls = []

        def launch(kind, chunk, output, protocol_body):
            calls.append(chunk["chunk_id"])
            return {"returncode": 1, "stderr": "", "elapsed_seconds": 0.1}

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            write_layout(run, [driver.SAMPLE_RATE])
            with self.assertRaises(driver.StageBlocked):
                driver.stage_asr(run, launch=launch)
            with self.assertRaises(driver.StageBlocked):
                driver.stage_asr(run, launch=launch)
            self.assertEqual(calls, ["c0000"])
            self.assertEqual(driver.load_json(run / "asr-tasks.json")[0]["status"], "unknown")

    def test_successful_asr_is_not_recomputed(self):
        calls = []

        def launch(kind, chunk, output, protocol_body):
            calls.append(chunk["chunk_id"])
            driver.save_json(output, asr_record("甲。"))
            return {"returncode": 0, "stderr": "           222  maximum resident set size\n", "elapsed_seconds": 0.3}

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            write_layout(run, [driver.SAMPLE_RATE, driver.SAMPLE_RATE])
            driver.stage_asr(run, launch=launch)
            driver.stage_asr(run, launch=launch)
            self.assertEqual(calls, ["c0000", "c0001"])
            self.assertEqual(len(driver.load_json(run / "asr-metrics.json")), 2)

    def test_more_than_four_documents_keep_every_character(self):
        text = "甲。乙。丙。丁。戊。"
        units = [
            {"text": char, "start_time": float(index * 10), "end_time": float(index * 10 + 0.2)}
            for index, char in enumerate("甲乙丙丁戊")
        ]
        chunk = {
            "chunk_id": "c0000",
            "start_sample": 0,
            "end_sample": 50 * driver.SAMPLE_RATE,
            "duration_seconds": 50.0,
        }
        rows = driver.documents_for_chunk(
            chunk,
            protocol(),
            text,
            {"input_text": text, "alignment_executed": True, "units": units},
            "a" * 64,
            "b" * 64,
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual("".join(row["text"] for row in rows), text)
        self.assertTrue(all(row["end_ticks"] <= 120_881_00000 for row in rows))
        self.assertTrue(all(row["start_ticks"] < row["end_ticks"] for row in rows))

    def test_exact_five_second_localization_uses_stored_ticks(self):
        row = driver.candidate_row(
            {"chunk_id": "c0000", "start_sample": 0, "end_sample": 20 * driver.SAMPLE_RATE},
            protocol(), "甲。", 0, 2, 3.12, 8.12,
            {"candidate_id": "c0000-U01", "overlong_retained": False},
        )
        self.assertEqual(row["end_ticks"] - row["start_ticks"], 50_000_000)
        self.assertTrue(row["localization_eligible"])

    def test_overlong_sentence_is_retained_and_not_forced_local(self):
        text = "甲乙丙丁"
        units = [{"text": text, "start_time": 0.0, "end_time": 12.0}]
        chunk = {
            "chunk_id": "c0009",
            "start_sample": 0,
            "end_sample": 20 * driver.SAMPLE_RATE,
            "duration_seconds": 20.0,
        }
        rows = driver.documents_for_chunk(
            chunk,
            protocol(),
            text,
            {"input_text": text, "alignment_executed": True, "units": units},
            "a" * 64,
            "b" * 64,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], text)
        self.assertTrue(rows[0]["overlong_retained"])
        self.assertFalse(rows[0]["localization_eligible"])

    def test_zero_duration_unit_is_flagged(self):
        text = "甲。"
        units = [{"text": "甲", "start_time": 2.0, "end_time": 2.0}]
        chunk = {
            "chunk_id": "c0002",
            "start_sample": 0,
            "end_sample": 12 * driver.SAMPLE_RATE,
            "duration_seconds": 12.0,
        }
        rows = driver.documents_for_chunk(
            chunk,
            protocol(),
            text,
            {"input_text": text, "alignment_executed": True, "units": units},
            "a" * 64,
            "b" * 64,
        )
        self.assertEqual(rows[0]["provenance"]["zero_duration_unit_indices"], [0])
        self.assertEqual(rows[0]["text"], text)

    def test_align_skips_empty_text_and_documents_cover_characters(self):
        calls = []

        def launch(kind, chunk, output, protocol_body):
            calls.append(chunk["chunk_id"])
            asr = driver.load_json(driver.output_path(output.parent.parent, "asr", chunk["chunk_id"]))
            text = asr["raw_result"]["text"]
            units = [{"text": "甲", "start_time": 0.0, "end_time": 0.4}, {"text": "乙", "start_time": 9.0, "end_time": 9.4}]
            driver.save_json(output, align_record(chunk, text, units))
            return {"returncode": 0, "stderr": "           333  maximum resident set size\n", "elapsed_seconds": 0.4}

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            chunks = write_layout(
                run,
                [driver.SAMPLE_RATE, 12 * driver.SAMPLE_RATE, 12 * driver.SAMPLE_RATE, 12 * driver.SAMPLE_RATE],
            )
            texts = ["", "。。。", "甲。乙。", "甲。乙。"]
            for chunk, text in zip(chunks, texts):
                write_asr(run, chunk, text)
            mark_succeeded(run, "asr", chunks)
            driver.stage_align(run, launch=launch)
            driver.stage_align(run, launch=launch)
            self.assertEqual(calls, ["c0002", "c0003"])
            rows = driver.stage_documents(run)
            stamp = (run / "candidates.json").stat().st_mtime_ns
            again = driver.stage_documents(run)
            self.assertEqual(again, rows)
            self.assertEqual((run / "candidates.json").stat().st_mtime_ns, stamp)
            retained = driver.load_json(run / "non-substantive.json")
            pieces = []
            for chunk in chunks:
                pieces.extend(row["text"] for row in rows if row["provenance"]["chunk_id"] == chunk["chunk_id"])
                pieces.extend(item["text"] for item in retained if item["chunk_id"] == chunk["chunk_id"])
            self.assertEqual("".join(pieces), "".join(texts))
            self.assertEqual(len(rows), 4)
            self.assertNotIn("。。。", [row["text"] for row in rows])
            self.assertTrue(all(row["start_ticks"] < row["end_ticks"] <= 120_881_00000 for row in rows))
            self.assertTrue(all(isinstance(row["start_ticks"], int) and isinstance(row["end_ticks"], int) for row in rows))
            self.assertEqual([item["text"] for item in retained], ["", "。。。"])
            self.assertTrue(all(item["embed"] is False and item["valid_utterance"] is False for item in retained))
            self.assertEqual(retained[1]["skip_reason"], "non_substantive")
            for row in rows:
                for key in ("candidate_id", "text", "start_ticks", "end_ticks", "localization_eligible", "provenance"):
                    self.assertIn(key, row)
                self.assertIn("asr_raw_sha256", row["provenance"])
                self.assertIn("align_raw_sha256", row["provenance"])

    def test_punctuation_is_retained_but_not_embedded(self):
        chunk = {
            "chunk_id": "c0001",
            "start_sample": 0,
            "end_sample": 12 * driver.SAMPLE_RATE,
            "duration_seconds": 12.0,
        }
        align = {"input_text": "。。。", "alignment_executed": False, "skip_reason": "non_substantive", "units": []}
        self.assertEqual(driver.documents_for_chunk(chunk, protocol(), "。。。", align, "a" * 64, "b" * 64), [])
        kept = driver.non_substantive_retention(chunk, "  。", align, "a" * 64, "b" * 64)
        self.assertEqual(kept["text"], "  。")
        self.assertFalse(kept["embed"])
        self.assertFalse(kept["valid_utterance"])

    def test_last_unit_out_of_bounds_is_kept_and_rejected(self):
        calls = []

        def launch(kind, chunk, output, protocol_body):
            calls.append(chunk["chunk_id"])
            text = "甲乙"
            units = [
                {"text": "甲", "start_time": 0.0, "end_time": 1.0},
                {"text": "乙", "start_time": 11.0, "end_time": 12.05},
            ]
            driver.save_json(output, align_record(chunk, text, units))
            return {"returncode": 0, "stderr": "", "elapsed_seconds": 0.2}

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            chunks = write_layout(run, [12 * driver.SAMPLE_RATE])
            write_asr(run, chunks[0], "甲乙")
            mark_succeeded(run, "asr", chunks)
            before = None
            with self.assertRaises(driver.StageBlocked) as caught:
                driver.stage_align(run, launch=launch)
            self.assertIn("last-unit-out-of-bounds", str(caught.exception))
            saved = driver.load_json(driver.output_path(run, "align", "c0000"))
            self.assertEqual([unit["text"] for unit in saved["units"]], ["甲", "乙"])
            task = driver.load_json(run / "align-tasks.json")[0]
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["retained_text"], "甲乙")
            self.assertEqual([unit["text"] for unit in task["retained_units"]], ["甲", "乙"])
            before = driver.file_sha256(driver.output_path(run, "align", "c0000"))
            with self.assertRaises(driver.StageBlocked):
                driver.stage_align(run, launch=launch)
            self.assertEqual(calls, ["c0000"])
            self.assertEqual(driver.file_sha256(driver.output_path(run, "align", "c0000")), before)
            mark_succeeded(run, "align", chunks)
            with self.assertRaises(driver.UnitBoundsRejected) as rejected:
                driver.stage_documents(run)
            self.assertIn("乙", str(rejected.exception))
            self.assertIn("未删词", str(rejected.exception))
            self.assertFalse((run / "candidates.json").exists())
            state = driver.load_json(run / "documents-state.json")
            self.assertEqual([unit["text"] for unit in state["retained"]["units"]], ["甲", "乙"])
            self.assertEqual(state["retained"]["text"], "甲乙")

    def test_illegal_source_time_stops_documents(self):
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            chunks = write_layout(run, [driver.SAMPLE_RATE], runtime=1000)
            write_asr(run, chunks[0], "甲。")
            mark_succeeded(run, "asr", chunks)
            driver.save_json(
                driver.output_path(run, "align", "c0000"),
                align_record(chunks[0], "甲。", [{"text": "甲", "start_time": 0.0, "end_time": 0.2}]),
            )
            mark_succeeded(run, "align", chunks)
            with self.assertRaises(driver.StageBlocked) as caught:
                driver.stage_documents(run)
            self.assertIn("源时间不合法", str(caught.exception))

    def test_cli_documents_without_chunks_stops(self):
        previous = os.umask(0)
        os.umask(previous)
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            save_protocol(run, protocol())
            code = driver.main(["--run", str(run), "--stage", "documents"])
            os.umask(previous)
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
