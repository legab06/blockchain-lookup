import io
import json
import unittest
from datetime import date, datetime, time, timezone
from unittest.mock import patch

from bitcoin_engine import search_bitcoin_window
from ethereum_engine import search_ethereum_window
from search_result_messages import no_matches_message, partial_search_warning


SEARCH_DATE = date(2024, 1, 1)
SEARCH_TIME = time(0, 0)
CENTER_TS = int(datetime.combine(SEARCH_DATE, SEARCH_TIME, timezone.utc).timestamp())


class FakeEthereumRpc:
    def __init__(self, missing_block=None, missing_receipt=False):
        self.missing_block = missing_block
        self.missing_receipt = missing_receipt

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        if isinstance(payload, list):
            return io.BytesIO(json.dumps([
                {"id": call["id"], "result": None} for call in payload
            ]).encode())
        method = payload["method"]
        if method == "eth_blockNumber":
            result = hex(CENTER_TS + 200)
        elif method == "eth_getBlockByNumber":
            block_number = int(payload["params"][0], 16)
            full = payload["params"][1]
            if full and block_number == self.missing_block:
                result = None
            else:
                transactions = (
                    [{"hash": "0xabc", "from": "0x1", "to": "0x2",
                      "value": "0x0", "input": "0x"}]
                    if full and block_number == CENTER_TS and self.missing_receipt
                    else []
                )
                result = {"timestamp": hex(block_number), "transactions": transactions}
        elif method == "eth_getTransactionReceipt":
            result = None
        else:
            raise AssertionError(method)
        return io.BytesIO(json.dumps({"id": payload["id"], "result": result}).encode())


class FakeBitcoinApi:
    def __call__(self, request, timeout):
        path = request.full_url.split("example.invalid", 1)[1]
        if path == "/blocks/tip/height":
            payload = b"10"
        elif path.startswith("/v1/mining/blocks/timestamp/"):
            payload = b'{"height": 5}'
        elif path.startswith("/blocks/"):
            start = int(path.rsplit("/", 1)[1])
            payload = json.dumps([
                {"height": height, "id": f"hash{height}",
                 "timestamp": CENTER_TS + (height - 5) * 600}
                for height in range(start, max(-1, start - 10), -1)
            ]).encode()
        elif path.startswith("/block/") and path.endswith("/raw"):
            payload = b"\x00" * 80 + b"\x00"
        else:
            raise AssertionError(path)
        return io.BytesIO(payload)


class SearchCompletenessTests(unittest.TestCase):
    def test_ethereum_complete_without_matches(self):
        with patch("ethereum_engine.urllib.request.urlopen", side_effect=FakeEthereumRpc()):
            result = search_ethereum_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0, rpc_delay=0
            )

        self.assertEqual(result["search_completeness"], "complete")
        self.assertEqual(result["failed_blocks"], 0)
        self.assertEqual(result["analyzed_blocks"], 1)
        self.assertEqual(result["outside_window_blocks"], 2)
        self.assertEqual(result["matches"], [])
        self.assertIsNone(partial_search_warning(result))
        self.assertEqual(no_matches_message(result), "Aucune correspondance trouvée.")

    def test_ethereum_missing_block_is_partial(self):
        rpc = FakeEthereumRpc(missing_block=CENTER_TS)
        with patch("ethereum_engine.urllib.request.urlopen", side_effect=rpc):
            result = search_ethereum_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0, rpc_delay=0
            )

        self.assertEqual(result["search_completeness"], "partial")
        self.assertEqual(result["failed_blocks"], 1)
        self.assertEqual(result["analyzed_blocks"], 0)
        self.assertEqual(result["outside_window_blocks"], 2)
        self.assertEqual(result["matches"], [])
        self.assertIn("1 bloc", partial_search_warning(result))
        self.assertIn("n’est pas concluante", no_matches_message(result, "1 ETH"))

    def test_ethereum_missing_receipt_is_partial(self):
        rpc = FakeEthereumRpc(missing_receipt=True)
        with patch("ethereum_engine.urllib.request.urlopen", side_effect=rpc):
            result = search_ethereum_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0, rpc_delay=0
            )

        self.assertEqual(result["search_completeness"], "partial")
        self.assertEqual(result["failed_blocks"], 1)
        self.assertEqual(result["analyzed_blocks"], 0)

    def test_bitcoin_complete_and_excludes_outside_window_blocks(self):
        with patch("bitcoin_engine.urllib.request.urlopen", side_effect=FakeBitcoinApi()):
            result = search_bitcoin_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0,
                api_url="https://example.invalid", api_delay=0,
            )

        self.assertEqual(result["search_completeness"], "complete")
        self.assertEqual(result["candidate_blocks"], 1)
        self.assertEqual(result["analyzed_blocks"], 1)
        self.assertEqual(result["failed_blocks"], 0)
        self.assertEqual(result["skipped_blocks"], 0)
        self.assertGreater(result["outside_window_blocks"], 0)


if __name__ == "__main__":
    unittest.main()
