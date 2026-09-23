import io
import json
import unittest
import urllib.error
from datetime import date, datetime, time, timezone
from unittest.mock import patch

from blockchain_lookup.ui.messages import no_matches_message, partial_search_warning
from blockchain_lookup.engines.solana import (
    MAX_CANDIDATE_SLOTS,
    KNOWN_TOKEN_MINTS,
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
        self.transactions_by_slot = {}

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
                else {
                    "blockTime": params[0],
                    "blockhash": f"solana-hash-{params[0]}",
                    "transactions": self.transactions_by_slot.get(params[0], []),
                }
            )
        else:
            raise AssertionError(f"Unexpected RPC method: {method}")

        return io.BytesIO(json.dumps({"result": result}).encode())


def _base58_encode(value):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    zeroes = len(value) - len(value.lstrip(b"\0"))
    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = alphabet[remainder] + encoded
    return "1" * zeroes + encoded


def _transaction(signature, accounts, meta, instructions=None, inner=None):
    return {
        "transaction": {
            "signatures": [signature],
            "message": {
                "accountKeys": accounts,
                "instructions": instructions or [],
            },
        },
        "meta": {"fee": 5_000, "err": None, **meta,
                 "innerInstructions": inner or []},
    }


def _search_solana_transaction(tx, amount, asset="SOL"):
    rpc = FakeSolanaRpc()
    rpc.transactions_by_slot[CENTER_TS] = [tx]
    with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc):
        return search_solana_window(
            SEARCH_DATE,
            SEARCH_TIME,
            tolerance_seconds=0,
            amount_sol=amount,
            asset_symbol=asset,
            rpc_delay=0,
        )


