#!/usr/bin/python3
"""Copy the stopped three-film index onto identical files in a migrated library.

Only media bindings change. Vectors, canonical provenance, transcripts and ranking
curves are retained. The historical index and the running host are never written.
"""
import argparse
import hashlib
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def stopped_database(path):
    if not path.is_file():
        raise ValueError('Index not found')
    holders = subprocess.run(['lsof', '-t', str(path)], capture_output=True, check=False)
    wal = path.with_name(path.name + '-wal')
    if holders.stdout.strip() or (wal.exists() and wal.stat().st_size):
        raise ValueError('Source must be stopped, with no pending WAL')


def native_id(item, source, version, start, end, generation):
    raw = bytearray(hashlib.sha256(
        f'{uuid.UUID(item).hex}|{source}|{version}|{start}|{end}|{generation}'.encode()).digest()[:16])
    raw[7] = (raw[7] & 15) | 80
    raw[8] = (raw[8] & 63) | 128
    return str(uuid.UUID(bytes_le=bytes(raw)))


def prepare(source, mapping, output):
    stopped_database(source)
    if output.exists():
        raise ValueError('Output already exists; retain prior evidence and choose a new directory')
    before = digest_file(source)
    original = sqlite3.connect(source.as_uri() + '?mode=ro&immutable=1', uri=True)
    original.row_factory = sqlite3.Row
    try:
        if original.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Source failed integrity check')
        settings = dict(original.execute('SELECT key,value FROM settings'))
        if settings.get('manual_pause') != '1' or settings.get('imported_candidates_only') != '1':
            raise ValueError('Source must remain paused and imported-candidates-only')
        rows = list(original.execute('SELECT * FROM media'))
        films = {uuid.UUID(row['sourceItemId']): row for row in mapping}
        if len(mapping) != 3 or len(films) != 3 or len(rows) != 3:
            raise ValueError('Expected exactly three unique films')
        if {uuid.UUID(row['item_id']) for row in rows} != set(films):
            raise ValueError('Mapping does not cover the source media')
        if len({uuid.UUID(row['itemId']) for row in mapping}) != 3:
            raise ValueError('Target item identities must be distinct')
        media_fields = ('contentVersion', 'runtimeTicks', 'audioStreamIndex')
        sql_fields = ('content_version', 'runtime_ticks', 'audio_stream_index')
        for old in rows:
            film = films[uuid.UUID(old['item_id'])]
            path = Path(film['sourcePath'])
            if not path.samefile(old['source_path']):
                raise ValueError('Mapped source is not the exact indexed file')
            stat = path.stat()
            if stat.st_size != film['sizeBytes'] or 621355968000000000 + stat.st_mtime_ns // 100 != film['modifiedTicks']:
                raise ValueError('Media file changed since source-facts inspection')
            if any(film[field] != old[column] for field, column in zip(media_fields, sql_fields)):
                raise ValueError('Indexed content facts differ; refusing to relabel old vectors')
            if old['state'] != 'complete' or film.get('presence') != 'Readable':
                raise ValueError('Both the indexed and target media must be complete and readable')

        # Use the already-reviewed binding/version implementation, without its old
        # 42/25 pilot-count assumptions or its partial-media installation path.
        helper_path = Path(__file__).resolve().parents[2] / 'plugins/EmbeddingVideoSearch/scripts/rebind-full-source.py'
        spec = importlib.util.spec_from_file_location('rebind_full_source', helper_path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        bundles = [json.loads(row[0]) for row in original.execute(
            'SELECT payload_json FROM candidate_representations WHERE active=1 ORDER BY channel')]
        counts = {b['channel']: len(b['candidates']) for b in bundles}
        if counts != {'joint': 510, 'text': 481}:
            raise ValueError('Expected the accepted 510 joint and 481 text candidates')
        old_ids = {c['segmentId'] for b in bundles for c in b['candidates']}
        rebuilt = [helper.build_bundle(b, films, old_ids)[0] for b in bundles]
        # This migration changes bindings, never the ranking population or curves.
        binding_fields = {'itemId', 'mediaSourceId', 'libraryId', 'segmentId'}
        for old, new in zip(bundles, rebuilt):
            for key in ('background', 'scale', 'referenceVersion', 'referenceQueryVersion', 'algorithm'):
                if old[key] != new[key]:
                    raise ValueError('Ranking provenance changed')
            for a, b in zip(old['candidates'], new['candidates']):
                if {k: v for k, v in a.items() if k not in binding_fields} != {k: v for k, v in b.items() if k not in binding_fields}:
                    raise ValueError('Candidate evidence changed beyond media bindings')

        output.mkdir(parents=True, mode=0o700)
        database = output / 'index.db'
        target = sqlite3.connect(database)
        try:
            original.backup(target)
            with target:
                target.execute('UPDATE candidate_representations SET active=0')
                for b in rebuilt:
                    target.execute('INSERT INTO candidate_representations VALUES (?,?,?,1)',
                                   (b['channel'], b['version'], json.dumps(b, ensure_ascii=False)))
                target.execute('DELETE FROM libraries')
                for film in mapping:
                    target.execute('INSERT OR IGNORE INTO libraries VALUES (?)', (str(uuid.UUID(film['libraryId'])),))
                for old in rows:
                    film = films[uuid.UUID(old['item_id'])]
                    target.execute('UPDATE media SET item_id=?,media_source_id=?,library_id=?,source_path=? WHERE item_id=?',
                                   (film['itemId'], film['mediaSourceId'], film['libraryId'], film['sourcePath'], old['item_id']))
                    windows = list(target.execute('SELECT segment_id,start_ticks,end_ticks,generation,content_version FROM windows WHERE item_id=?', (old['item_id'],)))
                    for segment, start, end, generation, version in windows:
                        target.execute('UPDATE windows SET segment_id=?,item_id=?,media_source_id=? WHERE segment_id=?',
                                       (native_id(film['itemId'], film['mediaSourceId'], version, start, end, generation),
                                        film['itemId'], film['mediaSourceId'], segment))
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Rebound copy failed integrity check')
            if target.execute('SELECT count(*) FROM windows').fetchone()[0] != 510:
                raise ValueError('Native window count changed')
            if dict(target.execute('SELECT key,value FROM settings')) != settings:
                raise ValueError('Index settings changed')
            target.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            target.execute('PRAGMA journal_mode=DELETE')
        finally:
            target.close()
        if digest_file(source) != before:
            raise ValueError('Source index changed during migration')
        record = {'sourceSha256': before, 'indexSha256': digest_file(database), 'candidateCounts': counts,
                  'nativeWindows': 510, 'mediaComplete': 3, 'newEmbeddingRequests': 0,
                  'vectorsTranscriptsCanonicalAndRankingUnchanged': True,
                  'representations': {b['channel']: b['version'] for b in rebuilt}}
        (output / 'rebind-evidence.json').write_text(json.dumps(record, indent=2))
        (output / 'mapping.json').write_text(json.dumps(mapping, ensure_ascii=False, indent=2))
        return record
    except BaseException:
        # Only remove the new staging directory created by this invocation.
        if output.exists():
            shutil.rmtree(output)
        raise
    finally:
        original.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--mapping', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.source.resolve(), json.loads(args.mapping.read_text()), args.output.resolve()), indent=2))
