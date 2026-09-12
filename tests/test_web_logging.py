import importlib.util
import logging
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class WebLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = []

        class ServerInstance:
            def send_sync(_self, event, payload):
                cls.events.append((event, payload))

        package = types.ModuleType("longcaster_routes_test")
        package.__path__ = [str(ROOT / "longcaster")]
        folder_paths = types.ModuleType("folder_paths")
        folder_paths.get_output_directory = lambda: str(ROOT / "test-output")
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.web = types.SimpleNamespace()
        server = types.ModuleType("server")
        server.PromptServer = type("PromptServer", (), {"instance": ServerInstance()})
        with patch.dict(sys.modules, {
            package.__name__: package,
            "aiohttp": aiohttp,
            "folder_paths": folder_paths,
            "server": server,
        }):
            spec = importlib.util.spec_from_file_location(
                f"{package.__name__}.routes", ROOT / "longcaster" / "routes.py"
            )
            cls.routes = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.routes)

    def setUp(self):
        self.events.clear()

    def test_longcaster_log_record_is_forwarded_to_browser_clients(self):
        handler = self.routes._LongCasterWebLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            "longcaster", logging.WARNING, __file__, 1,
            "render %s", ("warning",), None,
        )

        handler.emit(record)

        self.assertEqual(len(self.events), 1)
        event, payload = self.events[0]
        self.assertEqual(event, "longcaster_log")
        self.assertEqual(payload["level"], "warning")
        self.assertEqual(payload["message"], "render warning")
        self.assertIsInstance(payload["timestamp"], float)

    def test_card_summary_versions_only_the_current_artifact_preview(self):
        card = {
            "id": "card-id", "timeline_index": 0, "artifact_number": 1,
            "status": "DRAFT", "artifact_sha256": "new-artifact",
            "preview": {
                "asset_path": "previews/card.mp4",
                "asset_sha256": "preview-hash",
                "source_artifact_sha256": "old-artifact",
            },
        }
        stale = self.routes._card_summary(card)
        self.assertIs(stale["preview_available"], False)
        self.assertIsNone(stale["preview_version"])

        card["preview"]["source_artifact_sha256"] = "new-artifact"
        current = self.routes._card_summary(card)
        self.assertIs(current["preview_available"], True)
        self.assertEqual(current["preview_version"], "preview-hash")


if __name__ == "__main__":
    unittest.main()
