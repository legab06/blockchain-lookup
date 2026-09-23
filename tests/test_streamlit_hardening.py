import json
import logging
import re
import tomllib
import unittest
from pathlib import Path

from streamlit_errors import GENERIC_SEARCH_ERROR, log_unexpected_search_error


ROOT = Path(__file__).resolve().parents[1]


class StreamlitHardeningTests(unittest.TestCase):
    def test_streamlit_theme_is_valid_and_light(self):
        config_path = ROOT / ".streamlit" / "config.toml"
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))

        self.assertFalse((ROOT / "config.toml").exists())
        self.assertEqual(config["theme"]["base"], "light")
        self.assertEqual(config["theme"]["primaryColor"], "#7C3AED")
        self.assertEqual(config["theme"]["backgroundColor"], "#FFFFFF")
        self.assertEqual(
            config["theme"]["secondaryBackgroundColor"],
            "#F5F7FA",
        )
        self.assertEqual(config["theme"]["textColor"], "#1F2937")
        self.assertEqual(config["theme"]["font"], "sans-serif")

    def test_devcontainer_uses_streamlit_safe_server_defaults(self):
        path = ROOT / ".devcontainer" / "devcontainer.json"
        text = re.sub(
            r"(?m)^\s*//.*$",
            "",
            path.read_text(encoding="utf-8"),
        )
        config = json.loads(text)

        command = config["postAttachCommand"]["server"]
        self.assertEqual(command, "streamlit run app.py")
        self.assertNotIn("enableCORS", command)
        self.assertNotIn("enableXsrfProtection", command)

    def test_unexpected_error_is_logged_but_redacted_from_ui_message(self):
        logger = logging.getLogger("test.streamlit_errors")
        with self.assertLogs(logger, level="ERROR") as captured:
            try:
                raise RuntimeError("private endpoint and secret detail")
            except RuntimeError as exc:
                message = log_unexpected_search_error(logger, exc)

        self.assertEqual(message, GENERIC_SEARCH_ERROR)
        self.assertNotIn("private endpoint", message)
        self.assertNotIn("secret detail", message)
        self.assertIn(
            "RuntimeError: private endpoint and secret detail",
            captured.output[0],
        )
        self.assertIn("Traceback", captured.output[0])

    def test_app_does_not_render_exception_object(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")

        self.assertNotIn("st.exception(", source)
        self.assertIn("log_unexpected_search_error", source)


if __name__ == "__main__":
    unittest.main()