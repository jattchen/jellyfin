#!/usr/bin/env python3
"""Import existing pilot representations into a stopped acceptance COPY. No network or inference."""
import argparse
import hashlib
import json
import math
import pathlib
import sqlite3
import struct
import subprocess
import uuid


def read(path):
    return json.loads(path.read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def business_hash(connection):
    result = {}
    for table in ['settings', 'libraries', 'signatures', 'media', 'windows', 'events']:
        rows = connection.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()
        result[table] = digest([[v.hex() if isinstance(v, bytes) else v for v in row] for row in rows])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--acceptance-root', type=pathlib.Path, required=True)
    parser.add_argument('--pilot-root', type=pathlib.Path, required=True)
    parser.add_argument('--text-root', type=pathlib.Path, required=True)
    parser.add_argument('--mapping', type=pathlib.Path, required=True)
    parser.add_argument('--index', type=pathlib.Path, required=True)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    root, pilot, target = args.acceptance_root.resolve(), args.pilot_root.resolve(), args.index.resolve()
    original = root / 'stock-12.2/data/data/embedding-video-search/8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17/index.db'
    assert target != original.resolve() and target.is_file(), 'Refusing original or missing index'
    # This is an offline import: never compete with the host or its SQLite writer.
    assert not subprocess.run(['lsof', '-t', str(target)], capture_output=True).stdout.strip(), 'Stop the copied host first'
    connection = sqlite3.connect(target)
    before = business_hash(connection)
    assert dict(connection.execute('SELECT key,value FROM settings'))['manual_pause'] == '1'
    signature = connection.execute('SELECT model,revision,processing_id,dimension,normalized FROM signatures WHERE active=1').fetchone()
    vectors = read(pilot / 'reference16/candidate-vectors.json')
    sig = vectors['signature']
    assert signature == (sig['model'], sig['revision'], sig['processing_id'], sig['dimension'], int(sig['normalized']))
    assert signature[:2] == ('google/embeddinggemma-2', '914f7f89142e33e77833254d9c9b90c3cef7303b')
    assert signature[3:] == (768, 1)
    joint_input = connection.execute('SELECT input_version FROM signatures WHERE active=1').fetchone()[0]
    mapping = {row['case_id']: row for row in read(args.mapping)}
    plan = {row['case_id']: row for row in read(root / 'bounded-pilot/clip-plan.json')['entries']}
    assert len(mapping) == len(plan) == 14 and mapping.keys() == plan.keys()
    text = {row['document_id']: row for row in read(args.text_root / 'candidates.json')}
    matrices = read(pilot / 'reference16/reference-cosine-matrices.json')
    transforms = read(pilot / 'reference16/reference-transforms.json')['channels']
    query_version = digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((pilot / 'reference16/responses').glob('*.json'))})
    bundles = []
    for channel, count in [('joint', 42), ('text', 25)]:
        observations = [o for o in vectors['observations'] if o['channel'] == channel]
        assert len(observations) == count
        matrix = matrices['matrices'][channel]
        assert len(matrix) == 16 and all(len(row) == count for row in matrix)
        background = [float(x) for row in matrix for x in row]
        assert all(math.isfinite(x) for x in background)
        mean = math.fsum(background) / len(background)
        sigma = math.sqrt(math.fsum((x - mean) ** 2 for x in background) / len(background))
        scale = sigma * 16 ** (-1 / 5) * math.sqrt(3) / math.pi
        assert sigma > 1e-6 and abs(scale - transforms[channel]['scale']) < 1e-15
        candidates = []
        for observation in observations:
            case = observation['case_id']; mapped, source = mapping[case], plan[case]
            assert mapped['canonical_item_id'] == observation['source_item_id'] == source['source_item_id']
            assert mapped['canonical_offset_seconds'] == source['source_start_seconds']
            assert pathlib.Path(mapped['path']).resolve() == pathlib.Path(source['output_path']).resolve()
            assert pathlib.Path(mapped['path']).is_file()
            start, end = (round(observation[k] * 10_000_000) for k in ['local_start_seconds', 'local_end_seconds'])
            offset = round(source['source_start_seconds'] * 10_000_000)
            assert 0 <= start < end <= mapped['runtime_ticks'] == 120_000_000
            assert observation['start_ticks'] == offset + start and observation['end_ticks'] == offset + end
            assert observation['anchor_id'] == f"{observation['source_item_id']}:{offset + start}:{offset + end}"
            item_id = str(uuid.UUID(mapped['item_id'])); media_source = mapped['media_source_id']
            rows = connection.execute('SELECT library_id,content_version,runtime_ticks,source_path FROM media WHERE item_id=? AND media_source_id=? AND generation=(SELECT generation FROM signatures WHERE active=1) AND state=\'complete\'', (item_id, media_source)).fetchall()
            assert len(rows) == 1
            library, content_version, runtime, path = rows[0]
            assert runtime == mapped['runtime_ticks'] and path == mapped['path']
            vector = observation['vector']
            assert len(vector) == 768 and all(math.isfinite(x) for x in vector)
            assert abs(math.fsum(x*x for x in vector) - 1) < 0.02
            transcript = None
            if channel == 'joint':
                stored = connection.execute('SELECT vector FROM windows WHERE item_id=? AND media_source_id=? AND content_version=? AND start_ticks=? AND end_ticks=? AND state=\'succeeded\'', (item_id, media_source, content_version, start, end)).fetchall()
                assert len(stored) == 1 and stored[0][0] == struct.pack('<768f', *vector), 'Joint vector differs from original successful window'
            else:
                original_text = text[observation['candidate_id']]
                assert original_text['case_id'] == case
                assert round(original_text['playback_start_in_clip'] * 10_000_000) == start
                assert round(original_text['playback_end_in_clip'] * 10_000_000) == end
                response = read(args.text_root / 'responses' / (observation['candidate_id'] + '.json'))
                assert response['model'] == sig['model'] and response['revision'] == sig['revision'] and response['processing_id'] == sig['processing_id']
                assert response['data'][0]['processing'] == {'modalities': ['text'], 'task': 'document'}
                assert response['data'][0]['embedding'] == vector
                transcript = original_text['text']
            candidates.append(dict(candidateId=observation['candidate_id'], anchorId=observation['anchor_id'], itemId=item_id,
                mediaSourceId=media_source, libraryId=library, contentVersion=content_version, startTicks=start, endTicks=end,
                runtimeTicks=runtime, canonical=dict(sourceItemId=str(uuid.UUID(observation['source_item_id'])),
                    startTicks=observation['start_ticks'], endTicks=observation['end_ticks']), transcript=transcript, vector=vector))
        content_version = digest(candidates)
        segmentation = digest([[c['anchorId'], c['startTicks'], c['endTicks']] for c in candidates])
        input_version = joint_input if channel == 'joint' else 'transcript-document-cache-v1'
        embedding_task = 'video_audio' if channel == 'joint' else 'document'
        reference_version = digest(dict(model=sig, content=content_version, segmentation=segmentation,
            inputVersion=input_version, embeddingTask=embedding_task, queryVersion=query_version,
            background=background, scale=scale, algorithm='reference16-logistic-tail-f64-v1'))
        version = digest(dict(channel=channel, content=content_version, reference=reference_version))
        for candidate in candidates:
            candidate['segmentId'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'evs-candidate:' + version + ':' + candidate['candidateId']))
        bundles.append(dict(channel=channel, version=version, model=sig['model'], revision=sig['revision'],
            processingId=sig['processing_id'], dimension=768, normalized=True, contentVersion=content_version,
            inputVersion=input_version, embeddingTask=embedding_task,
            segmentationVersion=segmentation, referenceVersion=reference_version, referenceQueryVersion=query_version,
            algorithm='reference16-logistic-tail-f64-v1', scale=scale, background=background, candidates=candidates))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'candidate-bundles.json').write_text(json.dumps(bundles, ensure_ascii=False))
    if args.execute:
        backup = args.output / 'pre-import-index.db'
        assert not backup.exists(), 'Keep the previous import backup; choose another output for a second import'
        with sqlite3.connect(backup) as destination:
            connection.backup(destination)
        with connection:
            connection.execute('CREATE TABLE IF NOT EXISTS candidate_representations (channel TEXT NOT NULL,version TEXT NOT NULL,payload_json TEXT NOT NULL,active INTEGER NOT NULL,PRIMARY KEY(channel,version))')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS active_candidate_representation ON candidate_representations(channel) WHERE active=1')
            for bundle in bundles:
                connection.execute('UPDATE candidate_representations SET active=0 WHERE channel=?', (bundle['channel'],))
                connection.execute('INSERT OR REPLACE INTO candidate_representations VALUES (?,?,?,1)', (bundle['channel'], bundle['version'], json.dumps(bundle, ensure_ascii=False)))
    after = business_hash(connection)
    assert before == after, 'Original joint/settings/media business rows changed'
    summary = dict(executed=args.execute, candidateCounts={b['channel']:len(b['candidates']) for b in bundles},
        originalBusinessHashes=before, originalBusinessRowsUnchanged=True, representations={b['channel']:b['version'] for b in bundles},
        newModelInputs=0, mappingCount=len(mapping), rankingVersion='reference16-logistic-tail-f64-v1')
    (args.output / 'import-evidence.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
