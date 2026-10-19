#!/usr/bin/env python3
"""Load the plugin into a temporary official Jellyfin 12.2 process and stop it."""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

JELLYFIN = Path("/Users/mac/.local/share/jellyfin-video-search-tools/stock-server/12.2/jellyfin/jellyfin")
ROOT = Path("/tmp/evs-plugin-load-20261014")
MARKER = "evs-min-loop-load"
ACCEPTANCE_MARK = "jellyfin-video-search-acceptance"
PORT = 18773
PLUGIN_ID = "8f4c2a91-6d3e-4b7a-9c15-2e8d0f6a4b17"
PASSWORD = "load-test-only"
VIEWER_PASSWORD = "viewer-test-only"


class Stub(BaseHTTPRequestHandler):
    processing_id = "proc-load-a"

    def do_GET(self):
        if self.path != "/v1/models":
            self.send_error(404)
            return
        self._json({"data": [self._model()]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if self.path != "/v1/embeddings":
            self.send_error(404)
            return
        vector = [0.0] * 768
        vector[0] = 1.0
        self._json({
            "model": "google/embeddinggemma-2",
            "revision": "914f7f89142e33e77833254d9c9b90c3cef7303b",
            "processing_id": Stub.processing_id,
            "dimension": 768,
            "normalized": True,
            "data": [{"index": 0, "embedding": vector}],
        })

    def _model(self):
        return {
            "id": "google/embeddinggemma-2",
            "revision": "914f7f89142e33e77833254d9c9b90c3cef7303b",
            "processing_id": Stub.processing_id,
            "dimension": 768,
            "normalized": True,
            "ready": True,
        }

    def _json(self, payload):
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        sys.stderr.write("stub " + (fmt % args) + "\n")


def request(method, url, payload=None, token=None, anonymous=False, legacy_token=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if legacy_token is not None:
        headers["X-Emby-Token"] = legacy_token
    elif not anonymous:
        auth = (
            'MediaBrowser Client="EmbeddingVideoSearch-LoadTest", '
            'Device="load-test", DeviceId="evs-load-test", Version="12.2.0"'
        )
        if token:
            auth += f', Token="{token}"'
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            body = response.read().decode()
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def wait_ready(base, proc, log_path):
    deadline = time.time() + 90
    while time.time() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"jellyfin exited {proc.returncode}\n{log_path.read_text()[-4000:]}")
        text = log_path.read_text(errors="replace") if log_path.exists() else ""
        if "Startup complete" not in text:
            time.sleep(0.5)
            continue
        try:
            status, body = request("GET", base + "/System/Info/Public", anonymous=True)
        except Exception:
            status, body = 0, ""
        if status == 200 and "12.2" in body:
            return body
        time.sleep(0.5)
    raise SystemExit("server did not become ready\n" + log_path.read_text()[-4000:])


def start_server(plugin_dir):
    reset_root()
    data = ROOT / "data"
    config = ROOT / "config"
    plugin_dest = data / "plugins" / "EmbeddingVideoSearch"
    plugin_dest.mkdir(parents=True)
    config.mkdir()
    (ROOT / "cache").mkdir()
    (ROOT / "log").mkdir()
    for item in plugin_dir.iterdir():
        shutil.copy2(item, plugin_dest / item.name)
    (config / "network.xml").write_text(f"""<?xml version="1.0" encoding="utf-8"?>
<NetworkConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <InternalHttpPort>{PORT}</InternalHttpPort>
  <PublicHttpPort>{PORT}</PublicHttpPort>
  <InternalHttpsPort>8921</InternalHttpsPort>
  <PublicHttpsPort>8921</PublicHttpsPort>
  <AutoDiscovery>false</AutoDiscovery>
  <EnableHttps>false</EnableHttps>
  <EnableIPv6>false</EnableIPv6>
  <EnableRemoteAccess>false</EnableRemoteAccess>
</NetworkConfiguration>
""")
    log_path = ROOT / "server.log"
    log = log_path.open("w")
    env = os.environ.copy()
    env.pop("DOTNET_ROOT", None)
    env.pop("DOTNET_MULTILEVEL_LOOKUP", None)
    proc = subprocess.Popen(
        [str(JELLYFIN), "--datadir", str(data), "--configdir", str(config),
         "--cachedir", str(ROOT / "cache"), "--logdir", str(ROOT / "log"), "--nowebclient"],
        stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(JELLYFIN.parent)
    )
    return proc, log_path


def stop(proc):
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def assert_safe_root():
    root = ROOT.resolve()
    if root.parent not in {Path("/tmp"), Path("/private/tmp")} or not root.name.startswith("evs-plugin-load-"):
        raise SystemExit(f"refusing unexpected load directory {root}")
    if ACCEPTANCE_MARK in str(root):
        raise SystemExit("refusing to touch the acceptance instance")
    return root


def stop_our_jellyfin():
    """Stop only a Jellyfin whose command line uses this temporary datadir."""
    root = assert_safe_root()
    markers = {str(ROOT / "data"), str(root / "data")}
    listing = subprocess.run(["ps", "ax", "-o", "pid=,command="], check=True, text=True, capture_output=True).stdout
    for line in listing.splitlines():
        if "jellyfin" not in line or ACCEPTANCE_MARK in line or not any(marker in line for marker in markers):
            continue
        pid = int(line.split(None, 1)[0])
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.25)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def reset_root():
    root = assert_safe_root()
    if root.exists():
        marker = root / ".evs-load-marker"
        log = root / "server.log"
        log_text = log.read_text(errors="replace") if log.is_file() else ""
        owned = marker.is_file() and marker.read_text() == MARKER
        if not owned and any(str(path) in log_text and "datadir" in log_text for path in (ROOT / "data", root / "data")):
            owned = True
        if not owned:
            raise SystemExit(f"refusing to delete unverified directory {root}")
        stop_our_jellyfin()
        time.sleep(1)
        shutil.rmtree(root)
    root.mkdir()
    (root / ".evs-load-marker").write_text(MARKER)


def data_path_from_log(log_path):
    """The first storage check in this Jellyfin build is IApplicationPaths.DataPath."""
    text = log_path.read_text(errors="replace")
    paths = []
    token = "Storage path `"
    cursor = 0
    while True:
        start = text.find(token, cursor)
        if start < 0:
            break
        start += len(token)
        end = text.find("`", start)
        paths.append(Path(text[start:end]))
        cursor = end + 1
    if not paths:
        raise SystemExit("server log did not record a DataPath storage check")
    data_path = paths[0]
    if not (data_path / "jellyfin.db").is_file():
        raise SystemExit(f"{data_path} is not the host DataPath; jellyfin.db was not there")
    program_data = (assert_safe_root() / "data").resolve()
    if data_path.resolve() != (program_data / "data").resolve():
        raise SystemExit(f"DataPath {data_path} is not {{datadir}}/data")
    return data_path


def index_db(log_path):
    data_path = data_path_from_log(log_path)
    matches = list(data_path.glob(f"embedding-video-search/{PLUGIN_ID}/index.db"))
    if len(matches) != 1:
        raise SystemExit(f"expected one plugin index under DataPath, found {matches}")
    return matches[0]


def seed_window(log_path):
    import struct
    db = index_db(log_path)
    blob = struct.pack("<768f", *([1.0] + [0.0] * 767))
    blob = struct.pack("<768f", *([1.0] + [0.0] * 767))
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO media (item_id, media_source_id, generation, library_id, content_version, runtime_ticks, state, source_path) VALUES (?,?,?,?,?,?,?,?)",
        ("22222222-2222-2222-2222-222222222222", "media-1", 1, "11111111-1111-1111-1111-111111111111", "v1", 60000000, "complete", "SENTINEL_PRIVATE_PATH"),
    )
    con.execute(
        "INSERT INTO windows (segment_id, item_id, media_source_id, generation, content_version, start_ticks, end_ticks, state, vector) VALUES (?,?,?,?,?,?,?,?,?)",
        ("33333333-3333-3333-3333-333333333333", "22222222-2222-2222-2222-222222222222", "media-1", 1, "v1", 0, 60000000, "succeeded", blob),
    )
    con.commit()
    count = con.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
    con.close()
    return count


