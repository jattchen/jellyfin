#!/usr/bin/python3
"""Supervise the accepted 10-second query runtime; leave shared 18780 unchanged."""
import argparse
import json
import os
import plistlib
import shutil
import signal
import subprocess
import time
import urllib.request
from pathlib import Path

import jellyfin_native as native

LABEL = 'com.jattora.jellyfin-query-model'
NAME = 'Jellyfin Search Model'
APP = native.HOME / 'Applications' / (NAME + '.app')
PLIST = native.HOME / 'Library/LaunchAgents' / (LABEL + '.plist')
TARGET = native.DOMAIN + '/' + LABEL
ROOT = native.ROOT / 'query-service'
SOURCE = ROOT / 'source'
RUNTIME = ROOT / 'runtime'
PYTHON = Path('/Volumes/Turbo/Library/embedding-service/.venv/bin/python')
ACCEPTANCE = native.ACCEPTANCE / 'three-film-10s-20261017'
PROCESSING_ID = 'eg2-0b01d27c8c20b7d920e5'
LOG = native.HOME / 'Library/Logs/jellyfin-native/query-model.log'


def info():
    try:
        with urllib.request.urlopen('http://127.0.0.1:18783/v1/models', timeout=3) as response:
            return json.load(response)['data'][0]
    except (OSError, ValueError, KeyError):
        return None


def verify(model):
    if not model or model.get('processing_id') != PROCESSING_ID or model.get('revision') != '914f7f89142e33e77833254d9c9b90c3cef7303b' or model.get('dimension') != 768 or model.get('normalized') is not True or not model.get('ready'):
        raise SystemExit('18783 does not have the accepted model identity')


def stop():
    if native.ctl('print', TARGET, check=False).returncode == 0:
        native.ctl('bootout', TARGET)
    deadline = time.monotonic() + 30
    while info() is not None:
        if time.monotonic() >= deadline:
            raise SystemExit('Model listener has not stopped')
        time.sleep(0.5)


def install():
    native.require_volume()
    # Only adopt the exact known manual acceptance process. Never kill an unknown listener.
    old = info()
    if old:
        verify(old)
        command = subprocess.run(['ps', '-o', 'args=', '-p', str(old['pid'])], capture_output=True, text=True, check=True).stdout.strip()
        expected = f'{PYTHON} {ACCEPTANCE / "serve-model-with-query-evidence.py"} serve'
        if native.ctl('print', TARGET, check=False).returncode != 0:
            if command != expected:
                raise SystemExit('18783 is owned by another process; refusing adoption')
            with urllib.request.urlopen('http://127.0.0.1:18783/readyz', timeout=3) as response:
                health = json.load(response)
            if health.get('admitted_requests') != 0:
                raise SystemExit('Wait for existing model requests before adoption')
    freeze = json.loads((ACCEPTANCE / 'model-source-freeze.json').read_text())
    for name, wanted in freeze['files'].items():
        if native.sha256(ACCEPTANCE / 'code-snapshots/model-service/src/embedding_service' / name) != wanted:
            raise SystemExit('Frozen model source checksum mismatch: ' + name)
    ROOT.mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        shutil.copytree(ACCEPTANCE / 'code-snapshots/model-service/src', SOURCE)
    for name, wanted in freeze['files'].items():
        if native.sha256(SOURCE / 'embedding_service' / name) != wanted:
            raise SystemExit('Installed model source checksum mismatch: ' + name)
    script = ROOT / 'serve-with-query-evidence.py'
    wrapper = ACCEPTANCE / 'serve-model-with-query-evidence.py'
    if native.sha256(wrapper) != '55f538d8202ebc2e43f16aa1b85eb31ef0c48bbb4260b438ef9450a25627eea2':
        raise SystemExit('Frozen query wrapper checksum mismatch')
    shutil.copy2(wrapper, script)
    config = RUNTIME / 'config'
    config.mkdir(parents=True, exist_ok=True)
    runtime = json.loads(Path('/Volumes/Turbo/Library/embedding-service-jellyfin-10s/config/runtime.json').read_text())
    if runtime.get('port') != 18783 or runtime.get('video_profile') != 'jellyfin-10s-5f':
        raise SystemExit('Unexpected query runtime configuration')
    (config / 'runtime.json').write_text(json.dumps(runtime, indent=2))
    (config / 'installed-release.json').write_text(json.dumps({
        'mode': 'jellyfin-native-three-film-search', 'automaticStartup': True,
        'sourceFiles': freeze['files'], 'processingId': PROCESSING_ID}, indent=2))
    if APP.exists() and plistlib.loads((APP / 'Contents/Info.plist').read_bytes()).get('CFBundleIdentifier') != LABEL:
        raise SystemExit('Application path occupied')
    executable = APP / 'Contents/MacOS' / NAME
    executable.parent.mkdir(parents=True, exist_ok=True)
    candidate = executable.with_name(NAME + '.new')
    defines = {'QUERY_PYTHON': PYTHON, 'QUERY_SCRIPT': script, 'QUERY_HOME': RUNTIME, 'QUERY_SOURCE': SOURCE}
    subprocess.run(['/usr/bin/xcrun', 'clang', '-arch', 'arm64', '-O2', '-Wall', '-Wextra', '-Werror',
                    *[f'-D{k}="{v}"' for k, v in defines.items()], str(Path(__file__).with_name('query_launcher.c')),
                    '-o', str(candidate)], check=True)
    if old and native.ctl('print', TARGET, check=False).returncode != 0:
        os.kill(old['pid'], signal.SIGTERM)
    stop()
    candidate.replace(executable)
    (APP / 'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier': LABEL, 'CFBundleName': NAME, 'CFBundleDisplayName': NAME,
        'CFBundleExecutable': NAME, 'CFBundlePackageType': 'APPL', 'CFBundleVersion': '1',
        'LSUIElement': True, 'NSRemovableVolumesUsageDescription': 'Jellyfin 三集搜索模型从 Turbo 读取固定模型与配置。'}))
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', LABEL, str(APP)], check=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get('Label') != LABEL:
        raise SystemExit('LaunchAgent path occupied')
    PLIST.write_bytes(plistlib.dumps({'Label': LABEL, 'ProgramArguments': [str(executable)],
        'AssociatedBundleIdentifiers': [LABEL], 'RunAtLoad': True, 'KeepAlive': True,
        'ThrottleInterval': 30, 'ProcessType': 'Interactive', 'StandardOutPath': str(LOG), 'StandardErrorPath': str(LOG)}))
    native.ctl('bootstrap', native.DOMAIN, str(PLIST))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        model = info()
        if model:
            verify(model)
            print(json.dumps({'loaded': True, 'pid': model['pid'], 'processingId': model['processing_id'], 'port': 18783}))
            return
        time.sleep(2)
    raise SystemExit('Query model not ready; inspect ' + str(LOG))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['install', 'status', 'stop'])
    args = parser.parse_args()
    if args.command == 'install':
        install()
    elif args.command == 'stop':
        stop()
    else:
        model = info()
        print(json.dumps({'loaded': native.ctl('print', TARGET, check=False).returncode == 0,
                          'ready': bool(model and model.get('ready')), 'pid': model and model.get('pid'),
                          'processingId': model and model.get('processing_id'), 'port': 18783}))
