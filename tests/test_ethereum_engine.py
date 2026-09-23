import io
import json
import unittest
from datetime import date, datetime, time, timezone
from unittest.mock import patch

from ethereum_engine import (
    KNOWN_ERC20,
    TRANSFER_TOPIC,
    EthereumSearchError,
    search_ethereum_window,
)


SEARCH_DATE = date(2024, 1, 1)
SEARCH_TIME = time(0, 0)
CENTER_TS = int(datetime.combine(SEARCH_DATE, SEARCH_TIME, timezone.utc).timestamp())
USER = "0x1111111111111111111111111111111111111111"
ROUTER = "0x2222222222222222222222222222222222222222"


def _topic_address(address):
    return "0x" + address[2:].lower().rjust(64, "0")


def _transfer_log(token, source, destination, raw_amount):
    return {
        "address": KNOWN_ERC20[token]["address"],
        "topics": [TRANSFER_TOPIC, _topic_address(source), _topic_address(destination)],
        "data": hex(raw_amount),
    }


def _transaction(tx_hash, *, sender=USER, destination=ROUTER, value=0,
                 input_data="0x", transaction_index=None):
    return {
        "hash": tx_hash,
        "from": sender,
        "to": destination,
        "value": hex(value),
        "input": input_data,
        "gasPrice": hex(20_000_000_000),
        **({"transactionIndex": hex(transaction_index)} if transaction_index is not None else {}),
    }


class FakeEthereumRpc:
    """Minimal JSON-RPC fixture with ID-based, reversed batch replies."""

    def __init__(self, transactions=None, receipts=None, *, unavailable_blocks=None,
                 unavailable_receipts=None, pruned_from=None):
        self.transactions = transactions or []
        self.receipts = receipts or {}
        self.unavailable_blocks = set(unavailable_blocks or ())
        self.unavailable_receipts = set(unavailable_receipts or ())
        self.pruned_from = pruned_from
        self.batch_sizes = []
        self.methods = []

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        if isinstance(payload, list):
            self.batch_sizes.append(len(payload))
            responses = [
                {
                    "id": call["id"],
                    "result": self._receipt(call["params"][0]),
                }
                for call in payload
            ]
            return io.BytesIO(json.dumps(list(reversed(responses))).encode())

        method = payload["method"]
        self.methods.append(method)
        if method == "eth_blockNumber":
            response = {"id": payload["id"], "result": hex(CENTER_TS + 200)}
        elif method == "eth_getBlockByNumber":
            block_number = int(payload["params"][0], 16)
            full = payload["params"][1]
            if not full and block_number == 0 and self.pruned_from is not None:
                response = {
                    "id": payload["id"],
                    "error": {
                        "code": 4444,
                        "message": f"historical data unavailable; earliest available {self.pruned_from}",
                    },
                }
            else:
                result = None if full and block_number in self.unavailable_blocks else {
                    "timestamp": hex(block_number),
                    "hash": f"ethereum-hash-{block_number}",
                    "transactions": self.transactions if full and block_number == CENTER_TS else [],
                }
                response = {"id": payload["id"], "result": result}
        elif method == "eth_getTransactionReceipt":
            response = {
                "id": payload["id"],
                "result": self._receipt(payload["params"][0]),
            }
        else:
            raise AssertionError(f"Unexpected RPC method: {method}")

        return io.BytesIO(json.dumps(response).encode())

    def _receipt(self, tx_hash):
        if tx_hash in self.unavailable_receipts:
            return None
        return self.receipts.get(tx_hash, {
            "status": "0x1",
            "gasUsed": "0x5208",
            "effectiveGasPrice": "0x4a817c800",
            "logs": [],
        })


def _search(rpc, amount=None, asset="ETH"):
    with patch("ethereum_engine.urllib.request.urlopen", side_effect=rpc):
        return search_ethereum_window(
            SEARCH_DATE,
            SEARCH_TIME,
            tolerance_seconds=0,
            amount_eth=amount,
            asset_symbol=asset,
            rpc_delay=0,
        )


