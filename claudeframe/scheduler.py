from __future__ import annotations
import logging
import random
import threading
from typing import List, Optional

from claudeframe.indexer import Indexer, MediaItem

log = logging.getLogger(__name__)


class Scheduler:
    """Random order over current Indexer items, with reshuffle after N passes."""

    def __init__(self, indexer: Indexer, reshuffle_after_passes: int = 1):
        self._indexer = indexer
        self._reshuffle_after = max(1, reshuffle_after_passes)
        self._lock = threading.RLock()
        self._order: List[str] = []  # paths
        self._idx: int = 0
        self._passes: int = 0

    def _snapshot_paths(self) -> List[str]:
        return [i.path for i in self._indexer.items()]

    def _reshuffle(self) -> None:
        paths = self._snapshot_paths()
        random.shuffle(paths)
        self._order = paths
        self._idx = 0
        log.info("scheduler: shuffled %d items", len(paths))

    def next(self) -> Optional[MediaItem]:
        with self._lock:
            if not self._order:
                self._reshuffle()
                if not self._order:
                    return None

            while self._idx < len(self._order):
                path = self._order[self._idx]
                self._idx += 1
                item = next((i for i in self._indexer.items() if i.path == path), None)
                if item is None:
                    continue  # removed since shuffle
                return item

            # end of pass
            self._passes += 1
            if self._passes >= self._reshuffle_after:
                self._passes = 0
                self._reshuffle()
            else:
                self._idx = 0
            return self.next()

    def invalidate(self) -> None:
        """Call after items added/removed — rebuilds shuffle on next pick."""
        with self._lock:
            self._order = []
            self._idx = 0
