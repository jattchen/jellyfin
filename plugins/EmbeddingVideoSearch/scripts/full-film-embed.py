#!/usr/bin/env python3
"""Sequential one-film embedding pilot. Existing successful inputs are never resent."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


# The frozen pilot endpoint is local. macOS system proxies need not bypass loopback.
LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
TEN_SECOND_VERSION = "10s-8s-5f70-tail-v1"
_CONTRACT = None


def contract_module():
    global _CONTRACT
    if _CONTRACT is None:
        path = Path(__file__).with_name("ten-second-contract.py")
        spec = importlib.util.spec_from_file_location("ten_second_contract", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTRACT = module
    return _CONTRACT


def active_ten_second(protocol):
    window = (protocol or {}).get("window") or {}
    version = window.get("version")
    if version in (None, "", "6s-4s-tail-v1"):
        return None
    if version != TEN_SECOND_VERSION:
        raise AssertionError("Unsupported window version: " + str(version))
    return contract_module().checked_window(window)


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def object_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def source_facts(source):
    path = Path(source['sourcePath'])
    stat = path.stat()
    assert stat.st_size == source['sizeBytes'], 'Source size changed'
    assert stat.st_mtime_ns // 100 + 621355968000000000 == source['modifiedTicks'], 'Source time changed'
    return {'real_path': str(path.resolve()), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def get(base, path):
    with LOCAL_HTTP.open(base + path, timeout=30) as response:
        return json.load(response)


def validate_signature(response, signature, metadata=False):
    expected = {'model': signature['id'], 'revision': signature['revision'],
                'processing_id': signature['processing_id'], 'dimension': signature['dimension'],
                'normalized': signature['normalized']}
    if metadata:
        expected['id'] = expected.pop('model')
    for key, value in expected.items():
        assert response.get(key) == value, 'Model signature mismatch: ' + key


def embedding_input(protocol, kind, entry, clip_sha=None):
    signature = protocol['signature']
    if kind == 'joint':
        payload = {'clip_sha256': clip_sha, 'signature': signature, 'kind': 'joint', 'candidate': entry}
    else:
        payload = {'text': entry['text'], 'signature': signature, 'kind': 'document'}
    window = active_ten_second(protocol)
    if window is not None:
        payload['window_version'] = window['version']
    return object_hash(payload), window


def validate_response(response, signature, kind, window=None):
    validate_signature(response, signature)
    assert len(response['data']) == 1 and response['data'][0]['index'] == 0
    item = response['data'][0]
    vector = item['embedding']
    assert len(vector) == 768 and all(math.isfinite(x) for x in vector)
    assert abs(math.fsum(x * x for x in vector) - 1) < 0.02
    processing = item['processing']
    if kind == 'joint':
        if window is not None:
            contract_module().validate_joint_processing(processing)
        else:
            assert set(processing['modalities']) == {'video', 'audio'}
            assert processing['video']['audio_included'] is True
            assert 5.85 <= processing['video']['duration_seconds'] <= 6.15
            count = max(1, int(processing['video']['duration_seconds']))
            assert processing['video']['sampled_seconds'] == list(range(count))
            assert processing['video']['frame_count'] == count
            assert processing['video']['fps'] == 1
            assert 5.85 <= processing['audio']['duration_seconds'] <= 6.15
    else:
        assert processing == {'modalities': ['text'], 'task': 'document'}
    return vector


def cached(path, state_path, input_hash, signature, kind, window=None):
    if not state_path.exists():
        assert not path.exists(), 'Response without dispatch provenance'
        return None
    state = read(state_path)
    assert state['input_hash'] == input_hash, 'Input changed since dispatch'
    if state['status'] != 'succeeded':
        raise RuntimeError('Unresolved prior dispatch; inspect before any retry: ' + str(state_path))
    assert path.is_file() and sha(path) == state['response_sha256'], 'Cached response changed'
    validate_response(read(path), signature, kind, window)
    return state


PROTECTED_CACHE_NAMES = {'jellyfin-full-film-20261016', 'full-film-20261016'}


def shared_document_directory(protocol):
    """Explicit opt-in. Absent or false keeps one file per candidate and never reads another signature."""
    raw = protocol.get('shared_document_cache')
    if raw in (None, False, ''):
        return None
    if raw is True or (isinstance(raw, dict) and not raw.get('directory')):
        raise AssertionError('shared_document_cache requires an explicit directory')
    directory = Path(raw['directory'] if isinstance(raw, dict) else raw)
    assert directory.is_absolute(), 'shared document cache requires an absolute directory'
    directory = directory.resolve()
    if any(part.startswith('asr-') or part in PROTECTED_CACHE_NAMES for part in directory.parts):
        raise AssertionError('shared document cache must not write accepted or ASR artifacts')
    return directory


def _exclusive_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def _closed_shared_document(directory, input_hash, signature, window):
    state_path = directory / (input_hash + '.state.json')
    response_path = directory / (input_hash + '.json')
    if not state_path.exists():
        assert not response_path.exists(), 'Shared response without dispatch provenance'
        try:
            _exclusive_json(state_path, {'status': 'started', 'input_hash': input_hash, 'kind': 'document'})
        except FileExistsError:
            pass
        else:
            return None
    state = read(state_path)
    assert state.get('input_hash') == input_hash, 'Shared cache hash changed'
    if state.get('status') != 'succeeded':
        raise RuntimeError('Unresolved shared document; inspect before any retry: ' + str(state_path))
    assert response_path.is_file() and sha(response_path) == state['response_sha256'], 'Shared response changed'
    validate_response(read(response_path), signature, 'text', window)
    return response_path, state


def _materialize_reused_document(shared_response, shared_state, response_path, state_path, entry):
    raw = shared_response.read_bytes()
    response_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if response_path.exists():
        assert sha(response_path) == shared_state['response_sha256'], 'Candidate response does not match the shared document'
    else:
        with response_path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    state = {'candidate_id': entry['candidate_id'], 'status': 'succeeded', 'input_hash': shared_state['input_hash'],
             'response_sha256': shared_state['response_sha256'], 'reused_from': str(shared_response),
             'model_input_reused': True, 'seconds': 0}
    save(state_path, state)
    return state


def _publish_shared_document(directory, input_hash, response_path, signature, window):
    shared_response = directory / (input_hash + '.json')
    shared_state = directory / (input_hash + '.state.json')
    raw = response_path.read_bytes()
    if shared_response.exists():
        assert sha(shared_response) == sha(response_path), 'Shared document bytes changed'
    else:
        with shared_response.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    validate_response(read(shared_response), signature, 'text', window)
    state = read(shared_state)
    assert state.get('input_hash') == input_hash
    if state.get('status') != 'succeeded':
        state.update(status='succeeded', response_sha256=sha(shared_response), kind='document')
        save(shared_state, state)


def clip(run, entry, source, window=None, probe=None):
    if window is not None:
        return contract_module().clip_window(run, entry, source, window, probe, sha, save, read, source_facts)
    path = run / 'clips' / (entry['candidate_id'] + '.mp4')
    state_path = path.with_suffix('.clip.json')
    identity = {'source': source_facts(source), 'start_ticks': entry['start_ticks'],
                'end_ticks': entry['end_ticks'], 'profile': 'joint-x264-veryfast-crf28-yuv420p-aac-16000-mono-v1',
                'threads': 2, 'audio_stream_index': source['audioStreamIndex']}
    if state_path.exists():
        state = read(state_path)
        assert state['identity'] == identity and sha(path) == state['sha256'], 'Cached clip changed'
        return path, state
    assert not path.exists(), 'Unverified clip already exists'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.stem + '.partial.mp4')
    assert not temporary.exists(), 'Interrupted clip needs inspection'
    started = time.monotonic()
    subprocess.run(['/usr/local/bin/ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error',
                    '-accurate_seek', '-ss', str(entry['start_ticks'] / 10_000_000),
                    '-i', source['sourcePath'], '-t', '6', '-map', '0:v:0', '-map',
                    '0:' + str(source['audioStreamIndex']), '-c:v', 'libx264', '-preset', 'veryfast',
                    '-crf', '28', '-pix_fmt', 'yuv420p', '-threads', '2', '-c:a', 'aac', '-ac', '1',
                    '-ar', '16000', '-sn', '-dn', str(temporary)], check=True, timeout=180)
    probe = json.loads(subprocess.check_output(['/usr/local/bin/ffprobe', '-v', 'error',
        '-show_entries', 'stream=codec_type,start_time,duration', '-of', 'json', str(temporary)], timeout=20))
    kinds = {s['codec_type'] for s in probe['streams']}
    assert {'video', 'audio'} <= kinds
    for stream in probe['streams']:
        if stream['codec_type'] in {'video', 'audio'}:
            assert abs(float(stream.get('start_time', '0'))) <= 0.15
            assert abs(float(stream['duration']) - 6) <= 0.15
    source_facts(source)
    temporary.rename(path)
    state = {'identity': identity, 'sha256': sha(path), 'probe': probe,
             'clip_seconds': time.monotonic() - started, 'bytes': path.stat().st_size}
    save(state_path, state)
    return path, state


def multipart(signature, path):
    boundary = 'FullFilmPilot-' + sha(path)[:24]
    payload = {'model': signature['id'], 'inputs': [{'video': 'clip', 'video_audio': True}]}
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="request"\r\n\r\n'.encode()
            + json.dumps(payload).encode()
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="clip"; filename="window.mp4"\r\nContent-Type: video/mp4\r\n\r\n'.encode()
            + path.read_bytes() + f'\r\n--{boundary}--\r\n'.encode())
    assert len(body) < 100 * 1024 * 1024
    return body, 'multipart/form-data; boundary=' + boundary


def prepared_entries(run, entries, source, prefetch, window=None, probe=None):
    """Prepare at most the next CPU clip while the caller handles the current input."""
    if not prefetch or not entries:
        yield from entries
        return

    def submit(pool, entry):
        if window is None:
            return pool.submit(clip, run, entry, source)
        return pool.submit(clip, run, entry, source, window, probe)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = submit(pool, entries[0])
        for index, entry in enumerate(entries):
            pending.result()
            if index + 1 < len(entries):
                pending = submit(pool, entries[index + 1])
            yield entry


def await_idle(base, signature, run):
    deadline = time.monotonic() + 300
    while True:
        assert not (run / 'STOP').exists(), 'Stop requested before dispatch'
        ready = get(base, '/readyz')
        assert ready.get('ready') is True, 'Model not ready'
        if ready.get('admitted_requests') == 0:
            model = get(base, '/v1/models')['data'][0]
            validate_signature(model, signature, metadata=True)
            return
        assert time.monotonic() < deadline, 'Shared service did not become idle'
        time.sleep(1)


def dispatch(run, entry, protocol, kind):
    signature = protocol['signature']
    source_facts(protocol['source'])
    if kind == 'joint':
        window = active_ten_second(protocol)
        path, clip_state = clip(run, entry, protocol['source'], window, protocol.get('probe_facts'))
        input_hash, window = embedding_input(protocol, kind, entry, clip_state['sha256'])
    else:
        assert entry['text'].strip()
        input_hash, window = embedding_input(protocol, kind, entry)
        clip_state = None
    response_path = run / kind / (entry['candidate_id'] + '.json')
    state_path = response_path.with_suffix('.state.json')
    previous = cached(response_path, state_path, input_hash, signature, kind, window)
    if previous is not None:
        return False, previous
    shared_directory = shared_document_directory(protocol) if kind == 'text' else None
    shared_claimed = False
    if shared_directory is not None:
        closed = _closed_shared_document(shared_directory, input_hash, signature, window)
        if closed is not None:
            shared_response, shared_state = closed
            reused = _materialize_reused_document(shared_response, shared_state, response_path, state_path, entry)
            return False, reused
        shared_claimed = True
    await_idle(protocol['embedding_base_url'], signature, run)
    if kind == 'joint':
        body, content_type = multipart(signature, path)
    else:
        body = json.dumps({'model': signature['id'], 'inputs': [{'text': entry['text'], 'task': 'document'}]}, ensure_ascii=False).encode()
        content_type = 'application/json'
    state = {'candidate_id': entry['candidate_id'], 'status': 'inflight', 'input_hash': input_hash,
             'source_facts': source_facts(protocol['source']), 'clip': clip_state, 'started_at': time.time()}
    save(state_path, state)
    started = time.monotonic()
    try:
        request = urllib.request.Request(protocol['embedding_base_url'] + '/v1/embeddings', data=body,
            headers={'Content-Type': content_type}, method='POST')
        try:
            with LOCAL_HTTP.open(request, timeout=180) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as error:
            raw, status = error.read(), error.code
        response_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with response_path.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        state.update(status='terminal-response', http_status=status, response_sha256=sha(response_path),
                     seconds=time.monotonic() - started)
        save(state_path, state)
        assert status == 200, 'HTTP failure; no automatic retry: ' + str(status)
        validate_response(json.loads(raw), signature, kind, window)
        source_facts(protocol['source'])
        state['status'] = 'succeeded'
        save(state_path, state)
        if shared_claimed:
            _publish_shared_document(shared_directory, input_hash, response_path, signature, window)
        return True, state
    except BaseException as error:
        if state['status'] == 'inflight':
            state.update(status='completion-unknown', seconds=time.monotonic() - started)
        else:
            state['status'] = 'terminal-invalid'
        state['error_type'] = type(error).__name__
        save(state_path, state)
        if shared_claimed:
            shared_state_path = shared_directory / (input_hash + '.state.json')
            shared_state = read(shared_state_path)
            if shared_state.get('status') != 'succeeded':
                shared_state.update(status=state['status'], error_type=type(error).__name__)
                save(shared_state_path, shared_state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--stage', choices=['joint', 'text'], required=True)
    parser.add_argument('--max-new-jobs', type=int)
    parser.add_argument('--prefetch-clips', action='store_true',
                        help='Prepare one next CPU clip; embedding requests remain sequential')
    args = parser.parse_args()
    os.umask(0o077)
    run = args.run.resolve()
    protocol = read(run / 'protocol.json')
    if active_ten_second(protocol) is not None:
        contract_module().run_embedding(sys.modules[__name__], args, run, protocol)
        return
    assert protocol['max_inflight_embeddings'] == 1
    entries = protocol['joint_windows'] if args.stage == 'joint' else read(run / 'candidates.json')
    assert entries and len({e['candidate_id'] for e in entries}) == len(entries)
    assert not args.prefetch_clips or args.stage == 'joint'
    if args.stage == 'joint':
        assert len(entries) == 302 and entries[0]['start_ticks'] == 0
        assert entries[-1]['end_ticks'] == protocol['source']['runtimeTicks']
        assert all(b['start_ticks'] <= a['end_ticks'] for a, b in zip(entries, entries[1:]))
    lock = run / 'embedding.lock'
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    started = time.monotonic()
    completed = new_jobs = 0
    try:
        with closing(prepared_entries(run, entries, protocol['source'], args.prefetch_clips)) as jobs:
            while args.max_new_jobs is None or new_jobs < args.max_new_jobs:
                entry = next(jobs, None)
                if entry is None:
                    break
                fresh, state = dispatch(run, entry, protocol, args.stage)
                completed += 1
                new_jobs += int(fresh)
                if fresh:
                    print(json.dumps({'stage': args.stage, 'candidate': entry['candidate_id'],
                                      'completed': completed, 'total': len(entries), 'new_inputs': new_jobs,
                                      'seconds': round(state['seconds'], 3)}, ensure_ascii=False), flush=True)
                save(run / (args.stage + '-progress.json'), {'completed': completed, 'total': len(entries),
                    'new_inputs_this_process': new_jobs, 'elapsed_seconds': time.monotonic() - started,
                    'last_candidate_id': entry['candidate_id'], 'status': 'complete' if completed == len(entries) else 'running'})
        print(json.dumps({'stage': args.stage, 'completed': completed, 'total': len(entries),
                          'new_inputs': new_jobs, 'elapsed_seconds': time.monotonic() - started}), flush=True)
    finally:
        lock.unlink()


if __name__ == '__main__':
    main()
