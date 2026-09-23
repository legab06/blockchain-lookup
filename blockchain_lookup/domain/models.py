"""Common data contracts shared by the engines and Streamlit UI.

The engines still return dictionaries. These types document their stable keys
without adding a runtime conversion or a blockchain-wide base class.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, NotRequired, TypedDict


SearchCompleteness = Literal["complete", "partial"]


class RowBase(TypedDict):
    block: int
    signature: str


class TransactionRow(RowBase, total=False):
    block_time_utc: str
    transaction_index: int
    status: str
    operation_count: int
    operation_summary: str


class OperationRow(RowBase, total=False):
    block_time_utc: str
    transaction_index: int
    operation_type: str
    sent: str
    received: str
    source: str
    destination: str
    account: str
    detail: str


class MovementRow(RowBase, total=False):
    block_time_utc: str
    transaction_index: int
    asset: str
    delta_amount: str
    absolute_delta_amount: str
    account: str


class MatchRow(RowBase, total=False):
    block_time_utc: str
    transaction_index: int
    match_type: str
    match_quality: Literal["exact", "approximate"]
    matched_asset: str
    matched_amount: str


class ManifestBlockHash(TypedDict):
    number: int
    hash: str


class ManifestBlocks(TypedDict):
    unit: Literal["slot", "block"]
    localized_start: int
    localized_end: int
    queried_start: int
    queried_end: int
    candidate_count: int
    analyzed_count: int
    failed_count: int
    outside_window_count: int
    completeness: SearchCompleteness
    analyzed_hashes: list[ManifestBlockHash]


class ManifestApplication(TypedDict):
    name: str
    version: str
    python_version: str


class ManifestSearch(TypedDict):
    asset: str
    amount_input: str | None
    amount_interpreted: str | None
    decimal_precision: int | None
    matching_tolerance: str | None
    target_utc: str
    time_tolerance_seconds: int
    window_start_utc: str
    window_end_utc: str


class ManifestSource(TypedDict):
    kind: Literal["rpc", "api"]
    endpoint: str


class BitcoinTimeFallback(TypedDict):
    used: bool
    offset_seconds: int | None
    block: int | None
    block_timestamp_utc: str | None


class TemporalCoverage(TypedDict):
    covered_start_utc: str
    covered_end_utc: str
    missing_before: bool
    missing_after: bool


class SearchManifest(TypedDict):
    schema_version: int
    application: ManifestApplication
    network: str
    search: ManifestSearch
    source: ManifestSource
    executed_at_utc: str
    blocks: ManifestBlocks
    temporal_coverage: NotRequired[TemporalCoverage]
    bitcoin_time_fallback: NotRequired[BitcoinTimeFallback]


class SearchResult(TypedDict):
    network: str
    center_dt: datetime
    start_dt: datetime
    end_dt: datetime
    tolerance_seconds: int
    target_amount: Decimal | None
    target_asset: str
    target_amount_precision: int | None
    target_amount_tolerance: Decimal | None
    candidate_blocks: int
    analyzed_blocks: int
    skipped_blocks: int
    failed_blocks: int
    outside_window_blocks: int
    search_completeness: SearchCompleteness
    coverage_missing_before: NotRequired[bool]
    coverage_missing_after: NotRequired[bool]
    covered_start_dt: NotRequired[datetime]
    covered_end_dt: NotRequired[datetime]
    transactions: list[TransactionRow]
    operations: list[OperationRow]
    movements: list[MovementRow]
    matches: list[MatchRow]
    manifest: SearchManifest
    start_slot: NotRequired[int]
    end_slot: NotRequired[int]
    query_start_slot: NotRequired[int]
    query_end_slot: NotRequired[int]
    start_slot_time: NotRequired[int | None]
    end_slot_time: NotRequired[int | None]
    start_block: NotRequired[int]
    end_block: NotRequired[int]
    query_start_block: NotRequired[int]
    query_end_block: NotRequired[int]
    transfers: NotRequired[list[dict[str, Any]]]
    target_mint: NotRequired[str]
    target_token: NotRequired[str]
    target_sol: NotRequired[Decimal | None]
    target_lamports: NotRequired[int | None]
    time_fallback_used: NotRequired[bool]
    time_fallback_offset_seconds: NotRequired[int | None]
    time_fallback_block: NotRequired[int | None]
    time_fallback_block_timestamp: NotRequired[int | None]
    time_basis: NotRequired[str]
    submitted_amount_raw: NotRequired[str]
    submitted_asset: NotRequired[str]
    submitted_network: NotRequired[str]
