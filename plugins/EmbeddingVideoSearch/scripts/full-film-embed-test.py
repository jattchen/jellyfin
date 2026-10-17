#!/usr/bin/env python3
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import math
from pathlib import Path
import statistics
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


embed = load('film_embed_test', 'full-film-embed.py')
importer = load('film_import_test', 'full-film-import.py')


class Checkpoints(unittest.TestCase):
    def test_local_service_bypasses_a_broken_system_proxy(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ready": true}')

            do_POST = do_GET

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with patch('urllib.request.getproxies', return_value={'http': 'http://127.0.0.1:1'}), \
                    patch('urllib.request.proxy_bypass', return_value=False):
                with self.assertRaises(urllib.error.URLError):
                    urllib.request.build_opener().open(url, timeout=1)
                self.assertEqual(embed.get(url, '/readyz'), {'ready': True})
                with embed.LOCAL_HTTP.open(urllib.request.Request(url, data=b'{}'), timeout=1) as response:
                    self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_cpu_prefetch_is_bounded_to_one_next_clip_and_closed_cleanly(self):
        second_started, release_second = threading.Event(), threading.Event()
        calls, active = [], [0, 0]

        def prepare(run, entry, source):
            active[0] += 1
            active[1] = max(active)
            calls.append(entry)
            try:
                if entry == 2:
                    second_started.set()
                    if not release_second.wait(3):
                        raise AssertionError('test did not release the next clip')
            finally:
                active[0] -= 1

        with patch.object(embed, 'clip', prepare):
            jobs = embed.prepared_entries(None, [1, 2, 3], None, True)
            try:
                self.assertEqual(next(jobs), 1)
                self.assertTrue(second_started.wait(3))
                self.assertEqual(calls, [1, 2])
            finally:
                release_second.set()
                jobs.close()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(active, [0, 1])

    def test_serial_and_text_iteration_do_not_prepare_clips(self):
        with patch.object(embed, 'clip', side_effect=AssertionError('unexpected clip')):
            self.assertEqual(list(embed.prepared_entries(None, [1, 2], None, False)), [1, 2])

    def fixture(self):
        signature = {'id': 'google/embeddinggemma-2', 'revision': 'fixed',
                     'processing_id': 'processing-fixed', 'dimension': 768, 'normalized': True}
        response = {'model': signature['id'], **{k: signature[k] for k in
            ['revision', 'processing_id', 'dimension', 'normalized']},
            'data': [{'index': 0, 'embedding': [1.0] + [0.0] * 767,
                      'processing': {'modalities': ['text'], 'task': 'document'}}]}
        return signature, response

    def test_successful_input_is_reused_without_dispatch(self):
        signature, response = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path, state = Path(directory) / 'response.json', Path(directory) / 'response.state.json'
            embed.save(path, response)
            embed.save(state, {'input_hash': 'same', 'status': 'succeeded', 'response_sha256': embed.sha(path)})
            self.assertIsNotNone(embed.cached(path, state, 'same', signature, 'text'))

    def test_unknown_completion_cannot_be_silently_retried(self):
        signature, _ = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path, state = Path(directory) / 'response.json', Path(directory) / 'response.state.json'
            embed.save(state, {'input_hash': 'same', 'status': 'completion-unknown'})
            with self.assertRaises(RuntimeError):
                embed.cached(path, state, 'same', signature, 'text')

    def test_changed_input_or_cached_bytes_are_rejected(self):
        signature, response = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            path, state = Path(directory) / 'response.json', Path(directory) / 'response.state.json'
            embed.save(path, response)
            embed.save(state, {'input_hash': 'same', 'status': 'succeeded', 'response_sha256': embed.sha(path)})
            with self.assertRaises(AssertionError):
                embed.cached(path, state, 'different', signature, 'text')
            response['processing_id'] = 'edited'
            embed.save(path, response)
            with self.assertRaises(AssertionError):
                embed.cached(path, state, 'same', signature, 'text')

    def test_model_signature_is_checked_even_with_matching_hash(self):
        signature, response = self.fixture()
        response['processing_id'] = 'different'
        with self.assertRaises(AssertionError):
            embed.validate_response(response, signature, 'text')

    def test_visual_only_is_not_joint(self):
        signature, response = self.fixture()
        response['data'][0]['processing'] = {'modalities': ['video'], 'video': {'audio_included': False}}
        with self.assertRaises(AssertionError):
            embed.validate_response(response, signature, 'joint')


class CandidatePool(unittest.TestCase):
    def fixture(self):
        source = {'sourceItemId': '11111111-1111-1111-1111-111111111111',
                  'itemId': '11111111-1111-1111-1111-111111111111',
                  'mediaSourceId': 'source-a', 'libraryId': '33333333-3333-3333-3333-333333333333',
                  'contentVersion': 'version-a', 'runtimeTicks': 120_000_000}
        vector = [1.0] + [0.0] * 767
        entry = {'candidate_id': 'new-a', 'start_ticks': 0, 'end_ticks': 60_000_000}
        new = importer.candidate_row(entry, source, vector)
        old = copy.deepcopy(new)
        old.update(candidateId='old-a', segmentId='old-a-segment')
        other = copy.deepcopy(new)
        other.update(itemId='22222222-2222-2222-2222-222222222222', candidateId='other-film',
                     segmentId='other-old-segment', anchorId='other-anchor', transcript='other original text')
        references = [[i / 16] + [0.0] * 767 for i in range(16)]
        prior = {'channel': 'joint', 'candidates': [old, other],
                 'inputVersion': 'fixed-input', 'embeddingTask': 'video_audio'}
        return source, new, other, references, prior

    def test_only_selected_film_is_replaced_and_prior_pool_is_immutable(self):
        source, new, other, references, prior = self.fixture()
        before = copy.deepcopy(prior)
        bundle, _, _ = importer.build_bundle(prior, source, [new], references, 'reference-cache', {})
        self.assertEqual(prior, before)
        self.assertEqual([c['candidateId'] for c in bundle['candidates']], ['other-film', 'new-a'])
        retained = bundle['candidates'][0]
        self.assertEqual(retained['vector'], other['vector'])
        self.assertEqual(retained['transcript'], other['transcript'])
        self.assertEqual((retained['startTicks'], retained['endTicks']), (other['startTicks'], other['endTicks']))

    def test_reference_curve_recomputed_with_the_same_sixteen_query_formula(self):
        source, new, _, references, prior = self.fixture()
        bundle, matrix, _ = importer.build_bundle(prior, source, [new], references, 'reference-cache', {})
        self.assertEqual(len(matrix), 16)
        self.assertTrue(all(len(row) == 2 for row in matrix))
        points = [value for row in matrix for value in row]
        expected = statistics.pstdev(points) * 16 ** (-1 / 5) * math.sqrt(3) / math.pi
        self.assertAlmostEqual(bundle['scale'], expected, places=15)
        self.assertEqual(bundle['algorithm'], 'reference16-logistic-tail-f64-v1')
        self.assertEqual(len(bundle['background']), 32)

    def test_out_of_source_time_range_is_rejected(self):
        source, _, _, _, _ = self.fixture()
        with self.assertRaises(AssertionError):
            importer.candidate_row({'candidate_id': 'bad', 'start_ticks': 100_000_000,
                                   'end_ticks': 130_000_000}, source, [1.0] + [0.0] * 767)


class SharedDocumentCache(unittest.TestCase):
    def signature(self, processing_id="from-live-service"):
        return {"id": "google/embeddinggemma-2", "revision": "914f7f89142e33e77833254d9c9b90c3cef7303b",
                "processing_id": processing_id, "dimension": 768, "normalized": True}

    def window(self):
        return {"version": "10s-8s-5f70-tail-v1", "length_ticks": 100_000_000, "step_ticks": 80_000_000,
                "sampled_seconds": [0, 2, 4, 6, 8], "fps": 0.5, "max_soft_tokens": 70,
                "add_timestamps": False, "profile": "jellyfin-10s-5f"}

    def source(self, directory):
        path = Path(directory) / "film.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"film")
        stat = path.stat()
        return {"sourcePath": str(path), "sizeBytes": stat.st_size,
                "modifiedTicks": stat.st_mtime_ns // 100 + 621355968000000000}

    def payload(self, signature):
        return {"model": signature["id"], "revision": signature["revision"], "processing_id": signature["processing_id"],
                "dimension": 768, "normalized": True, "data": [{"index": 0, "embedding": [1.0] + [0.0] * 767,
                "processing": {"modalities": ["text"], "task": "document"}}]}

    def protocol(self, directory, cache, processing_id="from-live-service", shared=True):
        body = {"signature": self.signature(processing_id), "source": self.source(directory),
                "window": self.window(), "embedding_base_url": "http://127.0.0.1:18783",
                "max_inflight_embeddings": 1}
        if shared:
            body["shared_document_cache"] = str(cache)
        return body

    def entry(self, candidate_id, start):
        return {"candidate_id": candidate_id, "text": "同一句", "start_ticks": start,
                "end_ticks": start + 40_000_000}

    def post(self, protocol, entry, payload, posts):
        class Response:
            status = 200

            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout=None):
            posts.append(json.loads(request.data.decode()))
            return Response()

        with patch.object(embed, "await_idle", lambda *_args, **_kwargs: None), \
                patch.object(embed.LOCAL_HTTP, "open", opener):
            return embed.dispatch(Path(protocol["source"]["sourcePath"]).parent, entry, protocol, "text")

    def test_same_text_different_times_posts_once_and_keeps_each_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "shared-document-vectors"
            first_home, second_home = root / "embed-a", root / "embed-b"
            first = self.protocol(first_home, cache)
            second = self.protocol(second_home, cache)
            left, right = self.entry("c0000-U01", 0), self.entry("c0004-U02", 80_000_000)
            before = (left["text"], left["start_ticks"], right["text"], right["end_ticks"])
            self.assertEqual(embed.embedding_input(first, "text", left)[0], embed.embedding_input(second, "text", right)[0])
            posts = []
            fresh, _ = self.post(first, left, self.payload(first["signature"]), posts)
            reused, state = self.post(second, right, self.payload(second["signature"]), posts)
            self.assertTrue(fresh)
            self.assertFalse(reused)
            self.assertEqual(len(posts), 1)
            self.assertTrue(state["model_input_reused"])
            self.assertTrue(str(state["reused_from"]).endswith(embed.embedding_input(first, "text", left)[0] + ".json"))
            own = embed.read(second_home / "text" / "c0004-U02.state.json")
            self.assertEqual(own["candidate_id"], "c0004-U02")
            self.assertNotEqual((second_home / "text" / "c0004-U02.state.json").resolve(),
                                Path(state["reused_from"]).with_suffix(".state.json").resolve())
            self.assertEqual(before, (left["text"], left["start_ticks"], right["text"], right["end_ticks"]))
            self.assertFalse((first_home / "text" / "c0000-U01.state.json").read_text().count("model_input_reused"))

    def test_different_signature_is_not_reused_and_unknown_blocks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "shared-document-vectors"
            home = root / "embed-a"
            original = self.protocol(home, cache)
            entry = self.entry("c0000-U01", 0)
            posts = []
            self.post(original, entry, self.payload(original["signature"]), posts)
            other = self.protocol(home, cache, processing_id="other-signature")
            fresh, state = self.post(other, self.entry("c0009-U01", 10_000_000), self.payload(other["signature"]), posts)
            self.assertTrue(fresh)
            self.assertEqual(len(posts), 2)
            self.assertFalse(state.get("model_input_reused", False))
            blocked = self.protocol(root / "embed-b", cache)
            input_hash, _ = embed.embedding_input(blocked, "text", entry)
            embed.save(cache / (input_hash + ".state.json"),
                       {"status": "completion-unknown", "input_hash": input_hash})
            with patch.object(embed.LOCAL_HTTP, "open", side_effect=AssertionError("posted")):
                with self.assertRaises(RuntimeError) as caught:
                    embed.dispatch(root / "embed-b", self.entry("c0010-U03", 20_000_000), blocked, "text")
            self.assertIn("Unresolved shared document", str(caught.exception))

    def test_disabled_cache_posts_every_candidate_and_protected_paths_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "embed-a"
            protocol = self.protocol(home, root / "unused", shared=False)
            posts = []
            self.post(protocol, self.entry("c0000-U01", 0), self.payload(protocol["signature"]), posts)
            self.post(protocol, self.entry("c0001-U01", 50_000_000), self.payload(protocol["signature"]), posts)
            self.assertEqual(len(posts), 2)
            self.assertIsNone(embed.shared_document_directory(protocol))
            protocol["shared_document_cache"] = "relative-cache"
            with self.assertRaisesRegex(AssertionError, "absolute directory"):
                embed.shared_document_directory(protocol)
            protocol["shared_document_cache"] = str(root / "jellyfin-full-film-20261016" / "shared-document-vectors")
            with self.assertRaises(AssertionError):
                embed.dispatch(home, self.entry("c0002-U01", 0), protocol, "text")


if __name__ == "__main__":
    unittest.main()
