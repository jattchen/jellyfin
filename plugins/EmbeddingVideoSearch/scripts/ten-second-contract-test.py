#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load("ten_second_contract_test", "ten-second-contract.py")
embed = load("film_embed_for_ten", "full-film-embed.py")
importer = load("film_import_ten_test", "ten-second-import.py")

WEB3 = "6ec09f8e-46a9-3e51-a70d-42c7cb242231"
WEB2 = "f9f88dca-d323-22df-0890-4c9bae0158fe"
WEB1 = "7bd8bb2a-11a3-7d25-9efb-9c18afac0339"
LIBRARY = "31d420df-4b1d-fbf1-c727-abf331f6fd9d"


def signature(processing_id="from-live-service"):
    return {"id": contract.MODEL_ID, "revision": contract.REVISION, "processing_id": processing_id,
            "dimension": 768, "normalized": True}


def joint_processing(video_seconds=10, audio_seconds=10.05, fps=False, samples=None):
    video = {
        "duration_seconds": video_seconds, "audio_included": True, "has_audio": True,
        "sampled_seconds": contract.SAMPLED_SECONDS if samples is None else samples,
        "frame_count": 5 if samples is None else len(samples),
        "profile": contract.PROFILE, "offsets_seconds": contract.SAMPLED_SECONDS,
        "window_seconds": 10, "max_soft_tokens": 70, "add_timestamps": False,
        "overflow_strategy": "reject",
    }
    if fps:
        video["fps"] = 1
    return {"modalities": ["video", "audio"], "video": video, "audio": {
        "duration_seconds": audio_seconds, "sample_rate": 16000, "channels": 1,
        "truncated": False, "coverage": "full_track"}}


def response(processing, processing_id="from-live-service"):
    vector = [1.0] + [0.0] * 767
    return {"model": contract.MODEL_ID, "revision": contract.REVISION, "processing_id": processing_id,
            "dimension": 768, "normalized": True, "data": [{"index": 0, "embedding": vector, "processing": processing}]}


def source(item, runtime, path, content):
    return {"sourceItemId": item, "itemId": item, "mediaSourceId": uuid.UUID(item).hex,
            "libraryId": LIBRARY, "contentVersion": content, "runtimeTicks": runtime,
            "sourcePath": path, "audioStreamIndex": 1, "sizeBytes": 3}


def unit():
    return [1.0] + [0.0] * 767


class Metadata(unittest.TestCase):
    def test_service_metadata_has_no_fps_and_keeps_full_audio(self):
        contract.validate_joint_processing(joint_processing())
        contract.validate_joint_processing(joint_processing(audio_seconds=10.2))
        with self.assertRaises(contract.ContractError):
            contract.validate_joint_processing(joint_processing(fps=True))
        with self.assertRaises(contract.ContractError):
            contract.validate_joint_processing(joint_processing(audio_seconds=9.8))
        with self.assertRaises(contract.ContractError):
            contract.validate_joint_processing(joint_processing(samples=list(range(10))))

    def test_six_second_checker_still_requires_fps_one(self):
        with self.assertRaises(AssertionError):
            embed.validate_response(response(joint_processing()), signature(), "joint")
        six = joint_processing()
        six["video"] = {"duration_seconds": 6, "audio_included": True, "sampled_seconds": list(range(6)),
                        "frame_count": 6, "fps": 1}
        six["audio"] = {"duration_seconds": 6}
        embed.validate_response(response(six), signature(), "joint")
        window = contract.checked_window(contract.interface_template()["window"])
        embed.validate_response(response(joint_processing()), signature(), "joint", window)

    def test_old_checkpoint_is_not_reused_for_the_new_signature(self):
        entry = {"candidate_id": "text-a", "text": "原文"}
        old = signature("old-6s-service")
        new = signature("from-live-service")
        protocol = {"signature": new, "window": contract.interface_template()["window"]}
        new_hash, _ = embed.embedding_input(protocol, "text", entry)
        old_hash, _ = embed.embedding_input({"signature": old}, "text", entry)
        self.assertNotEqual(new_hash, old_hash)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "response.json"
            state = path.with_suffix(".state.json")
            embed.save(path, response({"modalities": ["text"], "task": "document"}, old["processing_id"]))
            embed.save(state, {"input_hash": old_hash, "status": "succeeded", "response_sha256": embed.sha(path)})
            with self.assertRaises(AssertionError):
                embed.cached(path, state, new_hash, new, "text")
            embed.save(state, {"input_hash": new_hash, "status": "completion-unknown"})
            with self.assertRaises(RuntimeError):
                embed.cached(path, state, new_hash, new, "text")

    def test_signature_is_not_invented(self):
        template = contract.interface_template()
        self.assertIsNone(template["signature"]["processing_id"])
        with self.assertRaises(contract.ContractError):
            contract.require_signature(template["signature"])
        contract.require_signature(signature("eg2-0b01d27c8c20b7d920e5"))


