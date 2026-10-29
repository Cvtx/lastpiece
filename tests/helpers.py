"""A web seed that redirects like archive.org, and a fake qBittorrent Web API that checks pieces on disk."""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import sys
import threading
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lastpiece.bencode import encode  # noqa: E402
from lastpiece.torrent import Torrent  # noqa: E402

PIECE = 16384


def make_torrent(name: str, files: dict[str, bytes], url_list: list[str], piece: int = PIECE) -> bytes:
    blob = b"".join(files.values())
    info = {"name": name, "piece length": piece,
            "pieces": b"".join(hashlib.sha1(blob[i:i + piece]).digest() for i in range(0, len(blob), piece)),
            "files": [{"length": len(b), "path": rel.split("/")} for rel, b in files.items()]}
    return encode({"announce": "udp://tracker.invalid:80", "url-list": url_list, "info": info})


class Server:
    """Runs a handler class on 127.0.0.1 in a thread."""

    def __init__(self, handler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def web_seed(root: Path) -> Server:
    """``/old/…`` hangs up (a server that moved), ``/download/…`` redirects to ``/0/items/…``,
    which serves files from ``root`` with Range requests."""

    class Seed(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/old/"):
                self.send_error(404)
                return
            if self.path.startswith("/download/"):
                self.send_response(302)
                self.send_header("Location", "/0/items/" + self.path[len("/download/"):])
                self.end_headers()
                return
            p = root / urllib.parse.unquote(self.path[len("/0/items/"):])
            if not p.is_file():
                self.send_error(404)
                return
            body = p.read_bytes()
            lo, hi = (int(x) for x in self.headers["Range"].split("=")[1].split("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {lo}-{hi}/{len(body)}")
            self.send_header("Content-Length", str(hi - lo + 1))
            self.end_headers()
            self.wfile.write(body[lo:hi + 1])

    return Server(Seed)


class FakeQbt:
    """qBittorrent's Web API as lastpiece uses it, for one torrent saved in ``save_path``."""

    def __init__(self, data: bytes, save_path: Path, state: str = "stalledDL", off: tuple[int, ...] = ()):
        self.data, self.save_path, self.state = data, save_path, state
        self.t = Torrent.parse(data)
        self.prio = {n: 0 if n in off else 1 for n in range(len(self.t.files))}
        self.seeds: list[str] = []
        self.calls: list[str] = []
        self.checking = 0
        fake = self

        class Api(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self._answer(dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query)))

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self._answer(dict(urllib.parse.parse_qsl(self.rfile.read(n).decode())))

            def _answer(self, q):
                method = urllib.parse.urlparse(self.path).path[len("/api/v2/"):]
                out = fake.handle(method, q)
                body = out if isinstance(out, bytes) else (json.dumps(out) if not isinstance(out, str) else out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = Server(Api)
        self.url = self.server.url

    @property
    def infohash(self) -> str:
        return self.t.infohash

    def states(self) -> list[int]:
        blob = b""
        for f in self.t.files:
            p = self.save_path.joinpath(self.t.name, *f.path)
            got = p.read_bytes()[:f.size] if p.exists() else b""
            blob += got + bytes(f.size - len(got))
        L = self.t.piece_length
        return [2 if hashlib.sha1(blob[i * L:(i + 1) * L]).digest() == h else 0 for i, h in enumerate(self.t.hashes)]

    def progress(self) -> float:
        s = self.states()
        return sum(x == 2 for x in s) / len(s)

    def handle(self, method: str, q: dict):
        if method not in ("torrents/info", "torrents/files", "torrents/pieceStates", "torrents/webseeds",
                          "torrents/export", "app/version", "app/preferences"):
            self.calls.append(method)
        if method == "app/version":
            return "v5.2.1"
        if method == "app/preferences":
            return {"incomplete_files_ext": False}
        if method == "torrents/info":
            if self.checking:
                self.checking -= 1
            state = "checkingDL" if self.checking else self.state
            return [{"hash": self.infohash, "name": self.t.name, "state": state, "progress": self.progress(),
                     "size": self.t.total, "save_path": str(self.save_path),
                     "content_path": str(self.save_path / self.t.name)}]
        if method == "torrents/files":
            return [{"index": n, "name": "/".join(self.t.parts(f)), "size": f.size, "priority": self.prio[n],
                     "progress": 0} for n, f in enumerate(self.t.files)]
        if method == "torrents/pieceStates":
            return self.states()
        if method == "torrents/export":
            return self.data
        if method == "torrents/webseeds":
            return [{"url": u} for u in self.seeds]
        if method == "torrents/addWebSeeds":
            self.seeds.append(q["urls"])
        elif method == "torrents/filePrio":
            for i in q["id"].split("|"):
                self.prio[int(i)] = int(q["priority"])
        elif method == "torrents/stop":
            self.state = "stoppedDL"
        elif method == "torrents/start":
            self.state = "uploading" if self.progress() >= 1 else "downloading"
        elif method == "torrents/recheck":
            self.checking = 3
        return "Ok."

    def close(self):
        self.server.close()


def random_files(name_meta: str) -> dict[str, bytes]:
    return {"Pack.rar": os.urandom(PIECE * 5 + 1000), f"{name_meta}_meta.xml": b"<meta/>" * 30}
