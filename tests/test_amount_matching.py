from decimal import Decimal
import unittest

from blockchain_lookup.domain.amount_matching import parse_amount_criterion


class AmountMatchingTests(unittest.TestCase):
    def test_eth_truncated_value_matches(self):
        criterion = parse_amount_criterion(
            "0.5472056",
            max_decimals=18,
        )
        self.assertIsNotNone(criterion)
        self.assertEqual(
            criterion.classify(Decimal("0.547205695045168545")),
            "approximate",
        )

    def test_eth_full_value_is_exact(self):
        criterion = parse_amount_criterion(
            "0.547205695045168545",
            max_decimals=18,
        )
        self.assertIsNotNone(criterion)
        self.assertEqual(
            criterion.classify(Decimal("0.547205695045168545")),
            "exact",
        )

    def test_precision_boundary_is_strict(self):
        criterion = parse_amount_criterion(
            "0.5472056",
            max_decimals=18,
        )
        self.assertIsNotNone(criterion)
        self.assertIsNone(
            criterion.classify(Decimal("0.5472057"))
        )

    def test_coarse_input_is_capped_to_one_millionth(self):
        criterion = parse_amount_criterion(
            "0.5",
            max_decimals=18,
        )
        self.assertIsNotNone(criterion)
        self.assertEqual(
            criterion.tolerance,
            Decimal("0.000001"),
        )
        self.assertEqual(
            criterion.classify(Decimal("0.5000009")),
            "approximate",
        )
        self.assertIsNone(
            criterion.classify(Decimal("0.500001"))
        )

    def test_solana_precision(self):
        criterion = parse_amount_criterion(
            "0.0084633",
            max_decimals=9,
        )
        self.assertIsNotNone(criterion)
        self.assertEqual(
            criterion.classify(Decimal("0.008463398")),
            "approximate",
        )

    def test_future_bitcoin_precision(self):
        criterion = parse_amount_criterion(
            "0.1234567",
            max_decimals=8,
        )
        self.assertIsNotNone(criterion)
        self.assertEqual(
            criterion.classify(Decimal("0.12345678")),
            "approximate",
        )

    def test_rejects_too_many_decimals(self):
        with self.assertRaises(ValueError):
            parse_amount_criterion(
                "0.123456789",
                max_decimals=8,
            )


if __name__ == "__main__":
    unittest.main()