def window_count(log_path):
    last = None
    for _ in range(20):
        try:
            con = sqlite3.connect(index_db(log_path))
            count = con.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
            path = con.execute("SELECT source_path FROM media").fetchone()[0]
            generations = con.execute("SELECT COUNT(*) FROM signatures").fetchone()[0]
            incompatible = con.execute("SELECT value FROM settings WHERE key = 'incompatible'").fetchone()[0]
            con.close()
            return count, path, generations, incompatible
        except sqlite3.OperationalError as exc:
            last = exc
            time.sleep(0.25)
    raise SystemExit(f"could not read the plugin index after shutdown: {last}")


def exercise(base, log_path):
    evidence = {}
    status, body = request("GET", base + "/EmbeddingVideoSearch/v1/capabilities", anonymous=True)
    evidence["anonymous_capabilities"] = (status, body[:300])

    status, _ = request("GET", base + "/EmbeddingVideoSearch/v1/capabilities", legacy_token="not-a-real-token")
    evidence["legacy_header"] = status

    status, body = request("GET", base + "/Startup/User")
    evidence["startup_user_before"] = (status, body[:300])
    status, body = request("POST", base + "/Startup/User", {"Name": "evsload", "Password": PASSWORD})
    evidence["startup_user"] = (status, body[:300])
    if status not in (200, 204):
        raise SystemExit(f"startup user was not saved: {evidence['startup_user']}")
    status, body = request("POST", base + "/Startup/Complete")
    evidence["startup_complete"] = (status, body[:200])
    token = None
    auth_body = ""
    for _ in range(5):
        try:
            status, auth_body = request("POST", base + "/Users/AuthenticateByName", {"Username": "evsload", "Pw": PASSWORD})
        except Exception as exc:
            status, auth_body = 0, str(exc)
        evidence["auth"] = (status, auth_body[:200])
        if status == 200 and auth_body.startswith("{"):
            token = json.loads(auth_body)["AccessToken"]
            break
        time.sleep(0.5)
    if not token:
        raise SystemExit(f"authentication did not succeed: {evidence['auth']}")
    (ROOT / "token").write_text(token)
    user_id = json.loads(auth_body)["User"]["Id"]

    status, body = request("GET", base + "/EmbeddingVideoSearch/v1/capabilities", token=token)
    evidence["capabilities"] = (status, body)
    status, body = request("GET", base + "/EmbeddingVideoSearch/v1/admin/status", token=token)
    evidence["admin_status"] = (status, body[:800])
    status, plugins = request("GET", base + "/Plugins", token=token)
    evidence["plugins_status"] = status
    evidence["plugin_entry"] = "Embedding Video Search" in plugins

    status, body = request("POST", base + "/Users/New", {"Name": "evsviewer", "Password": VIEWER_PASSWORD}, token=token)
    evidence["new_user"] = (status, body[:300])
    status, body = request("POST", base + "/Users/AuthenticateByName", {"Username": "evsviewer", "Pw": VIEWER_PASSWORD})
    evidence["viewer_auth"] = (status, body[:200])
    if status != 200:
        raise SystemExit(f"viewer auth failed: {evidence['viewer_auth']}")
    viewer = json.loads(body)["AccessToken"]
    status, body = request("GET", base + "/EmbeddingVideoSearch/v1/admin/status", token=viewer)
    evidence["viewer_admin"] = (status, body[:300])
    status, body = request("GET", base + "/EmbeddingVideoSearch/v1/capabilities", token=viewer)
    evidence["viewer_capabilities"] = (status, body[:400])

    stub_port = 18774
    status, body = request(
        "PUT", base + "/EmbeddingVideoSearch/v1/admin/settings",
        {"embeddingServiceBaseUrl": f"http://127.0.0.1:{stub_port}"},
        token=token,
    )
    evidence["settings"] = (status, body[:500])
    status, body = request("POST", base + "/EmbeddingVideoSearch/v1/admin/rebuild", token=token)
    evidence["rebuild"] = (status, body[:800])
    evidence["index_path"] = str(index_db(log_path))
    evidence["index_outside_plugin"] = "plugins" not in index_db(log_path).parts
    evidence["user_id_not_accepted_from_query"] = user_id
    return evidence, token


