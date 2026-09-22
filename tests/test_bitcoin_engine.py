import hashlib
import unittest

from bitcoin_engine import (
    _decode_output_destination,
    _parse_block,
    _parse_transaction,
    _select_candidate_blocks,
)


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
