"""qBittorrent, through its Web UI API (v2; tested with qBittorrent 5.2).

Turn the Web UI on in qBittorrent (Tools → Options → Web UI). Then either tick "Bypass
authentication for clients on localhost", or give ``--username``/``--password`` (or an API key).
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..core import LastpieceError, Log, Target
from ..progress import status
from ..torrent import Torrent
from . import Client, TorrentInfo

DEFAULT_URL = "http://127.0.0.1:8080"
CHECKING = ("checkingUP", "checkingDL", "checkingResumeData", "queuedForChecking", "moving", "allocating")
NOT_NOW = {"missingFiles": "qBittorrent can't find its files (moved or deleted?)", "error": "qBittorrent shows an error for it",
           "metaDL": "qBittorrent doesn't have its metadata yet"}
STEP = 0.5   # seconds between looks at qBittorrent


class QbtError(LastpieceError):
    pass


class QBittorrent(Client):
    name = "qbittorrent"

    def __init__(self, url: str = DEFAULT_URL, username: str = "", password: str = "", api_key: str = "",
                 timeout: float = 30):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.username, self.password, self.api_key, self.timeout = username, password, api_key, timeout
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.logged_in = False

    @classmethod
    def add_options(cls, parser: argparse.ArgumentParser) -> None:
        g = parser.add_argument_group("qBittorrent")
        g.add_argument("--url", default=os.environ.get("LASTPIECE_URL", DEFAULT_URL),
                       help=f"Web UI address (default {DEFAULT_URL}; env LASTPIECE_URL)")
        g.add_argument("--username", default=os.environ.get("LASTPIECE_USERNAME", ""), help="env LASTPIECE_USERNAME")
        g.add_argument("--password", default=os.environ.get("LASTPIECE_PASSWORD", ""), help="env LASTPIECE_PASSWORD")
        g.add_argument("--api-key", default=os.environ.get("LASTPIECE_API_KEY", ""), help="env LASTPIECE_API_KEY")

    @classmethod
    def from_options(cls, opts: argparse.Namespace, log: Log) -> "QBittorrent":
        return cls(opts.url, opts.username, opts.password, opts.api_key)

    def describe(self) -> str:
        with status.doing(f"connecting to qBittorrent at {self.url}"):
            return f"qBittorrent {self.call('app/version')} at {self.url}"

    # ---- Web API ----

    def call(self, method: str, data: dict[str, Any] | None = None, raw: bool = False) -> Any:
        if self.username and not self.logged_in and not self.api_key:
            self._login()
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"{self.url}/api/v2/{method}", body, headers={"Referer": self.url})
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                content = r.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise QbtError(f"qBittorrent at {self.url} refused the request. In qBittorrent, tick Tools → Options → "
                               "Web UI → “Bypass authentication for clients on localhost”, or give --username and "
                               "--password.") from exc
            raise QbtError(f"qBittorrent answered {exc.code} to {method}: "
                           f"{exc.read().decode('utf-8', 'replace')[:300]}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise QbtError(f"qBittorrent's Web UI doesn't answer at {self.url} ({getattr(exc, 'reason', exc)}). Start "
                           "qBittorrent and turn on Tools → Options → Web UI, or give its address with --url.") from exc
        if raw:
            return content
        text = content.decode("utf-8", "replace")
        return json.loads(text) if text[:1] in ("[", "{") else text

    def _login(self) -> None:
        self.logged_in = True
        answer = self.call("auth/login", {"username": self.username, "password": self.password})
        if str(answer).strip() != "Ok.":
            raise QbtError(f"qBittorrent didn't accept the username and password ({answer})")

    def info(self, hashes: list[str] | None = None) -> list[dict[str, Any]]:
        return self.call("torrents/info" + (f"?hashes={'|'.join(hashes)}" if hashes else ""))

    def torrents(self) -> list[TorrentInfo]:
        out = []
        with status.doing("asking qBittorrent for its torrents"):
            found = self.info()
        for t in found:
            state = t.get("state", "")
            busy = NOT_NOW.get(state, "") or ("qBittorrent is busy with it (checking or moving)" if state in CHECKING else "")
            out.append(TorrentInfo(t["hash"].lower(), t.get("name", ""), t.get("progress", 0), state, t.get("size", 0),
                                   t.get("content_path") or t.get("save_path", ""), busy))
        return out

    def target(self, infohash: str, sources: list[str]) -> "QbtTarget":
        return QbtTarget(self, infohash.lower(), sources)


class QbtTarget(Target):
    """One torrent in qBittorrent. Stopped while pieces are written (Windows doesn't let other
    programs write files qBittorrent has open), rechecked after, started again if it was running.
    Files set to "Do not download" that share a missing piece are selected first: the piece is
    only complete with them, and qBittorrent would otherwise copy its own leftovers for them
    (kept in a ``.parts`` file) over the written bytes later."""

    def __init__(self, client: QBittorrent, infohash: str, sources: list[str]):
        self.client, self.hash, self.sources = client, infohash, sources
        with status.doing(f"reading torrent {infohash[:8]} from qBittorrent"):
            self._load(infohash)

    def _load(self, infohash: str) -> None:
        client = self.client
        found = client.info([infohash])
        if not found:
            raise QbtError(f"no torrent {infohash} in qBittorrent")
        self.t = found[0]
        if self.t.get("state") in NOT_NOW:
            raise QbtError(NOT_NOW[self.t["state"]])
        self.torrent = Torrent.parse(client.call(f"torrents/export?hash={infohash}", raw=True))
        self.name = self.t.get("name") or self.torrent.name
        self.was_running = False
        self._paths: dict[int, Path] | None = None

    def missing(self) -> list[int]:
        return [i for i, s in enumerate(self.client.call(f"torrents/pieceStates?hash={self.hash}")) if s != 2]

    def extra_sources(self) -> list[str]:
        return [w["url"].strip() for w in self.client.call(f"torrents/webseeds?hash={self.hash}")] + self.sources

    def _files(self) -> dict[int, dict[str, Any]]:
        """qBittorrent's file entries by torrent file number: it lists files without padding, in
        torrent order, under their names in qBittorrent (renames included)."""
        q = sorted(self.client.call(f"torrents/files?hash={self.hash}"), key=lambda f: f.get("index", 0))
        real = [n for n, f in enumerate(self.torrent.files) if not f.pad]
        if len(real) != len(q):
            raise QbtError("qBittorrent lists other files than the torrent has")
        return dict(zip(real, q))

    def path(self, index: int) -> Path:
        if self._paths is None:
            files = self._files()
            try:
                suffix = bool(self.client.call("app/preferences").get("incomplete_files_ext"))
            except QbtError:
                suffix = False
            # qBittorrent's names are relative to the folder holding the content
            content = Path(self.t["content_path"])
            first = next(iter(files.values()))["name"].replace("\\", "/").split("/")[0]
            base = content.parent if not self.torrent.multi or content.name == first else content
            self._paths = {}
            for n, q in files.items():
                p = base.joinpath(*q["name"].replace("\\", "/").split("/"))
                if suffix and not p.exists() and q.get("progress", 0) < 1:
                    p = p.with_name(p.name + ".!qB")   # "Append .!qB extension to incomplete files"
                self._paths[n] = p
        return self._paths[index]

    def prepare(self, files: list[int]) -> None:
        self.path(0)   # read the layout before stopping
        self.was_running = not self.t["state"].startswith(("stopped", "paused"))
        status.task(f"{self.name}: stopping it in qBittorrent")
        self.client.call("torrents/stop", {"hashes": self.hash})
        self._wait(lambda t: t["state"].startswith(("stopped", "paused")) or t["state"] in CHECKING, "stopping", 60)
        q = self._files()
        off = sorted(q[n].get("index", n) for n in files if q[n].get("priority", 1) == 0)
        if off:
            status.task(f"{self.name}: selecting {len(off)} file(s) set to “Do not download”")
            self.client.call("torrents/filePrio", {"hash": self.hash, "id": "|".join(map(str, off)), "priority": 1})
            self._wait(lambda t: all(f.get("priority", 1) for f in self._files().values() if f.get("index") in off),
                       "selecting files", 30)

    def recheck(self, still_missing: list[int]) -> float:
        status.task(f"{self.name}: asking qBittorrent to recheck")
        self.client.call("torrents/recheck", {"hashes": self.hash})
        # qBittorrent starts checking a moment after it is asked: wait to see it begin, then to stay done
        soon = time.time() + 5
        while time.time() < soon and self.client.info([self.hash])[0]["state"] not in CHECKING:
            time.sleep(STEP / 5)
        quiet = 0
        status.bar(f"{self.name}: qBittorrent is rechecking", 100, unit="%")
        while quiet < 3:
            t = self._wait(lambda t: t["state"] not in CHECKING, "checking", None, show=True)
            quiet += 1
            time.sleep(STEP)
            if self.client.info([self.hash])[0]["state"] in CHECKING:
                quiet = 0
        return t.get("progress", 0)

    def release(self) -> None:
        if self.was_running:
            status.task(f"{self.name}: starting it again in qBittorrent")
            self.client.call("torrents/start", {"hashes": self.hash})

    def add_sources(self, urls: list[str], log: Log) -> None:
        for u in urls:
            try:
                self.client.call("torrents/addWebSeeds", {"hash": self.hash, "urls": u})
                log(f"  added as an HTTP source in qBittorrent: {u}")
            except QbtError:
                log(f"  works now (add it to the torrent's HTTP sources): {u}")

    def _wait(self, done: Callable[[dict], bool], what: str, timeout: float | None,
              show: bool = False) -> dict[str, Any]:
        start = time.time()
        while True:
            found = self.client.info([self.hash])
            if not found:
                raise QbtError("the torrent is gone from qBittorrent")
            if show:   # while it checks, progress is how much of the torrent it has checked
                status.update(found[0].get("progress", 0) * 100)
            if done(found[0]):
                return found[0]
            if timeout and time.time() - start > timeout:
                raise QbtError(f"qBittorrent is still {what} after {int(timeout)} s")
            time.sleep(STEP)
