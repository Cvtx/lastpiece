"""What a .torrent says: its files laid end to end, cut into pieces, each with a SHA-1."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .bencode import BencodeError, decode, info_span


class TorrentError(ValueError):
    pass


@dataclass(frozen=True)
class File:
    path: tuple[str, ...]   # inside the torrent's folder; empty for a single-file torrent
    size: int
    offset: int             # where the file starts in the torrent's byte stream
    pad: bool = False       # BEP 47 padding: zeros, never on disk


@dataclass
class Torrent:
    name: str
    files: list[File]
    piece_length: int
    hashes: list[bytes]
    infohash: str
    multi: bool
    url_list: list[str] = field(default_factory=list)   # HTTP sources (web seeds, BEP 19)

    @classmethod
    def parse(cls, data: bytes) -> "Torrent":
        try:
            top, _ = decode(data)
            start, end = info_span(data)
            info, _ = decode(data, start)
        except BencodeError as exc:
            raise TorrentError(f"not a valid .torrent: {exc}") from exc
        if b"pieces" not in info:
            raise TorrentError("a BitTorrent v2-only torrent: lastpiece needs v1 piece hashes (hybrid torrents work)")
        text = lambda b: b.decode("utf-8", "replace")
        files, off = [], 0
        for f in info.get(b"files") or [{b"length": info.get(b"length", 0)}]:
            parts = tuple(text(p) for p in (f.get(b"path.utf-8") or f.get(b"path") or []))
            files.append(File(parts, int(f[b"length"]), off, b"p" in f.get(b"attr", b"")))
            off += int(f[b"length"])
        seeds = top.get(b"url-list") or []
        seeds = [seeds] if isinstance(seeds, bytes) else seeds
        p = info[b"pieces"]
        return cls(name=text(info.get(b"name.utf-8") or info.get(b"name") or b""), files=files,
                   piece_length=int(info[b"piece length"]), hashes=[p[i:i + 20] for i in range(0, len(p), 20)],
                   infohash=hashlib.sha1(data[start:end]).hexdigest(), multi=b"files" in info,
                   url_list=[text(s).strip() for s in seeds if isinstance(s, bytes)])

    @classmethod
    def load(cls, path: Path) -> "Torrent":
        return cls.parse(Path(path).read_bytes())

    @property
    def total(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def piece_count(self) -> int:
        return len(self.hashes)

    def piece_span(self, i: int) -> tuple[int, int]:
        start = i * self.piece_length
        return start, min(start + self.piece_length, self.total)

    def piece_size(self, i: int) -> int:
        start, end = self.piece_span(i)
        return end - start

    def overlapping(self, start: int, end: int) -> Iterator[tuple[int, File, int, int]]:
        """Files holding bytes ``start``..``end`` of the stream: (index, file, from, to) within the file."""
        for n, f in enumerate(self.files):
            lo, hi = max(start, f.offset), min(end, f.offset + f.size)
            if lo < hi:
                yield n, f, lo - f.offset, hi - f.offset

    def files_of(self, pieces: list[int]) -> list[int]:
        """Indexes of the (real) files that share any of these pieces."""
        out: set[int] = set()
        for i in pieces:
            out.update(n for n, f, _, _ in self.overlapping(*self.piece_span(i)) if not f.pad)
        return sorted(out)

    def parts(self, f: File) -> list[str]:
        """The file's path below the folder holding the download, as BitTorrent clients lay it out."""
        return [self.name, *f.path] if self.multi else [self.name]
