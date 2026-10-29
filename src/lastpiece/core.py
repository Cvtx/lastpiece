"""Finishing a torrent: fetch the pieces it lacks, check each against its SHA-1, write the good ones.

A ``Target`` is one torrent somewhere: in a BitTorrent client (``clients/``) or a .torrent and a
folder (``folder.py``). It says which pieces are missing and where each file is on disk, and gets
the chance to stop the torrent before writing and recheck it after. ``fix`` does the rest, the
same for every kind of target.

Only pieces that are missing are written, and only after their SHA-1 matched: bytes there
weren't any good before, so nothing correct can be overwritten, even if lastpiece is stopped
halfway.
"""
from __future__ import annotations

import hashlib
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import http
from .progress import status
from .torrent import Torrent

Log = Callable[[str], None]
RUN = 16 * 1024 * 1024   # bytes asked for per request (several consecutive pieces at once)


class LastpieceError(Exception):
    """Something the user can act on; printed without a traceback."""


class Target(ABC):
    torrent: Torrent
    name: str

    @abstractmethod
    def missing(self) -> list[int]:
        """Pieces that aren't there (or don't match)."""

    @abstractmethod
    def path(self, index: int) -> Path:
        """Where the torrent's file number ``index`` is (or goes) on disk."""

    def extra_sources(self) -> list[str]:
        """HTTP sources besides the torrent's own (e.g. ones added in the client)."""
        return []

    def prepare(self, files: list[int]) -> None:
        """Get ready for writing: stop the torrent, make sure these files are selected for download."""

    def recheck(self, still_missing: list[int]) -> float:
        """Have the client check again; the fraction now complete."""
        return 1 - len(still_missing) / self.torrent.piece_count

    def release(self) -> None:
        """Always called after ``prepare``, even on errors: start the torrent again if it was running."""

    def add_sources(self, urls: list[str], log: Log) -> None:
        """Sources that worked after a redirect. By default only suggested, for clients that can't take them."""
        for u in urls:
            log(f"  works now (add it to the torrent's HTTP sources): {u}")


@dataclass
class Result:
    name: str
    status: str = "complete"   # complete · finished · partly · refused · failed · would-finish · would-partly
    missing: int = 0
    missing_bytes: int = 0
    written: int = 0
    not_found: int = 0
    progress: float | None = None
    new_sources: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("complete", "finished", "would-finish")


def long_path(p: Path) -> Path:
    """Windows: the ``\\\\?\\`` form, so paths over 260 characters work too."""
    if sys.platform != "win32":
        return p
    s = str(Path(os.path.abspath(p)))
    if s.startswith("\\\\?\\"):
        return p
    return Path("\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s)


def runs(pieces: list[int], most: int) -> list[list[int]]:
    """Consecutive pieces grouped, at most ``most`` per group."""
    out: list[list[int]] = []
    for i in pieces:
        if out and out[-1][-1] == i - 1 and len(out[-1]) < most:
            out[-1].append(i)
        else:
            out.append([i])
    return out


def write_piece(target: Target, i: int, data: bytes) -> None:
    t = target.torrent
    start, _ = t.piece_span(i)
    for n, f, lo, hi in t.overlapping(start, start + len(data)):
        if f.pad:
            continue
        p = long_path(target.path(n))
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "r+b" if p.exists() else "wb") as fh:
            fh.seek(lo)
            fh.write(data[f.offset + lo - start: f.offset + hi - start])


def survey(target: Target) -> dict:
    """What finishing would involve, without fetching anything."""
    t = target.torrent
    with status.doing(f"{target.name}: finding the missing pieces"):
        gone = target.missing()
    return {"name": target.name, "infohash": t.infohash, "size": t.total, "pieces": t.piece_count,
            "missing": len(gone), "missing_bytes": sum(t.piece_size(i) for i in gone),
            "sources": http.sources(t, target.extra_sources())}


def fix(target: Target, log: Log, max_bytes: int | None = None, timeout: float = 20.0,
        dry_run: bool = False) -> Result:
    """Finish ``target`` from its HTTP sources (see the module docstring)."""
    t = target.torrent
    with status.doing(f"{target.name}: finding the missing pieces"):
        gone = target.missing()
    res = Result(target.name, missing=len(gone), missing_bytes=sum(t.piece_size(i) for i in gone))
    if not gone:
        res.progress = 1.0
        log("  nothing missing")
        return res
    if max_bytes and res.missing_bytes > max_bytes:
        res.status = "refused"
        res.message = (f"{size_text(res.missing_bytes)} is missing, more than --max {size_text(max_bytes)}: that's a "
                       "download, not a stuck torrent (name it, or raise --max, to fetch it anyway)")
        log(f"  skipped: {res.message}")
        return res
    sources = http.sources(t, target.extra_sources())
    if not sources:
        res.status = "refused"
        res.message = "the torrent has no HTTP sources (web seeds); give one with --source URL"
        log(f"  skipped: {res.message}")
        return res
    log(f"  {len(gone)} piece(s) missing ({size_text(res.missing_bytes)}); asking {len(sources)} HTTP source(s)")
    written: list[int] = []
    redirects: set[str] = set()
    if not dry_run:
        with status.doing(f"{target.name}: getting the torrent ready"):
            target.prepare(t.files_of(gone))
    try:
        pending = list(gone)
        for src in sources:
            if not pending:
                break
            failed: list[int] = []
            status.bar(f"{target.name}: downloading {len(pending)} piece(s)", sum(t.piece_size(i) for i in pending))
            for run in runs(pending, max(1, RUN // t.piece_length)):
                start, end = t.piece_span(run[0])[0], t.piece_span(run[-1])[1]
                try:
                    data = http.fetch(t, src, start, end, timeout, redirects)
                except http.FetchError as exc:
                    log(f"  {src}: {exc}")
                    failed += run
                    continue
                for i in run:
                    a, b = t.piece_span(i)
                    piece = data[a - start:b - start]
                    if hashlib.sha1(piece).digest() != t.hashes[i]:
                        failed.append(i)
                    elif dry_run:
                        written.append(i)
                    else:
                        write_piece(target, i, piece)
                        written.append(i)
            if len(failed) < len(pending):
                log(f"  {len(pending) - len(failed)} piece(s) {'found' if dry_run else 'written'} from {src}")
            pending = failed
        res.written, res.not_found = len(written), len(pending)
        new = sorted(redirects - set(sources))
        res.new_sources = new
        if pending:
            log(f"  {len(pending)} piece(s) aren't on any source as the torrent has them "
                "(the file may have changed since the torrent was made)")
        if dry_run:
            res.status = "would-finish" if not pending else "would-partly" if written else "failed"
            for u in new:
                log(f"  redirects to: {u}")
            return res
        if new:
            with status.doing(f"{target.name}: adding the address that worked"):
                target.add_sources(new, log)
        if not written:
            res.status = "failed"
            res.message = "no HTTP source has the missing pieces as the torrent has them"
            return res
        with status.doing(f"{target.name}: rechecking"):
            res.progress = target.recheck(pending)
        res.status = "finished" if res.progress >= 1 else "partly"
        log(f"  {res.progress * 100:.2f}% complete")
        return res
    finally:
        status.idle()
        if not dry_run:
            with status.doing(f"{target.name}: starting it again"):
                target.release()


def size_text(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def parse_size(text: str) -> int:
    """``64M``, ``1.5G``, ``500k``, ``0`` (no limit) → bytes."""
    s = text.strip().upper().rstrip("B").replace(" ", "")
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
    if s and s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(float(s or 0))
