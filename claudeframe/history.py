from __future__ import annotations
from typing import Callable, Generic, List, Optional, TypeVar


T = TypeVar("T")


class DisplayHistory(Generic[T]):
    """Browser-style history of successfully rendered display events."""

    def __init__(self, max_entries: int = 11):
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._items: List[T] = []
        self._cursor = -1

    @property
    def cursor(self) -> int:
        return self._cursor

    def items(self) -> List[T]:
        return list(self._items)

    def current(self) -> Optional[T]:
        if self._cursor < 0:
            return None
        return self._items[self._cursor]

    def back_candidate(self) -> Optional[T]:
        if self._cursor <= 0:
            return None
        return self._items[self._cursor - 1]

    def forward_candidate(self) -> Optional[T]:
        if self._cursor >= len(self._items) - 1:
            return None
        return self._items[self._cursor + 1]

    def commit_back(self) -> None:
        if self.back_candidate() is None:
            raise RuntimeError("no back history to commit")
        self._cursor -= 1

    def commit_forward(self) -> None:
        if self.forward_candidate() is None:
            raise RuntimeError("no forward history to commit")
        self._cursor += 1

    def commit_new(self, item: T) -> None:
        if self._cursor != len(self._items) - 1:
            raise RuntimeError("new entries may only be committed at newest history")
        self._items.append(item)
        if len(self._items) > self._max_entries:
            del self._items[:len(self._items) - self._max_entries]
        self._cursor = len(self._items) - 1

    def remove_where(self, predicate: Callable[[T], bool]) -> None:
        """Remove invalid entries while preserving the cursor's relative place."""
        kept: List[T] = []
        kept_before_or_at_cursor = 0
        for index, item in enumerate(self._items):
            if predicate(item):
                continue
            kept.append(item)
            if index <= self._cursor:
                kept_before_or_at_cursor += 1
        self._items = kept
        self._cursor = kept_before_or_at_cursor - 1
