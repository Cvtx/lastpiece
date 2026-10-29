"""BitTorrent clients lastpiece can work with.

An adapter is a ``Client`` subclass in this package, added to ``CLIENTS``. It lists the client's
torrents and makes a ``core.Target`` for one of them, which knows how to stop it, which pieces
the client lacks, where its files are, and how to recheck and start it again. See
``qbittorrent.py``.
"""
from __future__ import annotations

import argparse
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core import Log, Target


@dataclass
class TorrentInfo:
    infohash: str
    name: str
    progress: float   # 0..1
    state: str        # the client's own word for it
    size: int
    location: str
    busy: str = ""    # why it can't be finished right now ("" = it can)


class Client(ABC):
    name = ""
    env_prefix = ""   # options can come from LASTPIECE_<OPTION> environment variables

    @classmethod
    def add_options(cls, parser: argparse.ArgumentParser) -> None:
        """Connection options (``--url`` and so on)."""

    @classmethod
    @abstractmethod
    def from_options(cls, opts: argparse.Namespace, log: Log) -> "Client":
        ...

    @abstractmethod
    def torrents(self) -> list[TorrentInfo]:
        ...

    @abstractmethod
    def target(self, infohash: str, sources: list[str]) -> Target:
        ...

    def describe(self) -> str:
        return self.name


def _clients() -> dict[str, type[Client]]:
    from .qbittorrent import QBittorrent
    return {QBittorrent.name: QBittorrent}


CLIENTS = _clients()
