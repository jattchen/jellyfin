#!/usr/bin/python3
"""Native macOS Jellyfin 12.2 with the EmbeddingVideoSearch plugin, migrated from Docker 10.11.

Paths stored by the Docker install (/media, /config) are kept byte-identical through
synthetic root links, because Jellyfin derives item ids from paths.
"""
import argparse
import datetime
import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
DATA_VOLUME = "/Volumes/Turbo"
ROOT = Path(DATA_VOLUME) / "Applications/JellyfinNative"
DATA = ROOT / "data"
CACHE = ROOT / "cache"
DOCKER_CONFIG = Path(DATA_VOLUME) / "Applications/Jellyfin/config"
MEDIA_TARGET = Path("/Volumes/Nas/影视库")

TOOLS = HOME / ".local/share/jellyfin-video-search-tools"
SERVER_ARCHIVE = TOOLS / "downloads/jellyfin_12.2-arm64.tar.xz"
SERVER_ARCHIVE_SHA256 = "f56a76b84471ac615ddf597f66d7445a9b1e90f3a8f76dedbe8fc30fbad732ca"
SERVER_DIR = ROOT / "server/12.2"
SERVER_EXE = SERVER_DIR / "jellyfin/jellyfin"

FFMPEG_REPO = "jellyfin/jellyfin-ffmpeg"
FFMPEG_TAG = "v8.1.3-1"
FFMPEG_ASSET = "jellyfin-ffmpeg_8.1.3-1_portable_macarm64-gpl.tar.xz"
FFMPEG_SHA256 = "22445d7299742749ad2eeb9ce87963d50def0357e45b3e6c7b69987c8365dbf6"
FFMPEG_DIR = ROOT / "ffmpeg/8.1.3-1"
FFMPEG_EXE = FFMPEG_DIR / "ffmpeg"

ACCEPTANCE = HOME / ".local/share/jellyfin-video-search-acceptance/20261014-9196a68f"
WEB_ID = "web-ae9762d97701"
WEB_SOURCE = ACCEPTANCE / "dual-web-20261016/full-source-20261016/build-snapshots" / WEB_ID
WEB_SHA256 = "ae9762d9770104097192e74d076d7a467d2d16544456a7b336492ab68252be0e"
WEB_DIR = ROOT / "web" / WEB_ID
PLUGIN_DLL = "Jellyfin.Plugin.EmbeddingVideoSearch.dll"
PLUGIN_SHA256 = "0732b935fb803938065c16af776d8923735d67d14c03e71a4b6fcf62d95fc003"
PLUGIN_SOURCE = ROOT / "plugin-releases" / PLUGIN_SHA256
PLUGIN_BUILD = Path(__file__).resolve().parents[2] / "plugins/EmbeddingVideoSearch/artifacts/plugin-with-sqlite"

LINKS = HOME / ".local/share/jellyfin-native/paths"
SYNTHETIC = Path("/etc/synthetic.d/jellyfin-native")
APFS_UTIL = "/System/Library/Filesystems/apfs.fs/Contents/Resources/apfs.util"

LABEL = "com.jattora.jellyfin-native"
NAME = "Jellyfin Server — Video Search"
APP = HOME / "Applications" / (NAME + ".app")
PLIST = HOME / "Library/LaunchAgents" / (LABEL + ".plist")
DOMAIN = "gui/" + str(os.getuid())
TARGET = DOMAIN + "/" + LABEL
LAUNCHER_SOURCE = Path(__file__).resolve().with_name("macos_launcher.c")
# Internal disk, so start-up blockers are visible even when Turbo is unavailable.
LAUNCHER_LOG = HOME / "Library/Logs/jellyfin-native/launcher.log"


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root):
    """Same pipeline that produced the recorded snapshot hashes."""
    pipeline = ("find . -type f ! -name .snapshot-sha256 -print0 | sort -z | xargs -0 shasum -a 256"
                " | shasum -a 256 | cut -d' ' -f1")
    return subprocess.run(["/bin/sh", "-c", pipeline], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def require_volume():
    if not os.path.ismount(DATA_VOLUME):
        raise SystemExit(DATA_VOLUME + " is not mounted")


def ctl(*args, check=True):
    result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)
    if args and args[0] == "bootstrap":
        for delay in (0.25, 0.5, 1, 2):
            if result.returncode != 5:
                break
            time.sleep(delay)
            result = subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError("launchctl failed: " + result.stderr.strip())
    return result