class SolanaEngineTests(unittest.TestCase):
    def test_window_below_limit_fetches_blocks(self):
        rpc = FakeSolanaRpc()
        with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc):
            result = search_solana_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=10, rpc_delay=0,
                rpc_url="https://user:secret@rpc.example.com/v2/key?api_key=token",
            )

        self.assertEqual(result["candidate_blocks"], 41)
        self.assertEqual(rpc.methods.count("getBlock"), 41)
        self.assertEqual(result["search_completeness"], "complete")
        self.assertEqual(result["failed_blocks"], 0)
        self.assertEqual(result["analyzed_blocks"], 21)
        self.assertEqual(result["outside_window_blocks"], 20)
        self.assertIsNone(partial_search_warning(result))
        self.assertEqual(
            result["manifest"]["source"]["endpoint"],
            "https://[endpoint privé masqué]",
        )

    def test_unavailable_candidate_marks_search_partial(self):
        rpc = FakeSolanaRpc()
        rpc.missing_block_slots.add(CENTER_TS)
        with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc):
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
        with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc):
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
        with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc), patch(
            "blockchain_lookup.engines.solana.time.sleep"
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
        with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc), patch(
            "blockchain_lookup.engines.solana.time.sleep"
        ) as sleep:
            search_solana_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0,
                rpc_delay=0, retries=3,
            )

        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_rpc_error_is_not_retried(self):
        rpc = FakeSolanaRpc()
        rpc.responses.append({"error": {"code": -32602, "message": "Invalid params"}})
        with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc), patch(
            "blockchain_lookup.engines.solana.time.sleep"
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
        with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc), patch(
            "blockchain_lookup.engines.solana.time.sleep"
        ) as sleep:
            with self.assertRaisesRegex(SolanaSearchError, "Invalid params"):
                search_solana_window(
                    SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0,
                    rpc_delay=0, retries=3,
                )

        self.assertEqual(rpc.methods.count("getBlock"), 1)
        sleep.assert_not_called()

    def test_native_sol_transfer_exact_pre_post_balances_and_deduplication(self):
        transfer = {
            "program": "system",
            "parsed": {
                "type": "transfer",
                "info": {
                    "source": "wallet",
                    "destination": "recipient",
                    "lamports": 1_250_000_000,
                },
            },
        }
        tx = _transaction(
            "sol-native-1",
            ["wallet", "recipient"],
            {
                "preBalances": [10_000_000_000, 0],
                "postBalances": [7_499_995_000, 2_500_000_000],
            },
            instructions=[transfer, transfer],
        )

        result = _search_solana_transaction(tx, "1.25")

        self.assertEqual(len(result["transactions"]), 1)
        self.assertEqual(len(result["operations"]), 1)
        self.assertEqual(result["operations"][0]["operation_type"], "transfer")
        self.assertEqual(result["operations"][0]["sent"], "1.25 SOL")
        wallet_movement = next(row for row in result["movements"] if row["account"] == "wallet")
        self.assertEqual(wallet_movement["delta_amount"], "-2.500005")
        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["match_quality"], "exact")
        self.assertEqual(result["matches"][0]["transaction_index"], 1)
        self.assertEqual(result["manifest"]["network"], "Solana")
        self.assertEqual(result["manifest"]["blocks"]["unit"], "slot")
        self.assertEqual(result["manifest"]["blocks"]["analyzed_hashes"], [
            {"number": CENTER_TS, "hash": f"solana-hash-{CENTER_TS}"}
        ])

    def test_rows_are_newest_transaction_first_with_one_based_indices(self):
        txs = [
            _transaction("sol-order-first", ["wallet", "recipient"], {}, instructions=[{
                "parsed": {"type": "transfer", "info": {
                    "source": "wallet", "destination": "recipient", "lamports": 1_000_000_000,
                }},
            }]),
            _transaction("sol-order-second", ["wallet", "recipient"], {}, instructions=[{
                "parsed": {"type": "transfer", "info": {
                    "source": "wallet", "destination": "recipient", "lamports": 2_000_000_000,
                }},
            }]),
        ]
        rpc = FakeSolanaRpc()
        rpc.transactions_by_slot[CENTER_TS] = txs
        with patch("blockchain_lookup.engines.solana.urllib.request.urlopen", side_effect=rpc):
            result = search_solana_window(
                SEARCH_DATE, SEARCH_TIME, tolerance_seconds=0, rpc_delay=0
            )

        expected = ["sol-order-second", "sol-order-first"]
        self.assertEqual([row["signature"] for row in result["transactions"]], expected)
        self.assertEqual([row["transaction_index"] for row in result["transactions"]], [2, 1])
        self.assertEqual([row["signature"] for row in result["operations"]], expected)
        self.assertEqual([row["transaction_index"] for row in result["operations"]], [2, 1])

    def test_spl_usdc_transfer_and_token_balance_deltas(self):
        mint = KNOWN_TOKEN_MINTS["USDC"]
        tx = _transaction(
            "sol-usdc-1",
            ["wallet", "wallet-usdc", "recipient-usdc"],
            {
                "preBalances": [5_000_000_000, 2_039_280, 2_039_280],
                "postBalances": [4_999_995_000, 2_039_280, 2_039_280],
                "preTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "owner": "wallet",
                     "uiTokenAmount": {"amount": "1250000", "decimals": 6}},
                    {"accountIndex": 2, "mint": mint, "owner": "recipient",
                     "uiTokenAmount": {"amount": "0", "decimals": 6}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "owner": "wallet",
                     "uiTokenAmount": {"amount": "0", "decimals": 6}},
                    {"accountIndex": 2, "mint": mint, "owner": "recipient",
                     "uiTokenAmount": {"amount": "1250000", "decimals": 6}},
                ],
            },
            instructions=[{
                "program": "spl-token",
                "parsed": {
                    "type": "transferChecked",
                    "info": {
                        "source": "wallet-usdc",
                        "destination": "recipient-usdc",
                        "authority": "wallet",
                        "mint": mint,
                        "tokenAmount": {"amount": "1250000", "decimals": 6},
                    },
                },
            }],
        )

        result = _search_solana_transaction(tx, "1.25", "USDC")

        token_transfer = next(
            row for row in result["operations"] if row["operation_type"] == "token_transfer"
        )
        self.assertEqual(token_transfer["sent"], "1.25 USDC")
        self.assertEqual(
            {row["delta_amount"] for row in result["movements"] if row["asset"] == "USDC"},
            {"-1.25", "+1.25"},
        )
        self.assertEqual(result["matches"][0]["match_quality"], "exact")
        self.assertEqual(result["matches"][0]["matched_asset"], "USDC")

    def test_sol_transfer_can_match_approximately(self):
        tx = _transaction(
            "sol-approx-1",
            ["wallet", "recipient"],
            {"preBalances": [2_000_000_000, 0],
             "postBalances": [799_994_500, 1_200_000_500]},
            instructions=[{
                "parsed": {"type": "transfer", "info": {
                    "source": "wallet", "destination": "recipient",
                    "lamports": 1_200_000_500,
                }},
            }],
        )

        result = _search_solana_transaction(tx, "1.2")

        self.assertTrue(any(row["match_quality"] == "approximate" for row in result["matches"]))

    def test_net_multi_asset_swap_and_inner_instruction_amount(self):
        mint = KNOWN_TOKEN_MINTS["USDC"]
        tx = _transaction(
            "sol-swap-inner-1",
            ["wallet", "wallet-usdc-source", "router-usdc", "wallet-usdc-received"],
            {
                "preBalances": [10_000_000_000, 2_039_280, 2_039_280, 2_039_280],
                "postBalances": [8_999_995_000, 2_039_280, 2_039_280, 2_039_280],
                "preTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "owner": "wallet",
                     "uiTokenAmount": {"amount": "500000000", "decimals": 6}},
                    {"accountIndex": 2, "mint": mint, "owner": "router",
                     "uiTokenAmount": {"amount": "0", "decimals": 6}},
                    {"accountIndex": 3, "mint": mint, "owner": "wallet",
                     "uiTokenAmount": {"amount": "0", "decimals": 6}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "owner": "wallet",
                     "uiTokenAmount": {"amount": "100000000", "decimals": 6}},
                    {"accountIndex": 2, "mint": mint, "owner": "router",
                     "uiTokenAmount": {"amount": "400000000", "decimals": 6}},
                    {"accountIndex": 3, "mint": mint, "owner": "wallet",
                     "uiTokenAmount": {"amount": "799990000", "decimals": 6}},
                ],
            },
            inner=[{
                "index": 0,
                "instructions": [{
                    "program": "spl-token",
                    "parsed": {"type": "transferChecked", "info": {
                        "source": "wallet-usdc-source",
                        "destination": "router-usdc",
                        "authority": "wallet",
                        "mint": mint,
                        "tokenAmount": {"amount": "400000000", "decimals": 6},
                    }},
                }],
            }],
        )

        result = _search_solana_transaction(tx, "400", "USDC")

        swap = next(row for row in result["operations"] if row["operation_type"] == "swap_probable")
        self.assertIn("SOL", swap["sent"])
        self.assertIn("USDC", swap["received"])
        self.assertEqual(result["matches"][0]["match_quality"], "exact")
        self.assertIn("inner:0:0", result["matches"][0]["detail"])

    def test_raw_instruction_amount_can_match_swap(self):
        target_lamports = 2_500_000_000
        mint = KNOWN_TOKEN_MINTS["USDC"]
        tx = _transaction(
            "sol-swap-raw-1",
            ["wallet", "wallet-usdc"],
            {
                "preBalances": [10_000_000_000, 2_039_280],
                "postBalances": [8_999_995_000, 2_039_280],
                "preTokenBalances": [{
                    "accountIndex": 1, "mint": mint, "owner": "wallet",
                    "uiTokenAmount": {"amount": "0", "decimals": 6},
                }],
                "postTokenBalances": [{
                    "accountIndex": 1, "mint": mint, "owner": "wallet",
                    "uiTokenAmount": {"amount": "100000000", "decimals": 6},
                }],
            },
            instructions=[{
                "programId": "Dex111111111111111111111111111111111111111",
                "data": _base58_encode(b"\x01" + target_lamports.to_bytes(8, "little")),
            }],
        )

        result = _search_solana_transaction(tx, "2.5", "SOL")

        self.assertEqual(len(result["matches"]), 1)
        self.assertEqual(result["matches"][0]["match_quality"], "exact")
        self.assertEqual(result["matches"][0]["matched_amount"], "2.5")
        self.assertIn("donn", result["matches"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