def main():
    plugin_dir = Path(sys.argv[1])
    stub = ThreadingHTTPServer(("127.0.0.1", 18774), Stub)
    thread = threading.Thread(target=stub.serve_forever, daemon=True)
    thread.start()
    proc, log_path = start_server(plugin_dir)
    base = f"http://127.0.0.1:{PORT}"
    try:
        public = wait_ready(base, proc, log_path)
        evidence, _token = exercise(base, log_path)
        stop(proc)
        seeded = seed_window(log_path)
        Stub.processing_id = "proc-load-b"
        proc, log_path = start_server_keep(plugin_dir)
        wait_ready(base, proc, log_path)
        status, body = request("GET", base + "/EmbeddingVideoSearch/v1/capabilities", token=reuse_token())
        evidence["after_mismatch"] = (status, body[:500])
        stop(proc)
        evidence["retained"] = window_count(log_path)
        evidence["seeded"] = seeded
        evidence["public"] = public[:200]
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
        log = log_path.read_text(errors="replace")
        interesting = [line for line in log.splitlines() if "Embedding" in line or "Malfunction" in line or "embedding video search" in line.lower()]
        print("---LOG---")
        print("\n".join(interesting[-40:]))
    finally:
        stop(proc)
        stub.shutdown()


def start_server_keep(plugin_dir):
    # Restart without deleting the data directory.
    log_path = ROOT / "server-2.log"
    log = log_path.open("w")
    env = os.environ.copy()
    env.pop("DOTNET_ROOT", None)
    data = ROOT / "data"
    proc = subprocess.Popen(
        [str(JELLYFIN), "--datadir", str(data), "--configdir", str(ROOT / "config"),
         "--cachedir", str(ROOT / "cache"), "--logdir", str(ROOT / "log"), "--nowebclient"],
        stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(JELLYFIN.parent)
    )
    return proc, log_path


def reuse_token():
    return (ROOT / "token").read_text()


if __name__ == "__main__":
    main()