class WindowsAndIdentity(unittest.TestCase):
    def test_runtime_counts_and_tail_do_not_follow_the_run_label(self):
        self.assertEqual(len(contract.plan_windows(12_088_100_000)), 151)
        self.assertEqual(len(contract.plan_windows(11_567_786_670)), 145)
        web3 = contract.plan_windows(17_101_752_380)
        self.assertEqual(len(web3), 214)
        self.assertEqual(web3[-1], (17_001_752_380, 17_101_752_380))
        self.assertEqual(web3[-1][1] - web3[-1][0], contract.LENGTH_TICKS)
        films = [
            {"runLabel": "E3", "display": {"episodeNumber": 2, "displayName": "SemanticAcceptance-S01E02"},
             "source": source(WEB2, 11_567_786_670, "/media/SemanticAcceptance-S01E02.mp4", "web2-content")},
            {"runLabel": "E2", "display": {"episodeNumber": 3, "displayName": "SemanticAcceptance-S01E03"},
             "source": source(WEB3, 17_101_752_380, "/media/SemanticAcceptance-S01E03.mp4", "web3-content")},
        ]
        resolved = contract.resolve_film(films, WEB3)
        record = contract.film_record(resolved)
        self.assertEqual(record["displayEpisodeNumber"], 3)
        self.assertNotEqual(int(record["runLabel"][1:]), record["displayEpisodeNumber"])
        self.assertFalse(record["runLabelIsEpisodeNumber"])
        self.assertEqual(record["runtimeTicks"], 17_101_752_380)
        self.assertEqual(len(contract.plan_windows(resolved["source"]["runtimeTicks"])), 214)

    def test_clip_uses_container_time_and_skips_the_attached_picture(self):
        web3 = {"streams": [
            {"index": 0, "codec_name": "hevc", "codec_type": "video", "width": 3840, "height": 2160,
             "start_time": "0.023242", "duration": "1710.146029"},
            {"index": 1, "codec_name": "aac", "codec_type": "audio", "start_time": "0.000000",
             "duration": "1710.175238"},
        ]}
        arguments, facts = contract.clip_ffmpeg_arguments("/media/S01E03.mp4", 17_001_752_380, 1, "/tmp/out.mp4", web3)
        self.assertEqual(arguments[arguments.index("-ss") + 1], "1700.1752380")
        self.assertNotIn("0.023242", arguments)
        self.assertFalse(facts["video_start_added"])
        self.assertEqual(arguments[arguments.index("-map") + 1], "0:v:0")
        self.assertEqual(arguments[arguments.index("-map") + 3], "0:1")
        web2 = {"streams": [
            {"index": 0, "codec_name": "hevc", "codec_type": "video", "width": 3840, "height": 2160,
             "start_time": "0.000000", "duration": "1156.760000"},
            {"index": 1, "codec_name": "aac", "codec_type": "audio", "sample_rate": "48000",
             "start_time": "0.000000", "duration": "1156.778667"},
            {"index": 2, "codec_name": "mjpeg", "codec_type": "video", "width": 3928, "height": 3172,
             "start_time": "0.000000", "duration": "1156.778667"},
        ]}
        arguments, facts = contract.clip_ffmpeg_arguments("/media/S01E02.mp4", 0, 1, "/tmp/out.mp4", web2)
        self.assertEqual(facts["attached_picture_stream_indexes"], [2])
        self.assertNotIn("0:2", arguments)
        self.assertEqual(arguments.count("-map"), 2)

    def test_blocked_alignment_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            source_run = Path(directory) / "asr-source"
            dest = Path(directory) / "embed-E1"
            source_run.mkdir()
            dest.mkdir()
            rows = [{"chunk_id": "c0050", "status": "failed"}]
            for name, value in {
                "chunks.json": [{"chunk_id": "c0050"}],
                "prepare.json": {"status": "succeeded"},
                "documents-state.json": {"status": "failed"},
                "asr-tasks.json": [{"chunk_id": "c0050", "status": "succeeded"}],
                "align-tasks.json": rows,
                "candidates.json": [],
            }.items():
                (source_run / name).write_text(json.dumps(value), encoding="utf-8")
            before = (source_run / "align-tasks.json").read_bytes()
            with self.assertRaises(contract.ContractError) as caught:
                contract.adopt_transcription(source_run, dest, 1, 0)
            self.assertIn("failed or unknown", str(caught.exception))
            self.assertEqual((source_run / "align-tasks.json").read_bytes(), before)
            self.assertFalse((dest / "candidates.json").exists())

    def test_failed_embedding_cannot_be_imported(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            (run / "text").mkdir()
            entry = {"candidate_id": "text-a", "text": "原文"}
            state = run / "text" / "text-a.state.json"
            state.write_text(json.dumps({"status": "completion-unknown"}), encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                importer.importer.verified_vector(run, entry, {"signature": signature()}, "text")
            self.assertIn("Unresolved prior dispatch", str(caught.exception))
            for status in ("failed", "unknown", "blocked"):
                with self.assertRaises(contract.ContractError):
                    contract.require_closed(status, "align c0050")


class EmptyIndex(unittest.TestCase):
    def test_three_films_land_in_a_new_index_by_item_id(self):
        films = [
            (WEB1, 12_088_100_000, "/media/SemanticAcceptance-S01E01.mp4", "e1-content", "E1", None),
            (WEB2, 11_567_786_670, "/media/SemanticAcceptance-S01E02.mp4", "web2-content", "E3", 2),
            (WEB3, 17_101_752_380, "/media/SemanticAcceptance-S01E03.mp4", "web3-content", "E2", 3),
        ]
        built = []
        for item, runtime, path, content, label, episode in films:
            body = source(item, runtime, path, content)
            windows = contract.window_entries(body)
            joint = [importer.importer.candidate_row(
                {"candidate_id": entry["candidate_id"], "start_ticks": entry["start_ticks"], "end_ticks": entry["end_ticks"]},
                body, unit()) for entry in windows]
            original = {"candidate_id": "c0000-U01", "start_ticks": 0, "end_ticks": contract.LENGTH_TICKS, "text": "一句"}
            namespaced, mapping = importer.namespace_text_entry(body, original)
            text = [importer.importer.candidate_row(namespaced, body, unit(), original["text"])]
            film = {"runLabel": label, "source": body, "joint_rows": joint, "text_rows": text,
                    "text_id_map": [mapping], "signature": signature("from-live-service"),
                    "window": contract.interface_template()["window"]}
            if episode is not None:
                film["display"] = {"episodeNumber": episode}
            built.append(film)
        references = [[index / 16] + [0.0] * 767 for index in range(16)]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-index"
            evidence = importer.build_empty_index(
                built, references, signature("from-live-service"), "queries", output, "http://127.0.0.1:18783")
            by_item = {row["itemId"]: row["windows"] for row in evidence["films"]}
            self.assertEqual(by_item[contract.canonical_item(WEB1)], 151)
            self.assertEqual(by_item[contract.canonical_item(WEB2)], 145)
            self.assertEqual(by_item[contract.canonical_item(WEB3)], 214)
            self.assertTrue(evidence["emptyNewIndex"])
            self.assertFalse(evidence["priorIndexOpened"])
            self.assertFalse(evidence["runLabelIsEpisodeNumber"])
            connection = __import__("sqlite3").connect(output / "prepared-index.db")
            stored = connection.execute("SELECT processing_id, window_version FROM signatures").fetchone()
            self.assertEqual(stored, ("from-live-service", contract.VERSION))
            counts = dict(connection.execute("SELECT item_id, count(*) FROM windows GROUP BY item_id"))
            self.assertEqual(counts[contract.canonical_item(WEB3)], 214)
            self.assertEqual(connection.execute("SELECT runtime_ticks FROM media WHERE item_id=?",
                                                (contract.canonical_item(WEB3),)).fetchone()[0], 17_101_752_380)
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='imported_candidates_only'").fetchone()[0], "1")
            self.assertEqual(connection.execute("SELECT value FROM settings WHERE key='embedding_base_url'").fetchone()[0],
                             "http://127.0.0.1:18783")
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM candidate_representations WHERE channel='text'").fetchone()[0])
            stored_ids = [row["candidateId"] for row in payload["candidates"]]
            mapped = evidence["textCandidateMap"]
            self.assertEqual(sorted(stored_ids), sorted(row["candidateId"] for row in mapped))
            self.assertEqual({row["originalCandidateId"] for row in mapped}, {"c0000-U01"})
            self.assertEqual(len(set(stored_ids)), 3)
            self.assertTrue(all(row["transcript"] == "一句" and row["startTicks"] == 0 for row in payload["candidates"]))
            self.assertEqual(connection.execute("SELECT count(*) FROM media").fetchone()[0], 3)
            connection.close()
            with self.assertRaises(importer.contract.ContractError):
                importer.build_empty_index(
                    built, references, signature(), "queries", output, "http://127.0.0.1:18783")

    def test_other_service_signature_is_rejected_before_any_output(self):
        other = signature("other-service")
        embed.validate_response(response({"modalities": ["text"], "task": "document"}, "other-service"), other, "text")
        films = [
            source(WEB1, 12_088_100_000, "/media/SemanticAcceptance-S01E01.mp4", "e1-content"),
            source(WEB2, 11_567_786_670, "/media/SemanticAcceptance-S01E02.mp4", "web2-content"),
            source(WEB3, 17_101_752_380, "/media/SemanticAcceptance-S01E03.mp4", "web3-content"),
        ]
        root = {"signature": signature("from-live-service"), "window": contract.interface_template()["window"],
                "embedding_base_url": "http://127.0.0.1:18783", "films": [{"source": body} for body in films]}
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "embed-E2"
            run.mkdir()
            film = {"signature": other, "window": root["window"], "source": films[2]}
            (run / "protocol.json").write_text(json.dumps(film), encoding="utf-8")
            output = Path(directory) / "new-index"
            with self.assertRaises(importer.contract.ContractError) as caught:
                importer.import_films(root, [run], Path(directory) / "references", output)
            self.assertIn("root signature", str(caught.exception))
            self.assertFalse(output.exists())

    def test_shared_18780_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new-index"
            for url in ("", "http://127.0.0.1:18780"):
                with self.assertRaises(importer.contract.ContractError):
                    importer.build_empty_index([], [], signature("from-live-service"), "queries", output, url)
                self.assertFalse(output.exists())

    def test_prefetch_calls_the_ten_second_clip_and_stays_one_ahead(self):
        runtime = 260_000_000
        body = source(WEB1, runtime, "/media/SemanticAcceptance-S01E01.mp4", "e1-content")
        windows = contract.window_entries(body)
        self.assertEqual(len(windows), 3)
        probe = {"streams": [
            {"index": 0, "codec_name": "hevc", "codec_type": "video", "width": 3840, "height": 2160,
             "start_time": "0.000000"},
            {"index": 1, "codec_name": "aac", "codec_type": "audio", "start_time": "0.000000"},
        ]}
        protocol = {"embedding_base_url": "http://127.0.0.1:18783", "max_inflight_embeddings": 1,
                    "signature": signature("from-live-service"), "window": contract.interface_template()["window"],
                    "source": body, "probe_facts": probe, "joint_windows": windows}
        calls, active = [], [0, 0]
        second_started, release_second = threading.Event(), threading.Event()

        def fake_clip(run, entry, source_arg, window=None, probe_arg=None):
            self.assertEqual(window["version"], contract.VERSION)
            self.assertEqual(window["sampled_seconds"], [0, 2, 4, 6, 8])
            self.assertIs(probe_arg, probe)
            active[0] += 1
            active[1] = max(active[1], active[0])
            calls.append(entry["candidate_id"])
            try:
                if len(calls) == 2:
                    second_started.set()
                    if not release_second.wait(3):
                        raise AssertionError("prefetch was not released")
            finally:
                active[0] -= 1

        args = type("Args", (), {"stage": "joint", "prefetch_clips": True, "max_new_jobs": None})()
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "embed-E1"
            run.mkdir()
            with patch.object(embed, "clip", fake_clip), patch.object(embed, "dispatch", return_value=(False, {"seconds": 0})):
                worker = threading.Thread(target=contract.run_embedding, args=(embed, args, run, protocol))
                worker.start()
                try:
                    self.assertTrue(second_started.wait(3))
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(active[1], 1)
                finally:
                    release_second.set()
                    worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(calls), 3)
        self.assertEqual(active, [0, 1])


if __name__ == "__main__":
    unittest.main()