class EthereumEngineTests(unittest.TestCase):
    def test_native_eth_transfer_exact(self):
        tx = _transaction("eth-native-1", value=1_500_000_000_000_000_000)
        result = _search(FakeEthereumRpc([tx]), "1.5")

        transfer = next(row for row in result["operations"] if row["operation_type"] == "transfer")
        self.assertEqual(transfer["sent"], "1.5 ETH")
        self.assertEqual(result["matches"][0]["match_quality"], "exact")
        self.assertEqual(result["matches"][0]["matched_asset"], "ETH")
        self.assertEqual(result["matches"][0]["transaction_index"], 1)
        self.assertEqual(result["manifest"]["network"], "Ethereum")
        self.assertEqual(result["manifest"]["blocks"]["analyzed_hashes"], [
            {"number": CENTER_TS, "hash": f"ethereum-hash-{CENTER_TS}"}
        ])

    def test_native_eth_transfer_approximate(self):
        tx = _transaction("eth-native-approx", value=1_500_000_500_000_000_000)
        result = _search(FakeEthereumRpc([tx]), "1.5")

        self.assertTrue(any(row["match_quality"] == "approximate" for row in result["matches"]))

    def test_results_follow_explicit_transaction_index_not_rpc_array_order(self):
        first = _transaction("eth-order-first", value=1_000_000_000_000_000_000,
                             transaction_index=0)
        second = _transaction("eth-order-second", value=2_000_000_000_000_000_000,
                              transaction_index=1)
        result = _search(FakeEthereumRpc([second, first]))

        self.assertEqual(
            [row["signature"] for row in result["transactions"]],
            ["eth-order-second", "eth-order-first"],
        )
        self.assertEqual([row["transaction_index"] for row in result["transactions"]], [2, 1])
        self.assertEqual([row["transaction_index"] for row in result["operations"]], [2, 1])

    def test_usdc_transfer_event_and_receipt_batch(self):
        tx = _transaction("eth-usdc-1", value=0)
        receipt = {
            "status": "0x1",
            "gasUsed": "0x5208",
            "effectiveGasPrice": "0x4a817c800",
            "logs": [_transfer_log("USDC", USER, ROUTER, 25_250_000)],
        }
        rpc = FakeEthereumRpc([tx], {"eth-usdc-1": receipt})

        result = _search(rpc, "25.25", "USDC")

        transfer = next(row for row in result["operations"] if row["operation_type"] == "token_transfer")
        self.assertEqual(transfer["sent"], "25.25 USDC")
        self.assertEqual(result["matches"][0]["matched_asset"], "USDC")
        self.assertEqual(result["matches"][0]["match_quality"], "exact")
        self.assertEqual(rpc.batch_sizes, [1])

    def test_weth_counts_as_eth_in_probable_swap(self):
        tx = _transaction("eth-weth-swap", value=0)
        receipt = {
            "status": "0x1",
            "gasUsed": "0x5208",
            "effectiveGasPrice": "0x4a817c800",
            "logs": [
                _transfer_log("USDC", USER, ROUTER, 2_000_000_000),
                _transfer_log("WETH", ROUTER, USER, 1_000_000_000_000_000_000),
            ],
        }

        result = _search(FakeEthereumRpc([tx], {"eth-weth-swap": receipt}), "1", "ETH")

        swap = next(row for row in result["operations"] if row["operation_type"] == "swap_probable")
        self.assertEqual(swap["received"], "1 ETH")
        self.assertTrue(any(row["matched_asset"] == "ETH" for row in result["matches"]))

    def test_calldata_amount_is_evidence_for_probable_swap(self):
        raw_usdt = 100_000_000
        calldata = "0x12345678" + f"{raw_usdt:064x}"
        tx = _transaction("eth-calldata-swap", value=0, input_data=calldata)
        receipt = {
            "status": "0x1",
            "gasUsed": "0x5208",
            "effectiveGasPrice": "0x4a817c800",
            "logs": [
                _transfer_log("USDC", USER, ROUTER, 500_000_000),
                _transfer_log("WETH", ROUTER, USER, 500_000_000_000_000_000),
            ],
        }

        result = _search(FakeEthereumRpc([tx], {"eth-calldata-swap": receipt}), "100", "USDT")

        self.assertTrue(any(row["operation_type"] == "swap_probable" for row in result["operations"]))
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["matched_asset"], "USDT")
        self.assertIn("calldata", result["matches"][0]["detail"])

    def test_duplicate_transfer_logs_produce_one_operation_and_match(self):
        tx = _transaction("eth-usdc-duplicate", value=0)
        repeated_event = _transfer_log("USDC", USER, ROUTER, 5_000_000)
        receipt = {"status": "0x1", "logs": [repeated_event, repeated_event]}

        result = _search(FakeEthereumRpc([tx], {"eth-usdc-duplicate": receipt}), "5", "USDC")

        token_transfers = [row for row in result["operations"] if row["operation_type"] == "token_transfer"]
        self.assertEqual(len(token_transfers), 1)
        self.assertEqual(len(result["matches"]), 1)

    def test_receipt_batch_is_split_at_configured_size_and_matched_by_id(self):
        txs = [_transaction(f"batch-{index}", value=0) for index in range(81)]
        rpc = FakeEthereumRpc(txs)

        result = _search(rpc)

        self.assertEqual(rpc.batch_sizes, [80, 1])
        self.assertEqual(len(result["transactions"]), 81)
        self.assertEqual(result["skipped_blocks"], 0)

    def test_pruned_rpc_history_uses_earliest_available_block(self):
        statuses = []
        rpc = FakeEthereumRpc(pruned_from=100)
        with patch("ethereum_engine.urllib.request.urlopen", side_effect=rpc):
            result = search_ethereum_window(
                SEARCH_DATE,
                SEARCH_TIME,
                tolerance_seconds=0,
                rpc_delay=0,
                status_callback=statuses.append,
            )

        self.assertGreaterEqual(result["query_start_block"], 100)
        self.assertTrue(any("prun" in message.lower() for message in statuses))

    def test_unavailable_block_and_receipt_are_reported_incomplete(self):
        tx = _transaction("eth-receipt-missing", value=0)
        missing_block = _search(FakeEthereumRpc(unavailable_blocks=[CENTER_TS]))
        missing_receipt = _search(
            FakeEthereumRpc([tx], unavailable_receipts=["eth-receipt-missing"])
        )

        self.assertEqual(missing_block["search_completeness"], "partial")
        self.assertEqual(missing_block["failed_blocks"], 1)
        self.assertEqual(missing_receipt["search_completeness"], "partial")
        self.assertEqual(missing_receipt["failed_blocks"], 1)


if __name__ == "__main__":
    unittest.main()
