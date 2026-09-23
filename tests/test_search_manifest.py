import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from search_manifest import (
    APP_NAME,
    APP_VERSION,
    build_search_manifest,
    manifest_json_bytes,
    redact_endpoint,
)


class SearchManifestTests(unittest.TestCase):
    def test_common_contract_and_stable_json(self):
        target = datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)
        result = {
            "network": "Solana",
            "center_dt": target,
            "start_dt": target - timedelta(seconds=30),
            "end_dt": target + timedelta(seconds=30),
            "tolerance_seconds": 30,
            "target_asset": "USDC",
            "target_amount": Decimal("1.50"),
            "target_amount_precision": 2,
            "target_amount_tolerance": Decimal("0.000001"),
            "start_slot": 100,
            "end_slot": 110,
            "query_start_slot": 98,
            "query_end_slot": 112,
            "candidate_blocks": 15,
            "analyzed_blocks": 10,
            "failed_blocks": 1,
            "outside_window_blocks": 4,
            "search_completeness": "partial",
        }
        endpoint = "https://alice:password@secret.rpc.example.com:443/v2/KEY?api_key=TOKEN#FRAGMENT"
        manifest = build_search_manifest(
            result,
            endpoint=endpoint,
            amount_input="1,50",
            analyzed_hashes=[{"number": 100, "hash": "hash100"}],
            executed_at=target + timedelta(minutes=1),
        )
        exported = manifest_json_bytes(manifest)
        parsed = json.loads(exported)

        self.assertEqual(exported, manifest_json_bytes(manifest))
        self.assertTrue(exported.endswith(b"\n"))
        self.assertEqual(parsed["application"]["name"], APP_NAME)
        self.assertEqual(parsed["application"]["version"], APP_VERSION)
        self.assertEqual(parsed["search"]["amount_input"], "1,50")
        self.assertEqual(parsed["search"]["amount_interpreted"], "1.50")
        self.assertEqual(parsed["search"]["decimal_precision"], 2)
        self.assertEqual(parsed["search"]["matching_tolerance"], "0.000001")
        self.assertEqual(parsed["search"]["target_utc"], "2024-01-01T12:00:00Z")
        self.assertEqual(parsed["search"]["window_start_utc"], "2024-01-01T11:59:30Z")
        self.assertEqual(parsed["executed_at_utc"], "2024-01-01T12:01:00Z")
        self.assertEqual(parsed["blocks"]["analyzed_hashes"], [{"number": 100, "hash": "hash100"}])
        self.assertEqual(parsed["blocks"]["completeness"], "partial")
        self.assertEqual(parsed["source"]["endpoint"], "https://[endpoint privé masqué]")
        for secret in ("alice", "password", "secret", "KEY", "TOKEN", "FRAGMENT"):
            self.assertNotIn(secret, exported.decode())

    def test_endpoint_redaction_handles_invalid_urls(self):
        self.assertEqual(redact_endpoint("https://mempool.space/api"), "https://mempool.space/api")
        self.assertEqual(redact_endpoint("https://user:pass@rpc.example.com/v2/key?token=abc"),
                         "https://[endpoint privé masqué]")
        self.assertEqual(redact_endpoint("https://example.com:secret/path"), "[endpoint masqué]")
        self.assertEqual(redact_endpoint("not-a-url"), "[endpoint masqué]")

    def test_bitcoin_fallback_is_included_when_used(self):
        target = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = {
            "network": "Bitcoin", "center_dt": target,
            "start_dt": target, "end_dt": target, "tolerance_seconds": 0,
            "target_asset": "BTC", "target_amount": None,
            "target_amount_precision": None, "target_amount_tolerance": None,
            "start_block": 5, "end_block": 5,
            "query_start_block": 3, "query_end_block": 7,
            "candidate_blocks": 1, "analyzed_blocks": 1,
            "failed_blocks": 0, "outside_window_blocks": 4,
            "search_completeness": "complete",
            "time_fallback_used": True, "time_fallback_offset_seconds": 600,
            "time_fallback_block": 5,
            "time_fallback_block_timestamp": int(target.timestamp()) + 600,
        }
        manifest = build_search_manifest(
            result, endpoint="https://mempool.space/api", amount_input="",
            analyzed_hashes=[{"number": 5, "hash": "bitcoin-hash"}], executed_at=target,
        )

        self.assertEqual(manifest["blocks"]["unit"], "block")
        self.assertEqual(manifest["bitcoin_time_fallback"], {
            "used": True, "offset_seconds": 600, "block": 5,
            "block_timestamp_utc": "2024-01-01T00:10:00Z",
        })

    def test_manifest_download_keeps_the_current_result(self):
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        self.assertIn('st.session_state["lookup_result"] = result', source)
        self.assertIn('data=manifest_json_bytes(result["manifest"])', source)
        self.assertIn('on_click="ignore"', source)


if __name__ == "__main__":
    unittest.main()
