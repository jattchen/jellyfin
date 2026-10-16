#!/usr/bin/env python3
"""Build a NEW offline index copy from one completed film and cached reference queries."""
import argparse
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import struct
import uuid


spec = importlib.util.spec_from_file_location('film_embed', Path(__file__).with_name('full-film-embed.py'))
embedding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(embedding)
read, digest, sha = embedding.read, embedding.object_hash, embedding.sha
ALGORITHM = 'reference16-logistic-tail-f64-v1'


def native_segment_id(item_id, media_source_id, content_version, start, end, generation):
    text = f'{uuid.UUID(item_id).hex}|{media_source_id}|{content_version}|{start}|{end}|{generation}'
    raw = bytearray(hashlib.sha256(text.encode()).digest()[:16])
    raw[7] = (raw[7] & 15) | 80
    raw[8] = (raw[8] & 63) | 128
    return str(uuid.UUID(bytes_le=bytes(raw)))


def f32(values):
    assert len(values) == 768
    return list(struct.unpack('<768f', struct.pack('<768f', *values)))


def verified_vector(run, entry, protocol, channel):
    response = run / channel / (entry['candidate_id'] + '.json')
    state_path = response.with_suffix('.state.json')
    state = read(state_path)
    assert state['status'] == 'succeeded' and sha(response) == state['response_sha256']
    expected = protocol['signature']
    if channel == 'joint':
        clip_path = run / 'clips' / (entry['candidate_id'] + '.mp4')
        clip_state = read(clip_path.with_suffix('.clip.json'))
        assert sha(clip_path) == clip_state['sha256']
        expected_input = digest({'clip_sha256': clip_state['sha256'], 'signature': expected,
                                 'kind': 'joint', 'candidate': entry})
        identity = clip_state['identity']
        assert identity['source'] == embedding.source_facts(protocol['source'])
        assert identity['start_ticks'] == entry['start_ticks'] and identity['end_ticks'] == entry['end_ticks']
    else:
        expected_input = digest({'text': entry['text'], 'signature': expected, 'kind': 'document'})
    assert state['input_hash'] == expected_input, 'Embedding input provenance changed'
    return f32(embedding.validate_response(read(response), expected, channel))


def build_bundle(prior, source, new_candidates, references, query_version, signature):
    channel = prior['channel']
    # Replace only the trial film. Other films keep their own vectors, ranges and transcript.
    retained = [copy.deepcopy(c) for c in prior['candidates']
                if uuid.UUID(c['itemId']) != uuid.UUID(source['itemId'])]
    candidates = retained + new_candidates
    for row in candidates:
        row.pop('segmentId', None)
    assert len({row['anchorId'] for row in candidates}) == len(candidates), 'Duplicate interval in one representation'
    assert len({row['candidateId'] for row in candidates}) == len(candidates)
    matrix = [[math.fsum(q * d for q, d in zip(query, row['vector'])) for row in candidates]
              for query in references]
    background = [v for row in matrix for v in row]
    mean = math.fsum(background) / len(background)
    sigma = math.sqrt(math.fsum((v - mean) ** 2 for v in background) / len(background))
    assert sigma > 1e-6 and all(math.isfinite(v) for v in background)
    scale = sigma * 16 ** (-1 / 5) * math.sqrt(3) / math.pi
    content = digest(candidates)
    segmentation = digest([[c['anchorId'], c['startTicks'], c['endTicks']] for c in candidates])
    reference_version = digest({'signature': signature, 'content': content, 'segmentation': segmentation,
        'inputVersion': prior['inputVersion'], 'embeddingTask': prior['embeddingTask'],
        'queryVersion': query_version, 'background': background, 'scale': scale, 'algorithm': ALGORITHM})
    version = digest({'channel': channel, 'content': content, 'reference': reference_version})
    for row in candidates:
        row['segmentId'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'evs-candidate:' + version + ':' + row['candidateId']))
    bundle = {key: copy.deepcopy(value) for key, value in prior.items() if key != 'candidates'}
    bundle.update(version=version, contentVersion=content, segmentationVersion=segmentation,
                  referenceVersion=reference_version, referenceQueryVersion=query_version,
                  algorithm=ALGORITHM, scale=scale, background=background, candidates=candidates)
    assert len(background) == 16 * len(candidates)
    return bundle, matrix, retained


