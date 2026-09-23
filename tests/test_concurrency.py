import logging
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from blockchain_lookup.engines.bitcoin import BitcoinSearchError
from blockchain_lookup.engines.ethereum import EthereumSearchError
from blockchain_lookup.engines.solana import SolanaSearchError
from blockchain_lookup.runtime.concurrency import (
    DEFAULT_MAX_CONCURRENT_SEARCHES,
    DEFAULT_QUEUE_TIMEOUT_SECONDS,
    MAX_CONCURRENT_SEARCHES_ENV,
    QUEUE_TIMEOUT_ENV,
    SearchLimiter,
    SearchQueueTimeoutError,
    configured_queue_timeout,
    configured_search_limit,
    finish_session_search,
    try_start_session_search,
)
from blockchain_lookup.ui.downloads import to_csv_bytes
from blockchain_lookup.runtime.result_limits import ResultLimitExceeded


ROOT = Path(__file__).resolve().parents[1]


class SearchLimiterTests(unittest.TestCase):
    def test_queue_timeout_configuration_defaults_and_invalid_values(self):
        for raw, expected in (
            (None, DEFAULT_QUEUE_TIMEOUT_SECONDS),
            ("2.5", 2.5),
            ("invalid", DEFAULT_QUEUE_TIMEOUT_SECONDS),
            ("0", DEFAULT_QUEUE_TIMEOUT_SECONDS),
            ("-1", DEFAULT_QUEUE_TIMEOUT_SECONDS),
            ("nan", DEFAULT_QUEUE_TIMEOUT_SECONDS),
            ("inf", DEFAULT_QUEUE_TIMEOUT_SECONDS),
        ):
            with self.subTest(raw=raw):
                environ = {} if raw is None else {QUEUE_TIMEOUT_ENV: raw}
                self.assertEqual(configured_queue_timeout(environ), expected)

    def test_queue_timeout_does_not_leak_a_slot(self):
        limiter = SearchLimiter(1, wait_timeout=0.01)
        queued = []
        with limiter.slot(search_id="first", network="Solana"):
            with self.assertRaises(SearchQueueTimeoutError) as caught:
                with limiter.slot(
                    search_id="second", network="Ethereum", on_queued=lambda: queued.append(True)
                ):
                    self.fail("Timed-out search entered the critical section")
        self.assertEqual(queued, [True])
        self.assertIn("Serveur actuellement occupé", str(caught.exception))
        with limiter.slot(search_id="third", network="Bitcoin"):
            pass

    def test_configuration_defaults_and_invalid_values(self):
        for raw, expected in (
            (None, DEFAULT_MAX_CONCURRENT_SEARCHES),
            ("2", 2),
            ("not-a-number", DEFAULT_MAX_CONCURRENT_SEARCHES),
            ("0", DEFAULT_MAX_CONCURRENT_SEARCHES),
            ("-1", DEFAULT_MAX_CONCURRENT_SEARCHES),
        ):
            with self.subTest(raw=raw):
                environ = {} if raw is None else {MAX_CONCURRENT_SEARCHES_ENV: raw}
                with self.assertLogs(
                    "blockchain_lookup.runtime.concurrency", level="WARNING"
                ) if raw in {"not-a-number", "0", "-1"} else self.assertNoLogs(
                    "blockchain_lookup.runtime.concurrency", level="WARNING"
                ):
                    self.assertEqual(configured_search_limit(environ), expected)

    def test_two_searches_run_and_third_waits_until_release(self):
        limiter = SearchLimiter(2)
        release_first = threading.Event()
        release_second = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()
        third_queued = threading.Event()
        third_started = threading.Event()

        def search(name, started, release):
            with limiter.slot(search_id=name, network="Solana"):
                started.set()
                self.assertTrue(release.wait(5))

        def third_search():
            with limiter.slot(
                search_id="third",
                network="Ethereum",
                on_queued=third_queued.set,
            ):
                third_started.set()

        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(search, "first", first_started, release_first)
            second = pool.submit(search, "second", second_started, release_second)
            try:
                self.assertTrue(first_started.wait(5))
                self.assertTrue(second_started.wait(5))
                third = pool.submit(third_search)
                self.assertTrue(third_queued.wait(5))
                self.assertFalse(third_started.is_set())
                release_first.set()
                self.assertTrue(third_started.wait(5))
                first.result(timeout=5)
                third.result(timeout=5)
            finally:
                release_first.set()
                release_second.set()
            second.result(timeout=5)

    def test_exception_releases_slot(self):
        limiter = SearchLimiter(1)
        with self.assertRaisesRegex(RuntimeError, "RPC failed"):
            with limiter.slot(search_id="failed", network="Bitcoin"):
                raise RuntimeError("RPC failed")

        with limiter.slot(search_id="next", network="Bitcoin"):
            pass

    def test_session_flags_and_results_are_isolated(self):
        first = {"lookup_result": {"matches": ["first"]}}
        second = {"lookup_result": {"matches": ["second"]}}

        self.assertTrue(try_start_session_search(first))
        self.assertFalse(try_start_session_search(first))
        self.assertTrue(try_start_session_search(second))
        self.assertEqual(first["lookup_result"], {"matches": ["first"]})
        self.assertEqual(second["lookup_result"], {"matches": ["second"]})

        finish_session_search(first)
        self.assertFalse(first["search_running"])
        self.assertTrue(second["search_running"])
        self.assertTrue(try_start_session_search(first))
        finish_session_search(first)
        finish_session_search(second)

    def test_concurrent_claims_in_one_session_start_only_once(self):
        state = {}
        barrier = threading.Barrier(2)

        def claim():
            barrier.wait(timeout=5)
            return try_start_session_search(state)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(claim) for _ in range(2)]
            self.assertEqual(sorted(job.result(timeout=5) for job in results), [False, True])
        finish_session_search(state)


