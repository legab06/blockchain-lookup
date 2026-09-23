"""Stable, serializable provenance for a completed blockchain search."""

import json
import platform
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, cast
from urllib.parse import urlsplit

from blockchain_lookup.domain.models import SearchManifest
from blockchain_lookup.version import __version__ as APP_VERSION

APP_NAME = "Blockchain Lookup"
MANIFEST_SCHEMA_VERSION = 1


def redact_endpoint(url: str) -> str:
    """Expose known public origins; hide every part of a private URL host."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return "[endpoint masqué]"
    if parts.scheme not in {"http", "https"} or not hostname:
        return "[endpoint masqué]"

    public_hosts = {
        "api.mainnet-beta.solana.com",
        "ethereum-rpc.publicnode.com",
        "mempool.space",
    }
    if hostname not in public_hosts:
        return f"{parts.scheme}://[endpoint privé masqué]"
    port_text = f":{port}" if port else ""
    safe_public_path = hostname == "mempool.space" and parts.path == "/api"
    path = "/api" if safe_public_path else (
        "/[redacted]" if parts.path and parts.path != "/" else ""
    )
    return f"{parts.scheme}://{hostname}{port_text}{path}"


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def build_search_manifest(
    result: Mapping[str, Any],
    *,
    endpoint: str,
    amount_input: str | Decimal | None,
    analyzed_hashes: list[dict[str, Any]],
    executed_at: datetime | None = None,
) -> SearchManifest:
    """Build the common manifest from data already collected by an engine."""
    network = result["network"]
    unit = "slot" if network == "Solana" else "block"
    suffix = "slot" if unit == "slot" else "block"
    target_dt = result.get("center_dt") or (
        result["start_dt"] + (result["end_dt"] - result["start_dt"]) / 2
    )
    tolerance_seconds = result.get("tolerance_seconds")
    if tolerance_seconds is None:
        tolerance_seconds = int(
            (result["end_dt"] - result["start_dt"]).total_seconds() / 2
        )
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "application": {
            "name": APP_NAME,
            "version": APP_VERSION,
            "python_version": platform.python_version(),
        },
        "network": network,
        "search": {
            "asset": result["target_asset"],
            "amount_input": None if amount_input is None else str(amount_input),
            "amount_interpreted": _decimal_text(result["target_amount"]),
            "decimal_precision": result["target_amount_precision"],
            "matching_tolerance": _decimal_text(result["target_amount_tolerance"]),
            "target_utc": _utc(target_dt),
            "time_tolerance_seconds": tolerance_seconds,
            "window_start_utc": _utc(result["start_dt"]),
            "window_end_utc": _utc(result["end_dt"]),
        },
        "source": {
            "kind": "api" if network == "Bitcoin" else "rpc",
            "endpoint": redact_endpoint(endpoint),
        },
        "executed_at_utc": _utc(executed_at or datetime.now(timezone.utc)),
        "blocks": {
            "unit": unit,
            "localized_start": result[f"start_{suffix}"],
            "localized_end": result[f"end_{suffix}"],
            "queried_start": result[f"query_start_{suffix}"],
            "queried_end": result[f"query_end_{suffix}"],
            "candidate_count": result["candidate_blocks"],
            "analyzed_count": result["analyzed_blocks"],
            "failed_count": result["failed_blocks"],
            "outside_window_count": result["outside_window_blocks"],
            "completeness": result["search_completeness"],
            "analyzed_hashes": analyzed_hashes,
        },
    }
    if "covered_start_dt" in result and "covered_end_dt" in result:
        manifest["temporal_coverage"] = {
            "covered_start_utc": _utc(result["covered_start_dt"]),
            "covered_end_utc": _utc(result["covered_end_dt"]),
            "missing_before": bool(result.get("coverage_missing_before")),
            "missing_after": bool(result.get("coverage_missing_after")),
        }
    if network == "Bitcoin":
        fallback_timestamp = result.get("time_fallback_block_timestamp")
        manifest["bitcoin_time_fallback"] = {
            "used": result["time_fallback_used"],
            "offset_seconds": result["time_fallback_offset_seconds"],
            "block": result["time_fallback_block"],
            "block_timestamp_utc": _utc(
                datetime.fromtimestamp(fallback_timestamp, timezone.utc)
                if fallback_timestamp is not None else None
            ),
        }
    return cast(SearchManifest, manifest)


def manifest_json_bytes(manifest: SearchManifest) -> bytes:
    """Produce a readable UTF-8 export with stable key ordering."""
    text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text.encode("utf-8")