def candidate_row(entry, source, vector, text=None):
    start, end = entry['start_ticks'], entry['end_ticks']
    assert isinstance(start, int) and isinstance(end, int)
    assert 0 <= start < end <= source['runtimeTicks']
    source_id = str(uuid.UUID(source['sourceItemId']))
    return {'candidateId': entry['candidate_id'],
        'anchorId': f'{uuid.UUID(source_id).hex}:{start}:{end}',
        'itemId': str(uuid.UUID(source['itemId'])), 'mediaSourceId': source['mediaSourceId'],
        'libraryId': str(uuid.UUID(source['libraryId'])), 'contentVersion': source['contentVersion'],
        'startTicks': start, 'endTicks': end, 'runtimeTicks': source['runtimeTicks'],
        'canonical': {'sourceItemId': source_id, 'startTicks': start, 'endTicks': end},
        'transcript': text, 'vector': vector}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run, out = args.run.resolve(), args.output.resolve()
    assert not out.exists(), 'Choose a new output; never overwrite a prepared index'
    protocol = read(run / 'protocol.json')
    source, signature = protocol['source'], protocol['signature']
    embedding.source_facts(source)
    base = Path(protocol['prior_acceptance']).resolve()
    assert base.name == 'accepted-before-index.db' and base.is_file()
    original = sqlite3.connect(base.as_uri() + '?mode=ro&immutable=1', uri=True)
    old_representations = {row[0]: json.loads(row[1]) for row in original.execute(
        'SELECT channel,payload_json FROM candidate_representations WHERE active=1')}
    assert set(old_representations) == {'joint', 'text'}
    settings = dict(original.execute('SELECT key,value FROM settings'))
    assert settings['manual_pause'] == '1' and settings['imported_candidates_only'] == '1'
    sig = original.execute('SELECT generation,model,revision,processing_id,dimension,normalized,input_version FROM signatures WHERE active=1').fetchone()
    assert sig[1:6] == (signature['id'], signature['revision'], signature['processing_id'], 768, 1)
    assert sig[6] == old_representations['joint']['inputVersion']
    query_paths = sorted((args.references / 'responses').glob('*.json'))
    assert len(query_paths) == 16
    query_version = digest({p.name: sha(p) for p in query_paths})
    assert all(b['referenceQueryVersion'] == query_version for b in old_representations.values())
    references = []
    for path in query_paths:
        response = read(path)
        embedding.validate_signature(response, signature)
        assert response['data'][0]['processing'] == {'modalities': ['text'], 'task': 'query'}
        references.append(f32(response['data'][0]['embedding']))
    joint_entries, text_entries = protocol['joint_windows'], read(run / 'candidates.json')
    assert len(joint_entries) == 302 and joint_entries[0]['start_ticks'] == 0
    assert joint_entries[-1]['end_ticks'] == source['runtimeTicks']
    assert all(e['end_ticks'] - e['start_ticks'] == 60_000_000 for e in joint_entries)
    assert all(a['start_ticks'] < b['start_ticks'] <= a['end_ticks'] for a, b in zip(joint_entries, joint_entries[1:]))
    assert text_entries, 'A dialogue film must not silently lose its text representation'
    # Build everything before creating any output or mutating even the new copy.
    new_joint = [candidate_row(e, source, verified_vector(run, e, protocol, 'joint')) for e in joint_entries]
    new_text = [candidate_row(e, source, verified_vector(run, e, protocol, 'text'), e['text']) for e in text_entries]
    assert all(c['transcript'].strip() for c in new_text)
    bundles, matrices, retained = [], {}, {}
    for channel, rows in [('joint', new_joint), ('text', new_text)]:
        bundle, matrix, kept = build_bundle(old_representations[channel], source, rows,
                                            references, query_version, signature)
        bundles.append(bundle)
        matrices[channel], retained[channel] = matrix, kept
    item_id = str(uuid.UUID(source['itemId']))
    media_key = (item_id, source['mediaSourceId'], sig[0])
    media = original.execute('SELECT content_version,runtime_ticks,source_path,state FROM media WHERE item_id=? AND media_source_id=? AND generation=?', media_key).fetchone()
    assert media == (source['contentVersion'], source['runtimeTicks'], source['sourcePath'], 'partial')
    assert original.execute('SELECT count(*) FROM windows WHERE item_id=? AND media_source_id=? AND generation=?', media_key).fetchone()[0] == 0
    # Independently check the Python GUID encoding against all original native windows.
    for segment, item, media_source, generation, version, start, end in original.execute(
            'SELECT segment_id,item_id,media_source_id,generation,content_version,start_ticks,end_ticks FROM windows'):
        assert native_segment_id(item, media_source, version, start, end, generation) == segment
    out.mkdir(parents=True, mode=0o700)
    target = out / 'prepared-index.db'
    connection = sqlite3.connect(target)
    original.backup(connection)
    original.close()
    with connection:
        for bundle in bundles:
            connection.execute('UPDATE candidate_representations SET active=0 WHERE channel=?', (bundle['channel'],))
            connection.execute('INSERT INTO candidate_representations VALUES (?,?,?,1)',
                (bundle['channel'], bundle['version'], json.dumps(bundle, ensure_ascii=False)))
        for entry, row in zip(joint_entries, new_joint):
            segment = native_segment_id(item_id, source['mediaSourceId'], source['contentVersion'],
                                       entry['start_ticks'], entry['end_ticks'], sig[0])
            connection.execute('''INSERT INTO windows
                (segment_id,item_id,media_source_id,generation,content_version,start_ticks,end_ticks,state,vector,attempt)
                VALUES (?,?,?,?,?,?,?,'succeeded',?,1)''',
                (segment, item_id, source['mediaSourceId'], sig[0], source['contentVersion'],
                 entry['start_ticks'], entry['end_ticks'], struct.pack('<768f', *row['vector'])))
        connection.execute("UPDATE media SET state='complete' WHERE item_id=? AND media_source_id=? AND generation=?", media_key)
    assert connection.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert dict(connection.execute('SELECT key,value FROM settings')) == settings
    assert connection.execute("SELECT count(*) FROM windows WHERE item_id=? AND media_source_id=? AND generation=? AND state='succeeded'", media_key).fetchone()[0] == 302
    connection.close()
    embedding.save(out / 'candidate-bundles.json', bundles)
    embedding.save(out / 'reference-matrices.json', matrices)
    evidence = {'newJoint': len(new_joint), 'newText': len(new_text),
        'retainedOtherFilms': {key: len(value) for key, value in retained.items()},
        'activeCandidates': {b['channel']: len(b['candidates']) for b in bundles},
        'representations': {b['channel']: b['version'] for b in bundles},
        'referenceQueryVersion': query_version, 'algorithm': ALGORITHM,
        'referenceQueryNewInputs': 0, 'all302WindowsCovered': True,
        'baseIndexSha256': sha(base), 'preparedIndexSha256': sha(target),
        'modelSignature': signature, 'sourceContentVersion': source['contentVersion'],
        'newModelInputsDuringImport': 0, 'target': str(target)}
    embedding.save(out / 'import-evidence.json', evidence)
    print(json.dumps({k: v for k, v in evidence.items() if k not in {'modelSignature', 'representations'}}, indent=2))


if __name__ == '__main__':
    main()
