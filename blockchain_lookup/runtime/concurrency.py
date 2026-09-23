"""Process-wide search capacity and per-session duplicate protection."""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Callable, Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any


DEFAULT_MAX_CONCURRENT_SEARCHES = 3
MAX_CONCURRENT_SEARCHES_ENV = "BLOCKCHAIN_LOOKUP_MAX_CONCURRENT_SEARCHES"
DEFAULT_QUEUE_TIMEOUT_SECONDS = 30.0
QUEUE_TIMEOUT_ENV = "BLOCKCHAIN_LOOKUP_SEARCH_QUEUE_TIMEOUT_SECONDS"
QUEUE_TIMEOUT_MESSAGE = "Serveur actuellement occupé. Réessayez dans quelques instants."

_logger = logging.getLogger(__name__)
_session_state_lock = threading.Lock()


def configured_search_limit(environ: MutableMapping[str, str] | None = None) -> int:
    """Read a positive process-wide limit, falling back to the safe default."""
    if environ is None:
        environ = os.environ
    raw = environ.get(MAX_CONCURRENT_SEARCHES_ENV)
    try:
        limit = int(raw) if raw is not None else DEFAULT_MAX_CONCURRENT_SEARCHES
    except ValueError:
        limit = 0
    if limit <= 0:
        _logger.warning("Invalid concurrent search limit; using default %s", DEFAULT_MAX_CONCURRENT_SEARCHES)
        return DEFAULT_MAX_CONCURRENT_SEARCHES
    return limit


def configured_queue_timeout(environ: MutableMapping[str, str] | None = None) -> float:
    """Read a finite positive queue timeout, falling back to 30 seconds."""
    if environ is None:
        environ = os.environ
    raw = environ.get(QUEUE_TIMEOUT_ENV)
    try:
        timeout = float(raw) if raw is not None else DEFAULT_QUEUE_TIMEOUT_SECONDS
    except ValueError:
        timeout = 0.0
    if not math.isfinite(timeout) or timeout <= 0:
        _logger.warning("Invalid search queue timeout; using default %s", DEFAULT_QUEUE_TIMEOUT_SECONDS)
        return DEFAULT_QUEUE_TIMEOUT_SECONDS
    return timeout


class SearchQueueTimeoutError(Exception):
    """Raised when a search cannot acquire a slot in time."""


class SearchLimiter:
    """Bound heavy scans in one Python process without busy waiting."""

    def __init__(self, limit: int, wait_timeout: float = DEFAULT_QUEUE_TIMEOUT_SECONDS) -> None:
        if limit <= 0:
            raise ValueError("Search limit must be positive")
        if not math.isfinite(wait_timeout) or wait_timeout <= 0:
            raise ValueError("Search queue timeout must be finite and positive")
        self.limit = limit
        self.wait_timeout = wait_timeout
        self._semaphore = threading.BoundedSemaphore(limit)

    @contextmanager
    def slot(
        self,
        *,
        search_id: str,
        network: str,
        on_queued: Callable[[], None] | None = None,
    ) -> Iterator[None]:
        acquired = self._semaphore.acquire(blocking=False)
        if not acquired:
            _logger.info("Search queued id=%s network=%s", search_id, network)
            if on_queued is not None:
                on_queued()
            if not self._semaphore.acquire(timeout=self.wait_timeout):
                _logger.warning("Search queue timed out id=%s network=%s", search_id, network)
                raise SearchQueueTimeoutError(QUEUE_TIMEOUT_MESSAGE)
            acquired = True
        try:
            _logger.info("Search slot acquired id=%s network=%s", search_id, network)
            yield
        finally:
            if acquired:
                self._semaphore.release()
                _logger.info("Search slot released id=%s network=%s", search_id, network)


SEARCH_LIMITER = SearchLimiter(configured_search_limit(), configured_queue_timeout())


def try_start_session_search(state: MutableMapping[str, Any]) -> bool:
    """Atomically claim this session's search flag across concurrent reruns."""
    with _session_state_lock:
        if state.get("search_running", False):
            return False
        state["search_running"] = True
        return True


def finish_session_search(state: MutableMapping[str, Any]) -> None:
    with _session_state_lock:
        state["search_running"] = False