class StreamlitConcurrencyTests(unittest.TestCase):
    def test_result_limit_error_is_visible_without_retaining_partial_result(self):
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
        with patch(
            "blockchain_lookup.ui.app.search_solana_window",
            side_effect=ResultLimitExceeded("Recherche interrompue : limite atteinte."),
        ):
            app.button[0].click().run(timeout=20)

        self.assertEqual(len(app.exception), 0)
        self.assertFalse(app.session_state["search_running"])
        self.assertNotIn("lookup_result", app.session_state)
        self.assertTrue(any("limite atteinte" in item.value for item in app.error))

    def test_queue_timeout_is_shown_and_session_flag_is_cleared(self):
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
        limiter = SearchLimiter(1, wait_timeout=0.01)
        with limiter.slot(search_id="holder", network="Bitcoin"):
            with patch("blockchain_lookup.ui.app.SEARCH_LIMITER", limiter):
                app.button[0].click().run(timeout=20)

        self.assertEqual(len(app.exception), 0)
        self.assertFalse(app.session_state["search_running"])
        self.assertTrue(any("Serveur actuellement occupé" in item.value for item in app.error))

    def test_queued_search_explains_wait_then_start_in_ui(self):
        class QueueOnce:
            @contextmanager
            def slot(self, *, search_id, network, on_queued):
                on_queued()
                yield

        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
        with patch(
            "blockchain_lookup.ui.app.SEARCH_LIMITER", QueueOnce()
        ), patch(
            "blockchain_lookup.ui.app.search_solana_window",
            side_effect=SolanaSearchError("RPC inaccessible"),
        ):
            app.button[0].click().run(timeout=20)

        messages = "\n".join(item.value for item in app.text)
        self.assertIn("Serveur occupé", messages)
        self.assertIn("Recherche démarrée.", messages)
        self.assertEqual(len(app.exception), 0)

    def test_two_streamlit_sessions_keep_their_state_separate(self):
        first = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
        second = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
        first.session_state["search_running"] = True
        first.session_state["lookup_result"] = {"network": "other", "marker": "first"}
        first.run(timeout=20)
        second.run(timeout=20)

        self.assertTrue(first.button[0].disabled)
        self.assertFalse(second.button[0].disabled)
        self.assertEqual(first.session_state["lookup_result"]["marker"], "first")
        self.assertNotIn("lookup_result", second.session_state)
        self.assertNotIn("search_running", second.session_state)

    def test_csv_format_is_unchanged_without_a_global_result_cache(self):
        self.assertEqual(
            to_csv_bytes([{"asset": "BTC", "amount": "1.5"}]).decode("utf-8-sig"),
            f"asset;amount{os.linesep}BTC;1.5{os.linesep}",
        )

    def test_successful_search_stores_result_and_clears_running_flag(self):
        app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
        result = {"network": "other", "target_amount": None, "matches": []}
        with patch(
            "blockchain_lookup.ui.app.SEARCH_LIMITER", SearchLimiter(1)
        ), patch(
            "blockchain_lookup.ui.app.search_solana_window", return_value=result
        ) as engine:
            app.button[0].click().run(timeout=20)

        self.assertEqual(len(app.exception), 0)
        self.assertEqual(engine.call_count, 1)
        self.assertIs(app.session_state["lookup_result"], result)
        self.assertFalse(app.session_state["search_running"])

    def test_search_guard_covers_all_three_networks_and_releases_on_engine_error(self):
        cases = (
            ("Solana", "search_solana_window", SolanaSearchError),
            ("Ethereum", "search_ethereum_window", EthereumSearchError),
            ("Bitcoin", "search_bitcoin_window", BitcoinSearchError),
        )
        for network, engine_name, error_type in cases:
            with self.subTest(network=network):
                app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=20)
                self.assertEqual(len(app.exception), 0)
                app.segmented_control[0].set_value(network)
                app.run(timeout=20)
                limiter = SearchLimiter(1)
                with patch(
                    "blockchain_lookup.ui.app.SEARCH_LIMITER", limiter
                ), patch(
                    f"blockchain_lookup.ui.app.{engine_name}",
                    side_effect=error_type("RPC inaccessible"),
                ) as engine, self.assertLogs(
                    "blockchain_lookup", level=logging.INFO
                ) as logs:
                    app.button[0].click().run(timeout=20)

                self.assertEqual(len(app.exception), 0)
                self.assertEqual(engine.call_count, 1)
                self.assertFalse(app.session_state["search_running"])
                self.assertIn("Search slot acquired", " ".join(logs.output))
                self.assertIn("Search slot released", " ".join(logs.output))
                self.assertIn("Search failed", " ".join(logs.output))
                with limiter.slot(search_id="after", network=network):
                    pass


if __name__ == "__main__":
    unittest.main()
