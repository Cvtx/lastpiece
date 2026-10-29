"""The status line: what lastpiece is doing right now, with a spinner, and a progress bar when
there is something to count (bytes downloaded, pieces checked, a client's recheck).

It is drawn on stderr, only when that is a terminal, and redrawn by a thread so the spinner
keeps turning while a request waits for a slow server. Log lines go through ``print``, which
clears the status line first, so the two never mix. Everything here is a no-op when it is off.
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator


def _encodable(text: str, stream) -> bool:
    try:
        text.encode(getattr(stream, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


class Status:
    def __init__(self) -> None:
        self.on = False
        self.stream = sys.stderr
        self.lock = threading.RLock()
        self.text = ""
        self.detail = ""
        self.done = 0.0
        self.total: float | None = None
        self.unit = "B"
        self.started = 0.0
        self.frame = 0
        self.width = 0   # characters of the status line now on screen
        self._thread: threading.Thread | None = None

    # ---- switching on and off ----

    def start(self, stream=None) -> None:
        self.stream = stream or sys.stderr
        if not (hasattr(self.stream, "isatty") and self.stream.isatty()) or os.environ.get("TERM") == "dumb":
            return
        fancy = _encodable("⠋█░", self.stream)
        self.spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏" if fancy else "|/-\\"
        self.full, self.empty = ("█", "░") if fancy else ("#", "-")
        self.ell = "…" if fancy else "..."
        self.on = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self.lock:
            self._clear()
            self.on = False

    def _spin(self) -> None:
        while self.on:
            time.sleep(0.1)
            with self.lock:
                if self.on and self.text:
                    self.frame += 1
                    self._draw()

    # ---- what is shown ----

    def task(self, text: str) -> None:
        """Something without a measurable amount: a spinner and the text."""
        with self.lock:
            self.text, self.detail, self.total = text, "", None
            self._draw()

    def bar(self, text: str, total: float, unit: str = "B", done: float = 0) -> None:
        """Something counted up to ``total`` (bytes when ``unit`` is "B", or a percentage with "%")."""
        with self.lock:
            self.text, self.detail, self.total, self.unit, self.done = text, "", float(total), unit, float(done)
            self.started = time.time()
            self._draw()

    def advance(self, n: float) -> None:
        with self.lock:
            self.done += n

    def update(self, done: float) -> None:
        with self.lock:
            self.done = float(done)

    def note(self, detail: str) -> None:
        """A short addition to the current text, e.g. "connecting to archive.org"."""
        with self.lock:
            self.detail = detail
            self._draw()

    def idle(self) -> None:
        with self.lock:
            self.text, self.detail, self.total = "", "", None
            self._clear()

    @contextmanager
    def doing(self, text: str) -> Iterator[None]:
        """``task(text)`` for a while, then back to what was shown before."""
        with self.lock:
            before = (self.text, self.detail, self.total, self.unit, self.done, self.started)
        self.task(text)
        try:
            yield
        finally:
            with self.lock:
                self.text, self.detail, self.total, self.unit, self.done, self.started = before
                self._draw() if self.text else self._clear()

    def print(self, line: str) -> None:
        """A log line (stdout), above the status line."""
        with self.lock:
            self._clear()
            print(line, flush=True)
            self._draw()

    # ---- drawing ----

    def _clear(self) -> None:
        if self.on and self.width:
            self.stream.write("\r" + " " * self.width + "\r")
            self.stream.flush()
            self.width = 0

    def _fit(self, text: str, room: int) -> str:
        """``text`` in ``room`` characters, shortened in the middle of the torrent name (before ": ")."""
        if len(text) <= room:
            return text
        name, sep, rest = text.partition(": ")
        if sep and len(rest) + len(sep) + len(self.ell) + 6 <= room:
            keep = room - len(rest) - len(sep) - len(self.ell)
            return name[: keep - keep // 3] + self.ell + name[len(name) - keep // 3:] + sep + rest
        return text[: max(1, room - len(self.ell))] + self.ell

    def _draw(self) -> None:
        if not self.on or not self.text:
            return
        cols = max(20, shutil.get_terminal_size((80, 20)).columns - 1)
        spin = self.spinner[self.frame % len(self.spinner)]
        tail = f"  {self.detail}" if self.detail else ""
        if self.total is not None:
            frac = min(1.0, self.done / self.total) if self.total else 1.0
            nums = f"{frac * 100:3.0f}%"
            if self.unit == "B":
                nums += f"  {_pair(self.done, self.total)}"
                elapsed = time.time() - self.started
                if elapsed > 1 and self.done:
                    nums += f"  {_size(self.done / elapsed)}/s"
            # the bar and the numbers stay whole; then the text; the detail only if there's room
            fixed = len(spin) + len(nums) + 5
            n = max(10, min(30, cols - fixed - len(self.text) - len(tail)))
            text = self._fit(self.text, max(12, cols - fixed - n))
            tail = tail if fixed + n + len(text) + len(tail) <= cols else ""
            filled = int(frac * n)
            body = f"{text}  {self.full * filled}{self.empty * (n - filled)} {nums}{tail}"
        else:
            body = f"{self._fit(self.text, cols - len(spin) - len(self.ell) - len(tail) - 1)}{self.ell}{tail}"
        line = f"{spin} {body}"
        if len(line) > cols:
            line = line[: cols - len(self.ell)] + self.ell
        pad = max(0, self.width - len(line))
        self.stream.write("\r" + line + " " * pad)
        self.stream.flush()
        self.width = len(line)


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _pair(done: float, total: float) -> str:
    """``1.0/3.0 MB``: both in the unit of the total."""
    n, unit = float(total), "B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            break
        n /= 1024
    scale = total / n if n else 1
    if unit == "B":
        return f"{done:.0f}/{total:.0f} B"
    return f"{done / scale:.1f}/{n:.1f} {unit}"


status = Status()