def loaded():
    return ctl("print", TARGET, check=False).returncode == 0


def configured_port():
    text = (DATA / "config/network.xml").read_text()
    return int(re.search(r"<InternalHttpPort>(\d+)</InternalHttpPort>", text).group(1))


def server_info(port, timeout=3):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/System/Info/Public", timeout=timeout) as r:
            return json.load(r)
    except (OSError, urllib.error.URLError, ValueError):
        return None


def extract(archive, destination):
    staging = Path(tempfile.mkdtemp(prefix=destination.name + ".", dir=destination.parent))
    subprocess.run(["/usr/bin/tar", "-xf", str(archive), "-C", str(staging)], check=True)
    staging.replace(destination)


# ---- stage -----------------------------------------------------------------

def stage():
    require_volume()
    for d in (SERVER_DIR.parent, FFMPEG_DIR.parent, WEB_DIR.parent, CACHE):
        d.mkdir(parents=True, exist_ok=True)

    if not SERVER_EXE.exists():
        if sha256(SERVER_ARCHIVE) != SERVER_ARCHIVE_SHA256:
            raise SystemExit("Server archive checksum mismatch")
        extract(SERVER_ARCHIVE, SERVER_DIR)

    if not FFMPEG_EXE.exists():
        downloads = ROOT / "downloads"
        downloads.mkdir(exist_ok=True)
        archive = downloads / FFMPEG_ASSET
        if not archive.exists() or sha256(archive) != FFMPEG_SHA256:
            subprocess.run(["gh", "release", "download", FFMPEG_TAG, "-R", FFMPEG_REPO, "-p", FFMPEG_ASSET,
                            "-D", str(downloads), "--clobber"], check=True)
        if sha256(archive) != FFMPEG_SHA256:
            raise SystemExit("jellyfin-ffmpeg checksum mismatch")
        extract(archive, FFMPEG_DIR)

    if not WEB_DIR.exists():
        if tree_sha256(WEB_SOURCE) != WEB_SHA256:
            raise SystemExit("Web snapshot checksum mismatch")
        shutil.copytree(WEB_SOURCE, WEB_DIR)

    if not PLUGIN_SOURCE.exists():
        if not (PLUGIN_BUILD / PLUGIN_DLL).is_file() or sha256(PLUGIN_BUILD / PLUGIN_DLL) != PLUGIN_SHA256:
            raise SystemExit("Build or restore the verified native HTTPS plugin artifact first")
        PLUGIN_SOURCE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PLUGIN_BUILD, PLUGIN_SOURCE)
    if sha256(PLUGIN_SOURCE / PLUGIN_DLL) != PLUGIN_SHA256:
        raise SystemExit("Plugin DLL checksum mismatch")

    version = subprocess.run([str(FFMPEG_EXE), "-hide_banner", "-version"], capture_output=True, text=True, check=True)
    print(json.dumps({"server": str(SERVER_EXE), "ffmpeg": version.stdout.splitlines()[0], "web": str(WEB_DIR)}, ensure_ascii=False))


# ---- migrate ---------------------------------------------------------------

def db_state(db):
    wal = db.with_name(db.name + "-wal")
    st = db.stat()
    return (st.st_size, st.st_mtime_ns, wal.stat().st_size if wal.exists() else 0)


def backup_docker_config(destination):
    """Copy the live Docker config without stopping it.

    SQLite locks do not cross the Docker Desktop file share, so the database is
    read as an immutable file and the copy is accepted only if the file and its
    WAL did not change while it was read.
    """
    db = DOCKER_CONFIG / "data/jellyfin.db"
    for attempt in range(5):
        before = db_state(db)
        if before[2]:
            time.sleep(5)
            continue
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(DOCKER_CONFIG, destination, symlinks=True,
                        ignore=lambda d, names: [n for n in names if Path(d) == db.parent and n.startswith("jellyfin.db")])
        source = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
        target = sqlite3.connect(destination / "data/jellyfin.db")
        source.backup(target)
        source.close()
        ok = target.execute("PRAGMA integrity_check").fetchone()[0]
        target.close()
        if db_state(db) == before and ok == "ok":
            return
        time.sleep(5)
    raise SystemExit("Docker database kept changing or failed integrity check; retry later")


