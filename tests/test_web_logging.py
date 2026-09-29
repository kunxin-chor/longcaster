import importlib.util
import asyncio
import logging
from pathlib import Path
import sys
import types
import tempfile
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

    def test_timeline_api_returns_membership_and_rejects_stale_writes(self):
        from longcaster.project import ProjectError, ProjectStore

        class Request:
            def __init__(self, payload):
                self.payload = payload

            async def json(self):
                return self.payload

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProjectStore(root, "organization")
            initial = store.create(prompt="", duration_seconds=5, seed=1, width=544, height=960, generation_mode="t2va")
            with (
                patch.object(self.routes, "_projects_root", return_value=root),
                patch.object(self.routes, "ProjectStore", ProjectStore),
                patch.object(self.routes, "ProjectError", ProjectError),
                patch.object(self.routes.web, "json_response", create=True,
                             side_effect=lambda data, status=200: (status, data)),
            ):
                status, created = asyncio.run(self.routes.manage_organization_timeline(Request({
                    "project": "organization", "revision": initial["revision"], "action": "create", "name": "Scene 1",
                })))
                self.assertEqual(status, 200)
                timeline_id = created["organization_timelines"][0]["id"]
                request = Request({
                    "project": "organization", "revision": created["revision"], "action": "assign",
                    "timeline_id": timeline_id, "card_id": initial["active_card_id"],
                })
                status, assigned = asyncio.run(self.routes.manage_organization_timeline(request))
                self.assertEqual(status, 200)
                self.assertEqual(assigned["cards"][0]["organization_timeline_id"], timeline_id)
                status, folders = asyncio.run(self.routes.manage_generation_folder(Request({
                    "project": "organization", "revision": assigned["revision"], "action": "create", "name": "Kitchen",
                })))
                self.assertEqual(status, 200)
                folder_id = folders["generation_folders"][0]["id"]
                status, grouped = asyncio.run(self.routes.manage_generation_folder(Request({
                    "project": "organization", "revision": folders["revision"], "action": "assign",
                    "folder_id": folder_id, "card_id": initial["active_card_id"],
                })))
                self.assertEqual(status, 200)
                self.assertEqual(grouped["cards"][0]["generation_folder_id"], folder_id)
                self.assertEqual(grouped["cards"][0]["organization_timeline_id"], timeline_id)
                status, error = asyncio.run(self.routes.manage_organization_timeline(request))
                self.assertEqual(status, 409)
                self.assertIn("stale project revision", error["error"])
                status, blank = asyncio.run(self.routes.create_blank_card(Request({
                    "project": "organization", "revision": grouped["revision"], "folder_id": folder_id,
                })))
                self.assertEqual(status, 200)
                new_card = blank["cards"][-1]
                self.assertEqual(new_card["id"], blank["active_card_id"])
                self.assertEqual(new_card["generation_folder_id"], folder_id)
                self.assertIsNone(new_card["generation_parent_id"])
                self.assertTrue(blank["resolution_editable"])
                self.assertTrue(blank["generation_mode_editable"])
                status, resumed = asyncio.run(self.routes.activate_cards_card(Request({
                    "project": "organization", "revision": blank["revision"], "card_id": initial["active_card_id"],
                })))
                self.assertEqual(status, 200)
                self.assertEqual(resumed["active_card_id"], initial["active_card_id"])
                status, error = asyncio.run(self.routes.create_blank_card(Request({
                    "project": "organization", "revision": blank["revision"], "folder_id": folder_id,
                })))
                self.assertEqual(status, 409)
                self.assertIn("stale project revision", error["error"])

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

    def test_card_summary_exposes_latest_refine_and_source_choice(self):
        card = {
            "id": "card-id", "timeline_index": 0, "artifact_number": 1,
            "status": "ACCEPTED", "artifact_sha256": "master-hash",
            "refine_enabled": True,
            "continuation_source_preference": "derivative",
            "derivatives": [
                {"id": "old", "type": "refine", "status": "FAILED"},
                {"id": "ready", "type": "refine", "status": "READY"},
            ],
        }
        summary = self.routes._card_summary(card)
        self.assertEqual(summary["continuation_refine"]["id"], "ready")
        self.assertEqual(summary["continuation_source_preference"], "derivative")
        self.assertTrue(summary["refine_enabled"])

    def test_card_summary_exposes_validated_and_invalidated_render_state(self):
        card = {
            "id": "card-id", "timeline_index": 0, "artifact_number": 1,
            "status": "DRAFT", "artifact_sha256": "render-hash",
        }
        self.assertEqual(self.routes._card_summary(card)["render_validity"], "VALIDATED")
        card.update({"status": "INVALIDATED", "artifact_sha256": None})
        self.assertEqual(self.routes._card_summary(card)["render_validity"], "INVALIDATED")

    def test_hires_summary_is_separate_and_keeps_previous_ready_after_failure(self):
        card = {"id": "card", "timeline_index": 0, "artifact_number": 1, "status": "ACCEPTED",
                "derivatives": [{"id": "refine", "type": "refine", "status": "READY"},
                                {"id": "hires", "type": "hires_refine", "status": "READY"},
                                {"id": "failed", "type": "hires_refine", "status": "FAILED"}]}
        summary = self.routes._card_summary(card)
        self.assertEqual(summary["continuation_refine"]["id"], "refine")
        self.assertEqual(summary["hires"]["id"], "failed")
        self.assertEqual(summary["ready_hires"]["id"], "hires")

    def test_card_summary_previews_project_prompt_templates(self):
        card = {
            "id": "card-id", "timeline_index": 0, "artifact_number": 1,
            "status": "EMPTY", "assembled_prompt": "<lighting> and <Picture 1> plus <missing>",
        }
        summary = self.routes._card_summary(
            card, [{"name": "lighting", "text": "soft window light"}]
        )
        self.assertEqual(
            summary["resolved_prompt"],
            "soft window light and <Picture 1> plus <missing>",
        )
        self.assertEqual(summary["prompt_templates_used"][0]["name"], "lighting")
        self.assertEqual(summary["unknown_template_tokens"], ["missing"])

    def test_card_summary_exposes_archived_take_prompt_for_diffing(self):
        card = {
            "id": "card-id", "timeline_index": 0, "artifact_number": 1,
            "status": "DRAFT", "artifact_sha256": "render-hash",
            "selected_draft_take_id": "take-id",
            "draft_takes": [{
                "id": "take-id", "attempt": 1, "artifact_sha256": "render-hash",
                "ref_image_size": "max",
                "assembled_prompt": "archived exact prompt", "prompt_hash": "prompt-hash",
                "recipe": {"upstream_provenance": {
                    "nodes": [{"node_id": "1"}], "graph_sha256": "graph-hash",
                    "stored_bytes": 321, "truncated": False,
                    "summary": {
                        "models": [{"name": "h3.safetensors"}],
                        "loras": [{"name": "look.safetensors"}],
                        "patches": [{"class_type": "PDDPatch"}],
                    },
                }},
            }],
        }
        take = self.routes._card_summary(card)["draft_takes"][0]
        self.assertEqual(take["assembled_prompt"], "archived exact prompt")
        self.assertTrue(take["selected"])
        self.assertEqual(take["generation_setup"]["models"][0]["name"], "h3.safetensors")
        self.assertEqual(take["generation_setup"]["node_count"], 1)
        self.assertEqual(take["ref_image_size"], "max")
        self.assertEqual(take["settings"]["ref_image_size"], "max")

    def test_card_summary_exposes_frozen_picture_sources_for_prompt_rail(self):
        identity = {"active": True, "anchor_id": "anchor-id"}
        card_reference = {"id": "reference-id", "enabled": True}
        references = {"resources": [{"id": "global-id", "kind": "image"}]}
        card = {
            "id": "card-id", "timeline_index": 0, "artifact_number": 1,
            "status": "ACCEPTED", "artifact_sha256": "render-hash",
            "recipe": {"identity_anchor": identity, "card_reference": card_reference},
            "reference_set": references,
            "selected_draft_take_id": "take-id",
            "draft_takes": [{
                "id": "take-id", "attempt": 1, "artifact_sha256": "render-hash",
                "reference_set": references,
                "recipe": {"identity_anchor": identity, "card_reference": card_reference},
            }],
        }
        summary = self.routes._card_summary(card)
        self.assertEqual(summary["identity_anchor_resolved"], identity)
        self.assertEqual(summary["card_reference_resolved"], card_reference)
        self.assertEqual(summary["draft_takes"][0]["settings"]["identity_anchor"], identity)
        self.assertEqual(summary["draft_takes"][0]["settings"]["card_reference"], card_reference)
        self.assertEqual(summary["draft_takes"][0]["settings"]["references"], references)


if __name__ == "__main__":
    unittest.main()
