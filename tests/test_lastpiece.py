from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import PIECE, FakeQbt, make_torrent, random_files, web_seed

from lastpiece import cli, http
from lastpiece.bencode import decode, encode
from lastpiece.core import parse_size, runs
from lastpiece.torrent import Torrent

NAME = "bfa-voltage"


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


class Basics(unittest.TestCase):
    def test_bencode_round_trip(self):
        v = {"a": [1, b"x", {"b": -2}], "c": b""}
        self.assertEqual(decode(encode(v))[0], {b"a": [1, b"x", {b"b": -2}], b"c": b""})

    def test_torrent_layout(self):
        files = {"a.bin": b"1" * (PIECE + 10), "sub/b.txt": b"2" * 20}
        t = Torrent.parse(make_torrent("pack", files, ["http://seed.example/items/"]))
        self.assertEqual((t.name, t.multi, t.total, t.piece_count), ("pack", True, PIECE + 30, 2))
        self.assertEqual(t.files_of([1]), [0, 1])
        self.assertEqual(t.parts(t.files[1]), ["pack", "sub", "b.txt"])
        self.assertEqual(http.file_url("http://seed.example/items/", t, t.files[1]), "http://seed.example/items/pack/sub/b.txt")

    def test_archive_org_download_is_asked_first(self):
        t = Torrent.parse(make_torrent("x", {"a": b"1"}, ["http://ia600300.us.archive.org/35/items/ "]))
        self.assertEqual(http.sources(t, ["ftp://nope/", "https://archive.org/download/"]),
                         ["https://archive.org/download/", "http://ia600300.us.archive.org/35/items/"])

    def test_sizes_and_runs(self):
        self.assertEqual((parse_size("64M"), parse_size("1.5g"), parse_size("0"), parse_size("500 KB")),
                         (64 << 20, int(1.5 * (1 << 30)), 0, 500 << 10))
        self.assertEqual(runs([1, 2, 3, 7, 8], 2), [[1, 2], [3], [7, 8]])


class Tty(io.StringIO):
    encoding = "utf-8"

    def isatty(self):
        return True


class StatusLine(unittest.TestCase):
    def test_spinner_bar_and_log_lines(self):
        from lastpiece.progress import Status
        s, tty = Status(), Tty()
        s.start(tty)
        self.addCleanup(s.stop)
        s.task("asking qBittorrent")
        self.assertIn("asking qBittorrent…", tty.getvalue())
        s.bar("downloading 2 piece(s)", 2048)
        s.advance(1024)
        s.note("from archive.org")
        frame = tty.getvalue().split("\r")[-1]
        self.assertIn("downloading 2 piece(s)", frame)
        self.assertIn(" 50%  1.0/2.0 KB", frame)
        self.assertIn("from archive.org", frame)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s.print("bfa-voltage")
        self.assertEqual(out.getvalue(), "bfa-voltage\n")
        self.assertTrue(tty.getvalue().split("\r")[-1].startswith(s.spinner[s.frame % len(s.spinner)]))   # drawn again
        s.stop()
        self.assertEqual(tty.getvalue().split("\r")[-1], "")   # cleared

    def test_off_when_not_a_terminal(self):
        from lastpiece.progress import Status
        s, pipe = Status(), io.StringIO()
        s.start(pipe)
        s.task("anything")
        self.assertEqual(pipe.getvalue(), "")


