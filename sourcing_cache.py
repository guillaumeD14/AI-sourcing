"""Cache mémoire borné (nombre d'entrées) avec durée de vie, pour ménager quotas et mémoire."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable, Generic, Hashable, TypeVar

T = TypeVar("T")


class BoundedTTLCache(Generic[T]):
    def __init__(self, max_entries: int = 8, ttl_seconds: float = 3600.0, clock: Callable[[], float] = time.monotonic) -> None:
        if max_entries < 1:
            raise ValueError("max_entries doit être >= 1")
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._data: "OrderedDict[Hashable, tuple[float, T]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> T | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            stored_at, value = item
            if self._clock() - stored_at > self.ttl_seconds:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: T) -> None:
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
