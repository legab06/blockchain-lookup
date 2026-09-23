"""A per-search cap on detailed rows retained by blockchain scans."""

from __future__ import annotations

import logging
import os
from collections.abc import MutableMapping
from typing import Any


DEFAULT_MAX_RESULT_ROWS = 50_000
MAX_RESULT_ROWS_ENV = "BLOCKCHAIN_LOOKUP_MAX_RESULT_ROWS"

_logger = logging.getLogger(__name__)


def configured_max_result_rows(environ: MutableMapping[str, str] | None = None) -> int:
    if environ is None:
        environ = os.environ
    raw = environ.get(MAX_RESULT_ROWS_ENV)
    try:
        limit = int(raw) if raw is not None else DEFAULT_MAX_RESULT_ROWS
    except ValueError:
        limit = 0
    if limit <= 0:
        _logger.warning("Invalid result row limit; using default %s", DEFAULT_MAX_RESULT_ROWS)
        return DEFAULT_MAX_RESULT_ROWS
    return limit


class ResultLimitExceeded(Exception):
    """The scan stopped before returning a misleading truncated result."""


class ResultRowBudget:
    def __init__(self, limit: int) -> None:
        if limit <= 0:
            raise ValueError("Result row limit must be positive")
        self.limit = limit
        self.count = 0

    def rows(self) -> CappedRows:
        return CappedRows(self)

    def claim(self) -> None:
        if self.count >= self.limit:
            raise ResultLimitExceeded(
                f"Recherche interrompue : plus de {self.limit} lignes détaillées "
                "seraient conservées. Réduisez la tolérance temporelle. "
                "Les résultats seraient incomplets."
            )
        self.count += 1


class CappedRows(list[dict[str, Any]]):
    def __init__(self, budget: ResultRowBudget) -> None:
        super().__init__()
        self._budget = budget

    def append(self, row: dict[str, Any]) -> None:
        self._budget.claim()
        super().append(row)