class Case(unittest.TestCase):
    """A torrent stuck at 99%: the last piece is shared with a small file that was never downloaded,
    and the .rar is short. Its HTTP source is stale; archive.org/download/ redirects to the files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.files = random_files(NAME)
        served = self.tmp / "served" / NAME
        served.mkdir(parents=True)
        for rel, b in self.files.items():
            (served / rel).write_bytes(b)
        self.seed = web_seed(self.tmp / "served")
        self.addCleanup(self.seed.close)
        self.data = make_torrent(NAME, self.files, [self.seed.url + "/old/"])
        (self.tmp / "item.torrent").write_bytes(self.data)
        self.dl = self.tmp / "downloads"
        (self.dl / NAME).mkdir(parents=True)
        (self.dl / NAME / "Pack.rar").write_bytes(self.files["Pack.rar"][:-1000])
        # what archive.org/download/ is, here
        p = patch.object(http, "ARCHIVE_ORG", self.seed.url + "/download/")
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(http, "sources", lambda t, extra=(): [self.seed.url + "/old/", http.ARCHIVE_ORG, *extra])
        p.start()
        self.addCleanup(p.stop)

    def assertFinished(self):
        for rel, b in self.files.items():
            self.assertEqual((self.dl / NAME / rel).read_bytes(), b, rel)


class FolderMode(Case):
    def test_list_shows_what_is_missing(self):
        code, out = run("list", str(self.tmp / "item.torrent"), "-d", str(self.dl))
        self.assertEqual(code, 0)
        self.assertIn("missing 1.2 KB (1 piece)", out)

    def test_fix_writes_the_checked_piece(self):
        code, out = run("fix", str(self.tmp / "item.torrent"), "-d", str(self.dl))
        self.assertEqual(code, 0, out)
        self.assertFinished()
        self.assertIn("works now (add it to the torrent's HTTP sources): " + self.seed.url + "/0/items/", out)
        self.assertIn("Recheck these torrents in your BitTorrent client", out)
        code, out = run("list", str(self.tmp / "item.torrent"), "-d", str(self.dl / NAME))   # the download's own folder
        self.assertIn("complete", out)

    def test_dry_run_writes_nothing(self):
        before = (self.dl / NAME / "Pack.rar").read_bytes()
        code, out = run("fix", "-n", str(self.tmp / "item.torrent"), "-d", str(self.dl))
        self.assertEqual(code, 0, out)
        self.assertIn("1 would finish", out)
        self.assertEqual((self.dl / NAME / "Pack.rar").read_bytes(), before)
        self.assertFalse((self.dl / NAME / f"{NAME}_meta.xml").exists())

    def test_a_piece_that_doesnt_match_is_never_written(self):
        (self.tmp / "served" / NAME / f"{NAME}_meta.xml").write_bytes(b"<changed/>" * 21)
        before = (self.dl / NAME / "Pack.rar").read_bytes()
        code, out = run("fix", str(self.tmp / "item.torrent"), "-d", str(self.dl))
        self.assertEqual(code, 1)
        self.assertIn("aren't on any source as the torrent has them", out)
        self.assertEqual((self.dl / NAME / "Pack.rar").read_bytes(), before)

    def test_max_skips_downloads(self):
        code, out = run("fix", "--max", "1k", str(self.tmp / "item.torrent"), "-d", str(self.dl))
        self.assertEqual(code, 1)
        self.assertIn("more than --max", out)


class QbittorrentMode(Case):
    def setUp(self):
        super().setUp()
        self.qb = FakeQbt(self.data, self.dl, state="stalledDL", off=(1,))
        self.addCleanup(self.qb.close)
        p = patch("lastpiece.clients.qbittorrent.STEP", 0.01)
        p.start()
        self.addCleanup(p.stop)

    def test_list_and_fix_all(self):
        code, out = run("list", "--url", self.qb.url)
        self.assertIn(f"{self.qb.infohash[:8]}   83.33%  missing 1.2 KB (1 piece)", out)
        self.assertIn("lastpiece fix --all", out)
        code, out = run("fix", "--all", "--url", self.qb.url)
        self.assertEqual(code, 0, out)
        self.assertFinished()
        # stopped, the "Do not download" file selected, the working address added, rechecked, started again
        self.assertEqual(self.qb.calls, ["torrents/stop", "torrents/filePrio", "torrents/addWebSeeds", "torrents/recheck",
                                         "torrents/start"])
        self.assertEqual(self.qb.seeds, [self.seed.url + "/0/items/"])
        self.assertEqual(self.qb.state, "uploading")
        self.assertIn("1 finished", out)

    def test_by_name_with_json(self):
        code, out = run("fix", "voltage", "--url", self.qb.url, "--json")
        self.assertEqual(code, 0, out)
        r = json.loads(out)[0]
        self.assertEqual((r["status"], r["written"], r["progress"]), ("finished", 1, 1.0))

    def test_stopped_torrents_stay_stopped_and_failures_restart_running_ones(self):
        self.qb.state = "stoppedDL"
        run("fix", NAME, "--url", self.qb.url)
        self.assertNotIn("torrents/start", self.qb.calls)
        self.assertEqual(self.qb.state, "stoppedDL")

    def test_a_failed_fix_starts_it_again(self):
        (self.tmp / "served" / NAME / f"{NAME}_meta.xml").write_bytes(b"<changed/>" * 21)
        code, out = run("fix", NAME, "--url", self.qb.url)
        self.assertEqual(code, 1)
        self.assertEqual(self.qb.calls[-1], "torrents/start")
        self.assertNotIn("torrents/recheck", self.qb.calls)

    def test_all_skips_big_gaps_without_failing(self):
        (self.dl / NAME / "Pack.rar").write_bytes(b"")
        code, out = run("fix", "--all", "--max", "20k", "--url", self.qb.url)
        self.assertEqual(code, 0, out)
        self.assertIn("0 finished, 0 partly, 1 skipped", out)
        self.assertEqual(self.qb.calls, [])

    def test_sources_shows_redirects(self):
        code, out = run("sources", NAME, "--url", self.qb.url)
        self.assertIn(f"→ {self.seed.url}/0/items/", out)
        self.assertIn("✗ HTTP 404", out)

    def test_qbittorrent_not_running(self):
        code, out = run("list", "--url", "http://127.0.0.1:9")
        self.assertEqual(code, 2)
        self.assertIn("doesn't answer", out)


if __name__ == "__main__":
    unittest.main()
