import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import blockchain_lookup
from streamlit.testing.v1 import AppTest
from blockchain_lookup.runtime.result_limits import (
    DEFAULT_MAX_RESULT_ROWS,
    MAX_RESULT_ROWS_ENV,
    ResultLimitExceeded,
    ResultRowBudget,
    configured_max_result_rows,
)
from blockchain_lookup.ui.downloads import to_csv_bytes
from blockchain_lookup.ui.tables import prepare_table_dataframe
from blockchain_lookup.version import __version__


ROOT = Path(__file__).resolve().parents[1]


class ReleaseHardeningTests(unittest.TestCase):
    def test_partial_time_coverage_is_visible_with_inconclusive_empty_result(self):
        target = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = {
            "network": "Solana",
            "start_dt": target - timedelta(seconds=30),
            "end_dt": target + timedelta(seconds=30),
            "covered_start_dt": target,
            "covered_end_dt": target + timedelta(seconds=30),
            "coverage_missing_before": True,
            "coverage_missing_after": False,
            "search_completeness": "partial",
            "candidate_blocks": 1,
            "analyzed_blocks": 1,
            "failed_blocks": 0,
            "outside_window_blocks": 0,
            "target_amount": Decimal("1"),
            "target_asset": "SOL",
            "target_amount_precision": 0,
            "target_amount_tolerance": Decimal("0.000001"),
            "transactions": [],
            "operations": [],
            "movements": [],
            "matches": [],
            "manifest": {},
        }
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
        with patch("blockchain_lookup.ui.app.search_solana_window", return_value=result):
            app.button[0].click().run(timeout=20)
        warnings = "\n".join(item.value for item in app.warning)
        self.assertEqual(len(app.exception), 0)
        self.assertIn("couverture temporelle incomplète", warnings)
        self.assertIn("n’est pas concluante", warnings)

    def test_result_row_limit_configuration_and_before_append_guard(self):
        for raw, expected in (
            (None, DEFAULT_MAX_RESULT_ROWS),
            ("100", 100),
            ("invalid", DEFAULT_MAX_RESULT_ROWS),
            ("0", DEFAULT_MAX_RESULT_ROWS),
            ("-1", DEFAULT_MAX_RESULT_ROWS),
        ):
            with self.subTest(raw=raw):
                environ = {} if raw is None else {MAX_RESULT_ROWS_ENV: raw}
                self.assertEqual(configured_max_result_rows(environ), expected)

        budget = ResultRowBudget(2)
        first, second = budget.rows(), budget.rows()
        first.append({"id": 1})
        second.append({"id": 2})
        with self.assertRaises(ResultLimitExceeded):
            first.append({"id": 3})
        self.assertEqual(budget.count, 2)
        self.assertEqual(first, [{"id": 1}])
        self.assertEqual(second, [{"id": 2}])

    def test_operation_csv_contains_visible_columns_without_internal_legs(self):
        rows = [{
            "block": 123,
            "signature": "tx123",
            "operation_type": "swap_probable",
            "sent": "1 SOL",
            "received": "2 USDC",
            "legs": [{"secret_internal": "do not export"}],
            "detail": "Échange probable",
        }]
        payload = to_csv_bytes(rows, table_kind="operations", network="Solana")
        csv_text = payload.decode("utf-8-sig")
        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        self.assertIn("Type;Envoyé;Reçu", csv_text)
        self.assertNotIn("legs", csv_text)
        self.assertNotIn("secret_internal", csv_text)
        self.assertIn(";", csv_text)

    def test_missing_receipt_status_has_french_table_label(self):
        table = prepare_table_dataframe(
            [{"status": "UNKNOWN", "block": 1, "signature": "tx1"}],
            table_kind="transactions", network="Ethereum",
        )
        self.assertEqual(table.iloc[0]["Statut"], "Statut inconnu")

    def test_package_version_uses_one_source(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(blockchain_lookup.__version__, __version__)
        self.assertEqual(__version__, "0.1.0")
        self.assertEqual(project["project"]["dynamic"], ["version"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"]["attr"],
            "blockchain_lookup.version.__version__",
        )

    def test_ci_runs_for_pr_main_and_release_tags(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("pull_request:", workflow)
        self.assertIn("push:", workflow)
        self.assertIn("- main", workflow)
        self.assertIn('- "v*"', workflow)
        for command in ("compileall", "ruff check .", "unittest discover -s tests -v"):
            self.assertIn(command, workflow)


if __name__ == "__main__":
    unittest.main()
