"""Bencode, the encoding .torrent files use."""
from __future__ import annotations

from typing import Any


class BencodeError(ValueError):
    pass


def decode(data: bytes, i: int = 0) -> tuple[Any, int]:
    """The value starting at byte ``i`` and the position right after it."""
    c = data[i:i + 1]
    try:
        if c == b"i":
            end = data.index(b"e", i)
            return int(data[i + 1:end]), end + 1
        if c in (b"l", b"d"):
            i += 1
            items = []
            while data[i:i + 1] != b"e":
                if i >= len(data):
                    raise BencodeError("unexpected end of data")
                v, i = decode(data, i)
                items.append(v)
            return (items if c == b"l" else dict(zip(items[::2], items[1::2]))), i + 1
        if c.isdigit():
            colon = data.index(b":", i)
            start = colon + 1
            end = start + int(data[i:colon])
            if end > len(data):
                raise BencodeError("string runs past the end of the data")
            return data[start:end], end
    except ValueError as exc:
        if isinstance(exc, BencodeError):
            raise
        raise BencodeError(f"invalid bencode at byte {i}") from exc
    raise BencodeError(f"invalid bencode at byte {i}")


def info_span(data: bytes) -> tuple[int, int]:
    """Byte range of the raw ``info`` dictionary: the infohash is the SHA-1 of exactly these bytes."""
    if data[:1] != b"d":
        raise BencodeError("not a torrent file")
    i = 1
    while data[i:i + 1] != b"e":
        key, i = decode(data, i)
        start = i
        _, i = decode(data, i)
        if key == b"info":
            return start, i
    raise BencodeError("the torrent has no info dictionary")


def encode(v: Any) -> bytes:
    if isinstance(v, bool):
        v = int(v)
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, (list, tuple)):
        return b"l" + b"".join(encode(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, x) for k, x in v.items())
        return b"d" + b"".join(encode(k) + encode(x) for k, x in items) + b"e"
    raise TypeError(f"can't bencode {type(v).__name__}")
