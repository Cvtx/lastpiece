# Contributing

lastpiece is plain Python 3.10+ with no dependencies. Keep it that way unless there's a very good reason.

```sh
python -m unittest discover -s tests -v    # all tests, offline, ~15 s
python -m pip install -e .                 # the `lastpiece` command, from this checkout
```

The tests run a local web seed (with an archive.org-style redirect) and a fake qBittorrent Web
API, so nothing touches the network or a real client.

## Layout

| Path | What |
|---|---|
| `src/lastpiece/core.py` | `Target` (one torrent somewhere) and `fix`, which fetches, checks and writes pieces |
| `src/lastpiece/torrent.py`, `bencode.py` | reading .torrent files |
| `src/lastpiece/http.py` | HTTP sources: order, URLs (BEP 19), range requests, redirects |
| `src/lastpiece/folder.py` | no client: a .torrent and a folder |
| `src/lastpiece/clients/` | client adapters (`qbittorrent.py`) |
| `src/lastpiece/cli.py` | the command |

## Adding a client

1. Add `src/lastpiece/clients/<client>.py` with a `Client` subclass (connection options, a list
   of torrents, `target(infohash)`) and a `Target` subclass. The target reports the pieces the
   client lacks and where each file is on disk; `prepare` stops the torrent and selects files,
   `recheck` has the client check again, `release` starts it again if it was running.
2. Register it in `CLIENTS` in `clients/__init__.py`.
3. Test it against a fake of the client's API, like `FakeQbt` in `tests/helpers.py`.

The one rule: write a piece only after its SHA-1 matched, and only where the client has no
checked data. `core.fix` already does both, so a target only has to say where things are.
