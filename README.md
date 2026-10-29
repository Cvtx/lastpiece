# lastpiece

**Finish torrents stuck at 99%.** lastpiece downloads the few pieces a torrent is missing from
its HTTP sources, checks each one against the torrent, and writes it in. Then your client
rechecks and the torrent is complete.

```console
$ lastpiece fix --all
qBittorrent v5.2.1 at http://127.0.0.1:8080
bfa-voltage
  1 piece(s) missing (169.7 KB); asking 6 HTTP source(s)
  1 piece(s) written from https://archive.org/download/
  added as an HTTP source in qBittorrent: https://dn721904.ca.archive.org/0/items/
  100.00% complete

1 finished, 0 partly, 0 skipped, 0 failed
```

## Why torrents get stuck

Many torrents can also be downloaded over plain HTTP from "web seeds" (HTTP sources), and
archive.org gives one with every item. Older archive.org torrents name servers that have since
moved. The address redirects, libtorrent (used by qBittorrent, Deluge and others) doesn't follow
the redirect properly, and after a few failures it stops using that source
([libtorrent#7682](https://github.com/arvidn/libtorrent/issues/7682),
[qBittorrent#19747](https://github.com/qbittorrent/qBittorrent/issues/19747)).

The torrent then waits at 99.9% for peers that never come. Usually the missing pieces are the
ones the big file shares with archive.org's small files (`_meta.xml`, `_meta.sqlite`,
thumbnails). Unticking those files doesn't help: the piece is still incomplete, so the big
file stays a few hundred KB short.

lastpiece follows the redirects itself, fetches just those pieces, and leaves the rest to your
client.

## Install

Python 3.10 or newer; nothing else.

```sh
pipx install git+https://github.com/Cvtx/lastpiece    # or, from a checkout: pipx install .
```

From a checkout

```sh
git clone https://github.com/Cvtx/lastpiece
cd lastpiece
py -m pip install -e .      # Linux and macOS: python3 -m pip install -e .
lastpiece --version
```

## Use it with qBittorrent

Turn on qBittorrent's Web UI (Tools → Options → Web UI) and tick **Bypass authentication for
clients on localhost**, or pass `--username` and `--password`.

```sh
lastpiece list                      # unfinished torrents and what they're missing
lastpiece fix --all                 # finish every one that's missing at most 64 MB
lastpiece fix bfa-voltage           # one torrent: part of its name...
lastpiece fix 385e4950              # ...or its infohash (the first few characters are enough)
lastpiece fix --all --dry-run       # fetch and check, but change nothing
```

For each torrent, lastpiece:

1. stops it;
2. selects any files set to "Do not download" that share a missing piece;
3. writes in the pieces it found;
4. adds the address that worked to its HTTP sources;
5. rechecks it, and starts it again if it was running.

## Use it without a client

Give the `.torrent` file and the folder the download is in. lastpiece checks every piece
itself, which reads all the files once.

```sh
lastpiece list item.torrent -d D:\Downloads       # what's missing
lastpiece fix  item.torrent -d D:\Downloads       # fill it in
```

`-d` can be the folder that holds the download or the download's own folder. Afterwards, tell
your client to recheck the torrent ("Force recheck") so it notices the new pieces.

## Other things it can do

```sh
lastpiece sources bfa-voltage                     # where each HTTP source leads now
lastpiece fix item.torrent -s https://mirror.example/files/   # try another HTTP source too
lastpiece list --json                             # for scripts
```

## Is it safe?

- A piece is written only if its SHA-1 matches the torrent, which is the same check your client
  does. If a server sends anything else (an error page, a newer version of the file), it's
  thrown away.
- Only pieces that are missing get written. Those bytes weren't good to begin with, so nothing
  correct is ever overwritten. That holds even if you press Ctrl+C halfway.
- If something fails, a torrent that was running is started again.
- `--all` skips torrents missing more than 64 MB, because those are real downloads, not stuck
  torrents. Name a torrent, or raise `--max`, to fetch it anyway.

## When it can't help

- **The file changed after the torrent was made.** No server has the bytes the torrent
  expects, so those pieces stay missing, and lastpiece says so.
- **The torrent has no HTTP sources.** Give one with `--source` if you know a mirror.
- **The client can't find the files** (they were moved or deleted), or it doesn't have the
  torrent's metadata yet. `lastpiece list` shows these separately.
- **BitTorrent v2-only torrents.** Hybrid (v1 + v2) torrents work.

## Options

| Option | |
|---|---|
| `--all` | every unfinished torrent (`list --all`: complete ones too) |
| `--max SIZE` | skip torrents missing more than this (`64M`, `1G`, `0` = no limit) |
| `-n`, `--dry-run` | fetch and check, write nothing, leave torrents as they are |
| `-d`, `--dir DIR` | with `.torrent` files: where the download is (default: here) |
| `-s`, `--source URL` | another HTTP source to try (repeatable) |
| `--timeout S` | seconds to wait for a server (default 20) |
| `-q`, `--quiet` | one line per torrent |
| `--no-progress` | no status line (it's only shown on a terminal, never in pipes or files) |
| `--json` | machine-readable output (`list`, `fix`) |
| `--url`, `--username`, `--password`, `--api-key` | qBittorrent's Web UI; also `LASTPIECE_URL` and so on |

Exit status: `0` everything finished (or was skipped by `--all`), `1` some didn't, `2` lastpiece
couldn't start (no client, bad arguments), `130` stopped with Ctrl+C.

## Other clients

qBittorrent is the first client adapter, and more can be added: see
[CONTRIBUTING.md](CONTRIBUTING.md#adding-a-client). Until your client has one, use the `.torrent`
and folder mode, then recheck in the client.

## License

MIT
