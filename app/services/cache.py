from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from time import monotonic
from typing import Generic, Hashable, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class CacheStats:
    """轻量缓存统计信息。"""

    name: str
    size: int
    maxsize: int
    ttl_seconds: float
    hits: int
    misses: int
    evictions: int


@dataclass(slots=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class TTLCache(Generic[T]):
    """进程内 TTL + LRU 缓存。

    适合缓存短时间内重复的外部 API/爬虫调用结果：
    - TTL 控制数据新鲜度；
    - LRU 控制内存上限；
    - get/set 时做 deepcopy，避免调用方修改缓存对象。
    """

    def __init__(self, name: str, *, ttl_seconds: float, maxsize: int = 128) -> None:
        self.name = name
        self.ttl_seconds = max(float(ttl_seconds), 0.0)
        self.maxsize = max(int(maxsize), 1)
        self._items: OrderedDict[Hashable, _CacheEntry[T]] = OrderedDict()
        self._lock = RLock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: Hashable) -> T | None:
        now = monotonic()
        with self._lock:
            entry = self._items.get(key)
            if entry is None:
                self._misses += 1
                return None

            if entry.expires_at <= now:
                self._items.pop(key, None)
                self._misses += 1
                self._evictions += 1
                return None

            self._items.move_to_end(key)
            self._hits += 1
            return deepcopy(entry.value)

    def set(self, key: Hashable, value: T, *, ttl_seconds: float | None = None) -> None:
        ttl = self.ttl_seconds if ttl_seconds is None else max(float(ttl_seconds), 0.0)
        expires_at = monotonic() + ttl
        with self._lock:
            self._items[key] = _CacheEntry(value=deepcopy(value), expires_at=expires_at)
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)
                self._evictions += 1

    def delete(self, key: Hashable) -> None:
        with self._lock:
            self._items.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(
                name=self.name,
                size=len(self._items),
                maxsize=self.maxsize,
                ttl_seconds=self.ttl_seconds,
                hits=self._hits,
                misses=self._misses,
                evictions=self._evictions,
            )


__all__ = ["CacheStats", "TTLCache"]
