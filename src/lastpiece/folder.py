"""No client: a .torrent and the folder its download is in. lastpiece checks every piece itself
(reading all the files once), fills in the missing ones, and you recheck in your client after."""
from __future__ import annotations

import hashlib
from pathlib import Path

from .core import Log, Target, long_path
from .progress import status
from .torrent import Torrent


class FolderTarget(Target):
    def __init__(self, torrent_file: Path, folder: Path, log: Log, sources: list[str] | None = None):
        self.torrent = Torrent.load(torrent_file)
        self.name = self.torrent.name
        self.log = log
        self.sources = list(sources or [])
        folder = Path(folder)
        # the folder holding the download, or the download itself (its folder, or its one file)
        if folder.name == self.torrent.name and not (folder / self.torrent.name).exists():
            folder = folder.parent
        self.base = folder
        self._missing: list[int] | None = None

    def path(self, index: int) -> Path:
        return self.base.joinpath(*self.torrent.parts(self.torrent.files[index]))

    def extra_sources(self) -> list[str]:
        return self.sources

    def missing(self) -> list[int]:
        if self._missing is None:
            self._missing = self._check()
        return self._missing

    def _check(self) -> list[int]:
        """Hash every piece from the files on disk (missing or short files read as zeros)."""
        t = self.torrent
        bad, buf, piece = [], bytearray(), 0
        status.bar(f"{self.name}: checking the pieces on disk", t.total)
        if not any(long_path(self.path(n)).exists() for n, f in enumerate(t.files) if not f.pad):
            self.log(f"  none of its files are in {self.base}")
        for n, f in enumerate(t.files):
            left = f.size
            fh = None
            if not f.pad:
                try:
                    fh = open(long_path(self.path(n)), "rb")
                except OSError:
                    fh = None
            try:
                while left:
                    want = min(left, t.piece_length - len(buf))
                    chunk = fh.read(want) if fh else b""
                    buf += chunk + bytes(want - len(chunk))
                    status.advance(want)
                    if len(chunk) < want and fh:
                        fh.close()
                        fh = None
                    left -= want
                    if len(buf) == t.piece_length:
                        if hashlib.sha1(buf).digest() != t.hashes[piece]:
                            bad.append(piece)
                        piece += 1
                        buf.clear()
            finally:
                if fh:
                    fh.close()
        if buf:
            if hashlib.sha1(buf).digest() != t.hashes[piece]:
                bad.append(piece)
        return bad
