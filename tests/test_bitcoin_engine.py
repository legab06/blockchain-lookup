import hashlib
import io
import json
import unittest
from datetime import date, datetime, time, timezone
from unittest.mock import patch

from blockchain_lookup.engines.bitcoin import (
    _decode_output_destination,
    _parse_block,
    _parse_transaction,
    _select_candidate_blocks,
    search_bitcoin_window,
)


SEARCH_DATE = date(2024, 1, 1)
SEARCH_TIME = time(0, 0)
CENTER_TS = int(datetime.combine(SEARCH_DATE, SEARCH_TIME, timezone.utc).timestamp())


def _legacy_transaction(value_sats, prev_hash):
    script = bytes.fromhex("76a914" + "00" * 20 + "88ac")
    coinbase = prev_hash is None
    previous_output = (
        b"\x00" * 32 + (0xFFFFFFFF).to_bytes(4, "little")
        if coinbase
        else prev_hash + (0).to_bytes(4, "little")
    )
    script_sig = b"\x01\x01" if coinbase else b"\x01\x51"
    return (
        (1).to_bytes(4, "little", signed=True)
        + b"\x01"
        + previous_output
        + bytes([len(script_sig)])
        + script_sig
        + (0xFFFFFFFF).to_bytes(4, "little")
        + b"\x01"
        + value_sats.to_bytes(8, "little")
        + bytes([len(script)])
        + script
        + (0).to_bytes(4, "little")
    )


class FakeBitcoinApi:
    def __init__(self, raw_block):
        self.raw_block = raw_block

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
        elif path == "/block/hash5/raw":
            payload = self.raw_block
        else:
            raise AssertionError(path)
        return io.BytesIO(payload)


