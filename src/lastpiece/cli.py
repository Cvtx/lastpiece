"""The ``lastpiece`` command."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__, http
from .clients import CLIENTS, Client, TorrentInfo
from .core import LastpieceError, Result, Target, fix, parse_size, size_text, survey
from .folder import FolderTarget
from .progress import status
from .torrent import TorrentError

AUTO_MAX = "64M"
EPILOG = """examples:
  lastpiece list                          unfinished torrents in qBittorrent
  lastpiece fix --all                     finish every one missing at most 64 MB
  lastpiece fix bfa-voltage 1e7c7d32      finish these (part of a name, or an infohash)
  lastpiece fix item.torrent -d D:\\dl     no client: the .torrent and the folder its download is in
  lastpiece sources item.torrent          where the torrent's HTTP sources lead now
"""


def _say(a):
    """Progress lines; ``--quiet`` keeps the unindented ones, ``--json`` none (stdout is the JSON)."""
    def log(line: str) -> None:
        if getattr(a, "json", False):
            return
        if not a.quiet or not line.startswith("  "):
            status.print(line)
    return log


def _is_torrent_file(ref: str) -> bool:
    return ref.lower().endswith(".torrent") and Path(ref).is_file()


def _client(a, log) -> Client:
    return CLIENTS[a.client].from_options(a, log)


def _pick(rows: list[TorrentInfo], ref: str) -> TorrentInfo:
    low = ref.lower()
    if len(low) >= 4 and all(c in "0123456789abcdef" for c in low):
        hits = [r for r in rows if r.infohash.startswith(low)]
        if hits:
            return _one(hits, ref)
    exact = [r for r in rows if r.name.casefold() == ref.casefold()]
    return _one(exact or [r for r in rows if ref.casefold() in r.name.casefold()], ref)


def _one(hits: list[TorrentInfo], ref: str) -> TorrentInfo:
    if not hits:
        raise LastpieceError(f"no torrent matches {ref!r} (see: lastpiece list --all)")
    if len(hits) > 1:
        raise LastpieceError(f"{ref!r} matches {len(hits)} torrents: " + ", ".join(
            f"{h.infohash[:8]} {h.name}" for h in hits[:5]) + "; use more of the name or the infohash")
    return hits[0]


def _targets(a, log, unfinished_only: bool) -> tuple[list[tuple[str, Target | None, str]], Client | None]:
    """(name, target or None, why not) for each torrent named, or every unfinished one."""
    out: list[tuple[str, Target | None, str]] = []
    files = [r for r in a.torrents if _is_torrent_file(r)]
    names = [r for r in a.torrents if r not in files]
    for f in files:
        status.task(f"reading {Path(f).name}")
        try:
            out.append((Path(f).name, FolderTarget(Path(f), Path(a.dir), log, a.source), ""))
        except (TorrentError, OSError) as exc:
            out.append((Path(f).name, None, str(exc)))
    client = None
    if names or not files:
        client = _client(a, log)
        log(client.describe())
        rows = client.torrents()
        chosen = [_pick(rows, n) for n in names] if names else [r for r in rows if r.progress < 1 or not unfinished_only]
        for n, r in enumerate(chosen, 1):
            status.task(f"reading {r.name} ({n}/{len(chosen)})")
            if r.busy:
                out.append((r.name, None, r.busy))
                continue
            try:
                out.append((r.name, client.target(r.infohash, a.source), ""))
            except (LastpieceError, TorrentError) as exc:
                out.append((r.name, None, str(exc)))
    return out, client


def cmd_list(a) -> int:
    log = _say(a)
    targets, _ = _targets(a, log, unfinished_only=not a.all)
    rows = []
    for n, (name, t, why) in enumerate(targets, 1):
        if t is None:
            rows.append({"name": name, "error": why})
            continue
        status.task(f"looking at {name} ({n}/{len(targets)})")
        try:
            rows.append(survey(t))
        except LastpieceError as exc:
            rows.append({"name": name, "error": str(exc)})
    status.idle()
    if a.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print("Every torrent is complete.")
        return 0
    limit = parse_size(AUTO_MAX)
    for r in rows:
        if "error" in r:
            continue
        done = 1 - r["missing"] / r["pieces"] if r["pieces"] else 1
        what = f"missing {size_text(r['missing_bytes'])} ({r['missing']} piece{'s' if r['missing'] != 1 else ''})" if r["missing"] else "complete"
        note = "" if not r["missing"] else "  no HTTP sources" if not r["sources"] else \
            "  (a download: most of it is missing)" if r["missing_bytes"] > limit else ""
        print(f"  {r['infohash'][:8]}  {done * 100:6.2f}%  {what:34} {r['name']}{note}")
    if any("error" not in r and r["missing"] and r["sources"] and r["missing_bytes"] <= limit for r in rows):
        print(f"\nFinish the stuck ones (missing up to {size_text(limit)}) with: lastpiece fix --all")
    cant = [r for r in rows if "error" in r]
    if cant:
        print(f"\n{len(cant)} can't be finished now:")
        for r in cant:
            print(f"  {r['name']}: {r['error']}")
    return 0


def cmd_fix(a) -> int:
    log = _say(a)
    if not a.torrents and not a.all:
        raise LastpieceError("name the torrents to finish (part of a name, an infohash, or a .torrent file), or use --all")
    if a.torrents and a.all:
        raise LastpieceError("either name torrents or use --all, not both")
    limit = parse_size(a.max if a.max is not None else AUTO_MAX if a.all else "0") or None
    targets, _ = _targets(a, log, unfinished_only=True)
    results: list[Result] = []
    for name, t, why in targets:
        log(f"{name}")
        if t is None:
            log(f"  can't: {why}")
            # with --all, torrents the client can't let go of now are skipped, not failures
            results.append(Result(name, status="refused" if a.all else "failed", message=why))
            continue
        try:
            results.append(fix(t, log, limit, a.timeout, a.dry_run))
        except LastpieceError as exc:
            log(f"  failed: {exc}")
            results.append(Result(name, status="failed", message=str(exc)))
    status.idle()
    if a.json:
        print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
    if not results:
        log("Nothing to finish.")
        return 0
    count = lambda *s: sum(r.status in s for r in results)
    if a.dry_run:
        log(f"\nDry run: {count('would-finish')} would finish, {count('would-partly')} partly, "
            f"{count('failed', 'refused')} not (nothing was written)")
    else:
        log(f"\n{count('finished', 'complete')} finished, {count('partly')} partly, {count('refused')} skipped, "
            f"{count('failed')} failed")
        if any(isinstance(t, FolderTarget) for _, t, _ in targets) and count("finished"):
            log("Recheck these torrents in your BitTorrent client (Force recheck) so it sees the new pieces.")
    allowed = ("refused",) if a.all else ()
    return 0 if all(r.ok or r.status in allowed for r in results) else 1


def cmd_sources(a) -> int:
    log = _say(a)
    targets, _ = _targets(a, log, unfinished_only=False)
    for name, t, why in targets:
        status.print(name)
        if t is None:
            status.print(f"  can't: {why}")
            continue
        found = http.sources(t.torrent, t.extra_sources())
        if not found:
            status.print("  no HTTP sources")
        for s in found:
            status.task(f"asking {http.host(s)}")
            try:
                final = http.probe(t.torrent, s, a.timeout)
                f = next(f for f in t.torrent.files if not f.pad)
                base = http.source_base(final, t.torrent, f) or final
                status.print(f"  {s}\n    → {base}" if base != s else f"  {s}  (answers directly)")
            except http.FetchError as exc:
                status.print(f"  {s}\n    ✗ {exc}")
    status.idle()
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("torrents", nargs="*", metavar="TORRENT",
                        help="part of a torrent's name, its infohash (or the start of it), or a .torrent file")
    common.add_argument("-c", "--client", choices=sorted(CLIENTS), default="qbittorrent",
                        help="BitTorrent client to ask (default: qbittorrent); not needed for .torrent files")
    common.add_argument("-d", "--dir", default=".", help="with .torrent files: the folder holding the download "
                                                         "(or the download's own folder); default: here")
    common.add_argument("-s", "--source", action="append", default=[], metavar="URL",
                        help="another HTTP source to try (repeatable)")
    common.add_argument("--timeout", type=float, default=20, metavar="S", help="seconds to wait for a server (default 20)")
    common.add_argument("-q", "--quiet", action="store_true", help="only one line per torrent")
    common.add_argument("--no-progress", action="store_true", help="no status line (it's only shown on a terminal anyway)")
    for c in CLIENTS.values():
        c.add_options(common)

    p = argparse.ArgumentParser(prog="lastpiece", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG,
                                description="Finish torrents stuck at 99%: fetch the pieces they lack from their HTTP "
                                            "sources, check each against the torrent, and write it in.")
    p.add_argument("--version", action="version", version=f"lastpiece {__version__}")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    s = sub.add_parser("list", parents=[common], help="unfinished torrents and what they lack",
                       description="Unfinished torrents and what they lack. Nothing is changed.")
    s.add_argument("--all", action="store_true", help="also complete torrents")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("fix", parents=[common], help="fetch the missing pieces and write them in",
                       description="Fetch the missing pieces over HTTP, check each against the torrent's SHA-1, write "
                                   "the ones that match, and recheck. With a client the torrent is stopped meanwhile "
                                   "and started again after.")
    s.add_argument("--all", action="store_true", help=f"every unfinished torrent missing at most --max ({AUTO_MAX})")
    s.add_argument("--max", metavar="SIZE", help=f"skip torrents missing more than this (64M, 1G; 0 = no limit). "
                                                 f"Default: {AUTO_MAX} with --all, no limit for torrents you name")
    s.add_argument("-n", "--dry-run", action="store_true", help="fetch and check, but write nothing and leave torrents as they are")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_fix)

    s = sub.add_parser("sources", parents=[common], help="where a torrent's HTTP sources lead now",
                       description="Asks each HTTP source for the torrent's first file and shows where it redirects. "
                                   "Nothing is changed.")
    s.set_defaults(fn=cmd_sources)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")   # names with characters the console can't show
        except (AttributeError, ValueError):
            pass
    a = build_parser().parse_args(argv)
    if not a.no_progress:
        status.start()
    try:
        return a.fn(a)
    except LastpieceError as exc:
        status.stop()
        print(f"lastpiece: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        status.stop()
        print("\nlastpiece: stopped. Pieces already written are correct; the torrent was started again if it was running.",
              file=sys.stderr)
        return 130
    finally:
        status.stop()