def replace_once(path, pattern, replacement):
    text = path.read_text()
    new, count = re.subn(pattern, replacement, text)
    if count != 1:
        raise SystemExit(f"{path.name}: expected one match for {pattern}, found {count}")
    path.write_text(new)


def patch_config(port, data=DATA):
    config = data / "config"
    replace_once(config / "system.xml", r"<CachePath>[^<]*</CachePath>", f"<CachePath>{CACHE}</CachePath>")
    replace_once(config / "encoding.xml", r"<TranscodingTempPath>[^<]*</TranscodingTempPath>",
                 f"<TranscodingTempPath>{CACHE}/transcodes</TranscodingTempPath>")
    replace_once(config / "encoding.xml", r"<EncoderAppPathDisplay>[^<]*</EncoderAppPathDisplay>",
                 f"<EncoderAppPathDisplay>{FFMPEG_EXE}</EncoderAppPathDisplay>")
    network = config / "network.xml"
    replace_once(network, r"<InternalHttpPort>\d+</InternalHttpPort>", f"<InternalHttpPort>{port}</InternalHttpPort>")
    replace_once(network, r"<PublicHttpPort>\d+</PublicHttpPort>", f"<PublicHttpPort>{port}</PublicHttpPort>")
    replace_once(network, r"<AutoDiscovery>\w+</AutoDiscovery>", "<AutoDiscovery>false</AutoDiscovery>")
    replace_once(network, r"<LocalNetworkAddresses\s*/>|<LocalNetworkAddresses>.*?</LocalNetworkAddresses>",
                 "<LocalNetworkAddresses>\n    <string>127.0.0.1</string>\n  </LocalNetworkAddresses>")
    replace_once(network, r"<KnownProxies\s*/>|<KnownProxies>.*?</KnownProxies>",
                 "<KnownProxies>\n    <string>127.0.0.1</string>\n  </KnownProxies>")


