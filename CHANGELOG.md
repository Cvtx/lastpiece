# Changelog

## 0.1.0 (unreleased)

First version.

- `lastpiece list`, `fix` and `sources`.
- qBittorrent (Web UI API v2): stops the torrent, selects "Do not download" files that share a
  missing piece, writes the pieces, adds the HTTP source that worked, rechecks, starts it again.
- No client: a `.torrent` and the folder its download is in.
- archive.org: `archive.org/download/` is asked first, because old torrents name servers that moved.
- A status line on the terminal: what lastpiece is doing right now (connecting, asking the client,
  waiting for a server), with a progress bar, size and speed for downloads, checking files on disk,
  and the client's recheck. `--no-progress` turns it off.
