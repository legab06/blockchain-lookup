import unittest

from blockchain_lookup.ui.operation_labels import operation_type_label


class OperationLabelTests(unittest.TestCase):
    def test_bitcoin_transfer_is_described_as_an_output(self):
        self.assertEqual(operation_type_label("transfer", "Bitcoin"), "Sortie BTC")

    def test_native_transfer_labels_remain_chain_specific(self):
        self.assertEqual(operation_type_label("transfer", "Solana"), "Transfert natif")
        self.assertEqual(operation_type_label("transfer", "Ethereum"), "Transfert natif")


if __name__ == "__main__":
    unittest.main()
