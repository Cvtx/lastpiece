"""Fetching byte ranges of a torrent's files from its HTTP sources (web seeds, BEP 19)."""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .progress import status
from .torrent import File, Torrent

USER_AGENT = f"lastpiece/{__version__}"
ARCHIVE_ORG = "https://archive.org/download/"


class FetchError(OSError):
    pass


def sources(t: Torrent, extra: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Where to ask, in order: the torrent's HTTP sources and any others given.

    Old archive.org torrents name servers that have since moved (and often hang). Their
    ``archive.org/download/`` address redirects to wherever the item is now, so it goes first."""
    out: list[str] = []
    for s in [*t.url_list, *extra]:
        s = s.strip()
        if s.startswith(("http://", "https://")) and s not in out:
            out.append(s)
    if any((urllib.parse.urlparse(s).hostname or "").endswith("archive.org") for s in out):
        out = [ARCHIVE_ORG] + [s for s in out if s != ARCHIVE_ORG]
    return out


def file_url(base: str, t: Torrent, f: File) -> str:
    """BEP 19: for a multi-file torrent the source is a folder holding the torrent's folder; for a
    single file, a URL ending in ``/`` gets the file name added, any other URL is the file itself."""
    q = "/".join(urllib.parse.quote(p, safe="") for p in t.parts(f))
    if not t.multi and not base.endswith("/"):
        return base
    return (base if base.endswith("/") else base + "/") + q


def source_base(final_url: str, t: Torrent, f: File) -> str:
    """The HTTP source a redirect ended at: the final URL without the file's path ("" if it doesn't end in it)."""
    tail = file_url("/", t, f)[1:]
    return final_url[: -len(tail)] if final_url.endswith("/" + tail) else ""


def get_range(url: str, start: int, end: int, size: int, timeout: float) -> tuple[bytes, str]:
    """Bytes ``start``..``end`` (inclusive) of a file of ``size`` bytes, following redirects; and the final URL."""
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": USER_AGENT})
    status.note(f"connecting to {host(url)}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status.note(f"from {host(r.geturl())}")
            whole = r.status == 200 and start == 0 and end == size - 1
            if r.status != 206 and not whole:
                raise FetchError(f"answered {r.status} instead of the bytes asked for")
            if r.status == 206 and not (r.headers.get("Content-Range") or "").startswith(f"bytes {start}-{end}/"):
                raise FetchError(f"sent other bytes than asked for ({r.headers.get('Content-Range')})")
            chunks, left = [], end - start + 1
            while left > 0:
                c = r.read(min(left, 1 << 20))
                if not c:
                    raise FetchError("the download stopped early")
                chunks.append(c)
                left -= len(c)
                status.advance(len(c))
            return b"".join(chunks), r.geturl()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(str(exc.reason)) from exc
    except TimeoutError as exc:
        raise FetchError("timed out") from exc


def host(url: str) -> str:
    return urllib.parse.urlparse(url).hostname or url


def fetch(t: Torrent, source: str, start: int, end: int, timeout: float, redirects: set[str]) -> bytes:
    """The torrent's bytes ``start``..``end`` (exclusive) from one source. Padding is zeros. Sources
    that redirected are added to ``redirects``."""
    out = []
    for _, f, lo, hi in t.overlapping(start, end):
        if f.pad:
            out.append(bytes(hi - lo))
            status.advance(hi - lo)
            continue
        data, final = get_range(file_url(source, t, f), lo, hi - 1, f.size, timeout)
        base = source_base(final, t, f) if t.multi else ""
        if base and base != source:
            redirects.add(base)
        out.append(data)
    return b"".join(out)


def probe(t: Torrent, source: str, timeout: float) -> str:
    """Where ``source`` sends a request for the torrent's first file now (its final URL)."""
    f = next(f for f in t.files if not f.pad)
    _, final = get_range(file_url(source, t, f), 0, 0, f.size, timeout)
    return final
