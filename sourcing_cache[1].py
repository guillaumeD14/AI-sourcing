"""Petit cache borné avec TTL (mémoire limitée pour Streamlit Community Cloud)."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable, Generic, Hashable, TypeVar

V = TypeVar("V")


class BoundedTTLCache(Generic[V]):
    def __init__(self, max_entries: int = 8, ttl_seconds: float = 3600.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.max_entries, self.ttl, self._clock = max_entries, ttl_seconds, clock
        self._data: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()

    def get(self, key: Hashable) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at > self.ttl:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: Hashable, value: V) -> None:
        self._data[key] = (self._clock(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)
