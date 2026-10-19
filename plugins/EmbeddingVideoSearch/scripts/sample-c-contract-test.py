#!/usr/bin/env python3
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = load("sample_c_contract_test", "sample-c-contract.py")


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


if __name__ == "__main__":
    unittest.main()