def migrate(port):
    require_volume()
    if loaded() or server_info(port):
        raise SystemExit("Stop the native service before migrating")
    if not SERVER_EXE.exists() or not FFMPEG_EXE.exists():
        raise SystemExit("Run stage first")
    ts = stamp()
    backup = ROOT / "backups" / f"docker-config-{ts}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup_docker_config(backup)
    staging = Path(tempfile.mkdtemp(prefix="migrate-", dir=ROOT))
    replaced = None
    try:
        shutil.copytree(backup, staging, symlinks=True, dirs_exist_ok=True)
        patch_config(port, staging)
        plugin = staging / "plugins/EmbeddingVideoSearch"
        shutil.copytree(PLUGIN_SOURCE, plugin)
        if sha256(plugin / PLUGIN_DLL) != PLUGIN_SHA256:
            raise SystemExit("Installed plugin checksum mismatch")
        db = sqlite3.connect(staging / "data/jellyfin.db")
        try:
            counts = {
                "items": db.execute("SELECT count(*) FROM BaseItems").fetchone()[0],
                "users": [r[0] for r in db.execute("SELECT Username FROM Users ORDER BY Username")],
                "user_data": db.execute("SELECT count(*) FROM UserData").fetchone()[0],
            }
        finally:
            db.close()
        if DATA.exists():
            replaced = ROOT / "replaced" / f"data-{ts}"
            if replaced.exists():
                raise SystemExit("Data backup already exists")
            replaced.parent.mkdir(parents=True, exist_ok=True)
            DATA.replace(replaced)
        try:
            staging.replace(DATA)
        except BaseException:
            if replaced is not None:
                replaced.replace(DATA)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    record = {"created": ts, "source": str(DOCKER_CONFIG), "backup": str(backup), "port": port, "counts": counts}
    (backup.parent / f"docker-config-{ts}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False))


def install_index(prepared):
    """Install a verified offline copy; retain the replaced index for rollback."""
    require_volume()
    if loaded() or server_info(configured_port()):
        raise SystemExit("Stop the native service before installing the index")
    database = prepared / "index.db"
    evidence = json.loads((prepared / "rebind-evidence.json").read_text())
    if sha256(database) != evidence["indexSha256"]:
        raise SystemExit("Prepared index checksum mismatch")
    from rebind_index import stopped_database
    stopped_database(database)
    mapping = json.loads((prepared / "mapping.json").read_text())
    core = sqlite3.connect((DATA / "data/jellyfin.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        for film in mapping:
            row = core.execute("SELECT Id,RunTimeTicks FROM BaseItems WHERE Path=?", (film["sourcePath"],)).fetchone()
            if not row or row[0].replace("-", "").lower() != film["itemId"].replace("-", "").lower() or row[1] != film["runtimeTicks"]:
                raise SystemExit("The current migrated library differs from the verified mapping")
    finally:
        core.close()
    parent = DATA / "data/embedding-video-search"
    target = parent / "8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17"
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="prepared-", dir=parent))
    backup = None
    try:
        shutil.copy2(database, staging / "index.db")
        if sha256(staging / "index.db") != evidence["indexSha256"]:
            raise SystemExit("Copied index checksum mismatch")
        if target.exists():
            stopped_database(target / "index.db")
            backup = ROOT / "replaced" / ("index-" + stamp())
            if backup.exists():
                raise SystemExit("Index backup already exists")
            backup.parent.mkdir(parents=True, exist_ok=True)
            target.replace(backup)
        try:
            staging.replace(target)
        except BaseException:
            if backup is not None:
                backup.replace(target)
            raise
        print(json.dumps({"index": str(target), "backup": str(backup) if backup else None,
                          "candidates": evidence["candidateCounts"]}, ensure_ascii=False))
    finally:
        if staging.exists():
            shutil.rmtree(staging)


# ---- root links ------------------------------------------------------------

def links():
    LINKS.mkdir(parents=True, exist_ok=True)
    for name, target in (("media", MEDIA_TARGET), ("config", DATA)):
        link = LINKS / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise SystemExit(f"{link} is occupied")
        link.symlink_to(target)
    wanted = {"media": MEDIA_TARGET, "config": DATA}
    if all(os.path.realpath("/" + n) == os.path.realpath(t) for n, t in wanted.items()):
        print("Root links already active")
        return
    for name in wanted:
        root_entry = Path("/" + name)
        if root_entry.exists() and not root_entry.is_symlink():
            raise SystemExit(f"/{name} already exists and is not a link")
    manifest = "".join(f"{n}\t{LINKS / n}\n" for n in wanted)
    with tempfile.NamedTemporaryFile("w", delete=False, prefix="jellyfin-native-", suffix=".synthetic") as f:
        f.write(manifest)
    shell = (f"/bin/mkdir -p /etc/synthetic.d && /usr/bin/install -m 644 '{f.name}' '{SYNTHETIC}'"
             f" && ({APFS_UTIL} -t || true)")
    prompt = "Jellyfin 本机版需要在系统根目录创建 /media 和 /config 两个链接。"
    script = f'do shell script "{shell}" with prompt "{prompt}" with administrator privileges'
    subprocess.run(["/usr/bin/osascript", "-e", script], check=True)
    os.unlink(f.name)
    active = all(os.path.realpath("/" + n) == os.path.realpath(t) for n, t in wanted.items())
    print("Root links active" if active else "Manifest installed; restart macOS to activate the root links")


# ---- service ---------------------------------------------------------------

def wait_ready(port, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        info = server_info(port)
        if info:
            return info
        time.sleep(3)
    return None


def install():
    require_volume()
    if not (DATA / "config/system.xml").exists() or not WEB_DIR.exists():
        raise SystemExit("Run stage and migrate first")
    if os.path.realpath("/config") != os.path.realpath(DATA) or os.path.realpath("/media") != os.path.realpath(MEDIA_TARGET):
        raise SystemExit("Root links are not active; run links")
    if APP.exists() and plistlib.loads((APP / "Contents/Info.plist").read_bytes()).get("CFBundleIdentifier") != LABEL:
        raise SystemExit("Application path is occupied by another application")
    executable = APP / "Contents/MacOS" / NAME
    executable.parent.mkdir(parents=True, exist_ok=True)
    candidate = executable.with_name(NAME + ".new")
    defines = {"DATA_VOLUME": DATA_VOLUME, "SERVER_EXE": SERVER_EXE, "CACHE_DIR": CACHE, "WEB_DIR": WEB_DIR, "FFMPEG_EXE": FFMPEG_EXE}
    subprocess.run(["/usr/bin/xcrun", "clang", "-arch", "arm64", "-O2", "-Wall", "-Wextra", "-Werror",
                    *[f'-D{k}="{v}"' for k, v in defines.items()], str(LAUNCHER_SOURCE), "-o", str(candidate)], check=True)
    stop()
    candidate.replace(executable)
    info = {"CFBundleIdentifier": LABEL, "CFBundleName": NAME, "CFBundleDisplayName": NAME, "CFBundleExecutable": NAME,
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "CFBundleShortVersionString": "12.2", "LSUIElement": True,
            "NSRemovableVolumesUsageDescription": "从 Turbo 读取 Jellyfin 程序和数据，从 NAS 读取影视库。"}
    (APP / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", LABEL, str(APP)], check=True)
    data = {"Label": LABEL, "ProgramArguments": [str(executable)], "AssociatedBundleIdentifiers": [LABEL],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, "ProcessType": "Interactive",
            "StandardOutPath": "/dev/null", "StandardErrorPath": str(LAUNCHER_LOG)}
    LAUNCHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get("Label") != LABEL:
        raise SystemExit("LaunchAgent path is occupied")
    PLIST.write_bytes(plistlib.dumps(data))
    start()


def start():
    if not loaded():
        ctl("bootstrap", DOMAIN, str(PLIST))
    port = configured_port()
    # The first boot after migration runs the 10.11 -> 12.x database migrations.
    info = wait_ready(port, 1200)
    print(json.dumps({"port": port, "ready": bool(info), "version": info and info.get("Version")}))


def stop():
    if loaded():
        ctl("bootout", TARGET)
    deadline = time.monotonic() + 60
    while loaded():
        if time.monotonic() >= deadline:
            raise RuntimeError("launchd did not remove the service within 60 seconds")
        time.sleep(0.5)


def status():
    port = configured_port() if (DATA / "config/network.xml").exists() else None
    info = port and server_info(port)
    pid = re.search(r"\n\s*pid = (\d+)", ctl("print", TARGET, check=False).stdout)
    print(json.dumps({
        "label": LABEL, "loaded": loaded(), "pid": pid and int(pid.group(1)), "port": port,
        "version": info and info.get("Version"),
        "/config": os.path.realpath("/config"), "/media": os.path.realpath("/media"),
    }, ensure_ascii=False, indent=2))


def uninstall():
    stop()
    if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get("Label") == LABEL:
        PLIST.unlink()
    if APP.exists() and plistlib.loads((APP / "Contents/Info.plist").read_bytes()).get("CFBundleIdentifier") == LABEL:
        shutil.rmtree(APP)
    print(f"Removed login service; {ROOT} and the root links are retained")


def main():
    parser = argparse.ArgumentParser(description=NAME)
    parser.add_argument("command", choices=["stage", "migrate", "install-index", "links", "install", "start", "stop", "restart", "status", "uninstall"])
    parser.add_argument("--port", type=int, default=8097, help="HTTP port written by migrate")
    parser.add_argument("--prepared-index", type=Path, help="Verified offline rebind output for install-index")
    args = parser.parse_args()
    if args.command == "stage":
        stage()
    elif args.command == "migrate":
        migrate(args.port)
    elif args.command == "install-index":
        if args.prepared_index is None:
            parser.error("install-index requires --prepared-index")
        install_index(args.prepared_index)
    elif args.command == "links":
        links()
    elif args.command == "install":
        install()
    elif args.command == "start":
        start()
    elif args.command == "stop":
        stop()
        print("Stopped")
    elif args.command == "restart":
        stop()
        start()
    elif args.command == "status":
        status()
    elif args.command == "uninstall":
        uninstall()


if __name__ == "__main__":
    main()