class BitcoinEngineTests(unittest.TestCase):
    def test_p2pkh_destination(self):
        script = bytes.fromhex(
            "76a914"
            + "00" * 20
            + "88ac"
        )
        destination, script_type = (
            _decode_output_destination(script)
        )
        self.assertEqual(
            destination,
            "1111111111111111111114oLvT2",
        )
        self.assertEqual(
            script_type,
            "P2PKH",
        )

    def test_parse_legacy_transaction_and_block(self):
        script = bytes.fromhex(
            "76a914"
            + "00" * 20
            + "88ac"
        )
        transaction = (
            (1).to_bytes(
                4,
                "little",
                signed=True,
            )
            + b"\x01"
            + b"\x00" * 32
            + (0xFFFFFFFF).to_bytes(
                4,
                "little",
            )
            + b"\x01\x00"
            + (0xFFFFFFFF).to_bytes(
                4,
                "little",
            )
            + b"\x01"
            + (12_345_678).to_bytes(
                8,
                "little",
            )
            + bytes([len(script)])
            + script
            + (0).to_bytes(
                4,
                "little",
            )
        )

        parsed, offset = _parse_transaction(
            transaction,
            0,
        )
        self.assertEqual(
            offset,
            len(transaction),
        )
        self.assertFalse(
            parsed["segwit"]
        )
        self.assertEqual(
            parsed["outputs"][0][
                "value_sats"
            ],
            12_345_678,
        )
        self.assertEqual(
            parsed["outputs"][0][
                "destination"
            ],
            "1111111111111111111114oLvT2",
        )
        self.assertEqual(
            len(parsed["txid"]),
            64,
        )

        block = (
            b"\x00" * 80
            + b"\x01"
            + transaction
        )
        block_transactions = (
            _parse_block(block)
        )
        self.assertEqual(
            len(block_transactions),
            1,
        )
        self.assertEqual(
            block_transactions[0]["txid"],
            parsed["txid"],
        )

    def test_candidate_blocks_use_exact_window_when_available(self):
        blocks = [
            {"height": 100, "timestamp": 1_000},
            {"height": 101, "timestamp": 1_600},
            {"height": 102, "timestamp": 2_200},
        ]

        selected, fallback = _select_candidate_blocks(
            blocks,
            start_ts=1_550,
            end_ts=1_650,
            center_ts=1_600,
        )

        self.assertFalse(fallback)
        self.assertEqual([block["height"] for block in selected], [101])

    def test_candidate_blocks_fall_back_to_nearest_block(self):
        blocks = [
            {"height": 100, "timestamp": 1_000},
            {"height": 101, "timestamp": 1_600},
            {"height": 102, "timestamp": 2_200},
        ]

        selected, fallback = _select_candidate_blocks(
            blocks,
            start_ts=1_850,
            end_ts=1_950,
            center_ts=1_900,
        )

        self.assertTrue(fallback)
        self.assertEqual([block["height"] for block in selected], [101])

    def test_search_uses_one_based_positions_and_newest_first_for_outputs(self):
        raw_block = (
            b"\x00" * 80
            + b"\x02"
            + _legacy_transaction(50_000, None)
            + _legacy_transaction(100_000, b"\x11" * 32)
        )
        parsed = _parse_block(raw_block)
        api = FakeBitcoinApi(raw_block)
        with patch("blockchain_lookup.engines.bitcoin.urllib.request.urlopen", side_effect=api):
            result = search_bitcoin_window(
                SEARCH_DATE,
                SEARCH_TIME,
                tolerance_seconds=0,
                amount_btc="0.001",
                api_url="https://example.invalid",
                api_delay=0,
            )

        expected_signatures = [parsed[1]["txid"], parsed[0]["txid"]]
        self.assertEqual([row["signature"] for row in result["transactions"]], expected_signatures)
        self.assertEqual([row["transaction_index"] for row in result["transactions"]], [2, 1])
        self.assertEqual([row["transaction_index"] for row in result["operations"]], [2, 1])
        self.assertEqual(result["matches"][0]["transaction_index"], 2)

        output = result["operations"][0]
        self.assertEqual(output["operation_type"], "transfer")
        self.assertEqual(output["source"], "")
        self.assertEqual(output["destination"], "1111111111111111111114oLvT2")
        self.assertIn("vout #0", output["evidence"])
        self.assertIn("P2PKH", output["detail"])
        self.assertIn("100000 satoshis", output["detail"])
        self.assertEqual(result["manifest"]["network"], "Bitcoin")
        self.assertEqual(result["manifest"]["blocks"]["analyzed_hashes"], [
            {"number": 5, "hash": "hash5"}
        ])
        self.assertEqual(result["manifest"]["bitcoin_time_fallback"]["used"], False)

    def test_segwit_txid_excludes_witness(self):
        version = (2).to_bytes(
            4,
            "little",
            signed=True,
        )
        vin = (
            b"\x01"
            + b"\x11" * 32
            + (0).to_bytes(
                4,
                "little",
            )
            + b"\x00"
            + (0xFFFFFFFD).to_bytes(
                4,
                "little",
            )
        )
        script = bytes.fromhex(
            "0014" + "22" * 20
        )
        vout = (
            b"\x01"
            + (5_000).to_bytes(
                8,
                "little",
            )
            + bytes([len(script)])
            + script
        )
        witness = (
            b"\x02"
            + b"\x01\x01"
            + b"\x02\x02\x03"
        )
        locktime = (0).to_bytes(
            4,
            "little",
        )
        transaction = (
            version
            + b"\x00\x01"
            + vin
            + vout
            + witness
            + locktime
        )

        parsed, offset = _parse_transaction(
            transaction,
            0,
        )
        stripped = (
            version
            + vin
            + vout
            + locktime
        )
        expected_txid = hashlib.sha256(
            hashlib.sha256(
                stripped
            ).digest()
        ).digest()[::-1].hex()

        self.assertEqual(
            offset,
            len(transaction),
        )
        self.assertTrue(
            parsed["segwit"]
        )
        self.assertEqual(
            parsed["txid"],
            expected_txid,
        )
        self.assertEqual(
            parsed["outputs"][0][
                "script_type"
            ],
            "P2WPKH",
        )
        self.assertTrue(
            parsed["outputs"][0][
                "destination"
            ].startswith("bc1q")
        )


if __name__ == "__main__":
    unittest.main()
