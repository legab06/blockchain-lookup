import io
import json
import unittest
import urllib.error
from datetime import date, datetime, time, timezone
from unittest.mock import patch

from search_result_messages import no_matches_message, partial_search_warning
from solana_engine import (
    MAX_CANDIDATE_SLOTS,
    SolanaSearchError,
    _retry_after_seconds,
    search_solana_window,
)


SEARCH_DATE = date(2024, 1, 1)
SEARCH_TIME = time(0, 0)
CENTER_TS = int(datetime.combine(SEARCH_DATE, SEARCH_TIME, timezone.utc).timestamp())


class FakeSolanaRpc:
    def __init__(self):
        self.methods = []
        self.responses = []
        self.block_error = None
        self.missing_block_slots = set()

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        method = payload["method"]
        self.methods.append(method)

        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return io.BytesIO(json.dumps(response).encode())

        params = payload["params"]
        if method == "getFirstAvailableBlock":
            result = CENTER_TS - 2000
        elif method == "getSlot":
            result = CENTER_TS + 2000
        elif method == "getBlocks":
            result = list(range(params[0], params[1] + 1))
        elif method == "getBlockTime":
            result = params[0]
        elif method == "getBlock":
            if self.block_error is not None:
                return io.BytesIO(json.dumps({"error": self.block_error}).encode())
            result = (
                None if params[0] in self.missing_block_slots
                else {"blockTime": params[0], "transactions": []}
            )
        else:
            raise AssertionError(f"Unexpected RPC method: {method}")

        return io.BytesIO(json.dumps({"result": result}).encode())


class SolanaEngineTests(unittest.TestCase):
    def test_window_below_limit_fetches_blocks(self):
        rpc = FakeSolanaRpc()
        with patch("solana_engine.urllib.request.urlopen", side_effect=rpc):
            result = search_solana_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=10, rpc_delay=0
            )

        self.assertEqual(result["candidate_blocks"], 41)
        self.assertEqual(rpc.methods.count("getBlock"), 41)
        self.assertEqual(result["search_completeness"], "complete")
        self.assertEqual(result["failed_blocks"], 0)
        self.assertEqual(result["analyzed_blocks"], 21)
        self.assertEqual(result["outside_window_blocks"], 20)
        self.assertIsNone(partial_search_warning(result))

    def test_unavailable_candidate_marks_search_partial(self):
        rpc = FakeSolanaRpc()
        rpc.missing_block_slots.add(CENTER_TS)
        with patch("solana_engine.urllib.request.urlopen", side_effect=rpc):
            result = search_solana_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0, rpc_delay=0
            )

        self.assertEqual(result["search_completeness"], "partial")
        self.assertEqual(result["candidate_blocks"], 21)
        self.assertEqual(result["analyzed_blocks"], 0)
        self.assertEqual(result["failed_blocks"], 1)
        self.assertEqual(result["skipped_blocks"], 1)
        self.assertEqual(result["outside_window_blocks"], 20)
        self.assertEqual(result["matches"], [])
        self.assertIn("1 slot", partial_search_warning(result))
        self.assertIn("n’est pas concluante", no_matches_message(result, "1 SOL"))

    def test_window_above_limit_stops_before_get_block(self):
        rpc = FakeSolanaRpc()
        with patch("solana_engine.urllib.request.urlopen", side_effect=rpc):
            with self.assertRaises(SolanaSearchError) as caught:
                search_solana_window(
                    SEARCH_DATE, SEARCH_TIME, tolerance_seconds=400, rpc_delay=0
                )

        self.assertEqual(MAX_CANDIDATE_SLOTS, 750)
        self.assertIn("821 slots seraient analysés", str(caught.exception))
        self.assertIn("trop importante pour le RPC public Solana", str(caught.exception))
        self.assertIn("Réduisez la tolérance temporelle", str(caught.exception))
        self.assertNotIn("getBlock", rpc.methods)

    def test_retry_after_seconds_and_http_date(self):
        now = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(_retry_after_seconds(" 7 ", now), 7)
        self.assertEqual(
            _retry_after_seconds("Mon, 01 Jan 2024 00:00:12 GMT", now), 12
        )
        self.assertIsNone(_retry_after_seconds("not a date", now))

    def test_http_429_uses_retry_after_header(self):
        rpc = FakeSolanaRpc()
        rpc.responses.append(
            urllib.error.HTTPError(
                "https://example.invalid", 429, "Too Many Requests",
                {"Retry-After": "7"}, None,
            )
        )
        with patch("solana_engine.urllib.request.urlopen", side_effect=rpc), patch(
            "solana_engine.time.sleep"
        ) as sleep:
            search_solana_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0,
                rpc_delay=0, retries=2,
            )

        sleep.assert_called_once_with(7.0)

    def test_http_429_invalid_header_uses_progressive_backoff(self):
        rpc = FakeSolanaRpc()
        rpc.responses.extend(
            urllib.error.HTTPError(
                "https://example.invalid", 429, "Too Many Requests",
                {"Retry-After": "invalid"}, None,
            )
            for _ in range(2)
        )
        with patch("solana_engine.urllib.request.urlopen", side_effect=rpc), patch(
            "solana_engine.time.sleep"
        ) as sleep:
            search_solana_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0,
                rpc_delay=0, retries=3,
            )

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_rpc_error_is_not_retried(self):
        rpc = FakeSolanaRpc()
        rpc.responses.append({"error": {"code": -32602, "message": "Invalid params"}})
        with patch("solana_engine.urllib.request.urlopen", side_effect=rpc), patch(
            "solana_engine.time.sleep"
        ) as sleep:
            with self.assertRaisesRegex(SolanaSearchError, "Invalid params"):
                search_solana_window(
                    SEARCH_DATE, SEARCH_TIME, rpc_delay=0, retries=3
                )

        self.assertEqual(rpc.methods, ["getFirstAvailableBlock"])
        sleep.assert_not_called()

    def test_deterministic_get_block_error_is_not_skipped(self):
        rpc = FakeSolanaRpc()
        rpc.block_error = {"code": -32602, "message": "Invalid params"}
        with patch("solana_engine.urllib.request.urlopen", side_effect=rpc), patch(
            "solana_engine.time.sleep"
        ) as sleep:
            with self.assertRaisesRegex(SolanaSearchError, "Invalid params"):
                search_solana_window(
                    SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0,
                    rpc_delay=0, retries=3,
                )

        self.assertEqual(rpc.methods.count("getBlock"), 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
