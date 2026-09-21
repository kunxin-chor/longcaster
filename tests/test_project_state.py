import importlib.util
import inspect
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class ProjectStateInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        package = types.ModuleType("longcaster_node_test")
        package.__path__ = [str(ROOT)]
        folder_paths = types.ModuleType("folder_paths")
        graph_utils = types.ModuleType("comfy_execution.graph_utils")
        graph_utils.ExecutionBlocker = object
        with patch.dict(sys.modules, {
            package.__name__: package,
            "folder_paths": folder_paths,
            "comfy_execution.graph_utils": graph_utils,
        }):
            spec = importlib.util.spec_from_file_location(f"{package.__name__}.nodes", ROOT / "nodes.py")
            cls.nodes = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.nodes)

    def test_connected_project_state_overrides_stale_widget_value(self):
        state = json.dumps({"project": "project_interface_selection"})
        self.assertEqual(
            self.nodes._project_from_state("stale_node_value", state),
            "project_interface_selection",
        )

    def test_missing_project_state_keeps_legacy_widget_value(self):
        self.assertEqual(self.nodes._project_from_state("legacy_project", None), "legacy_project")

    def test_project_dependent_nodes_expose_authoritative_state_input(self):
        for node in (self.nodes.LongCasterIdentityAnchor, self.nodes.LongCasterTimelineExport):
            with self.subTest(node=node.__name__):
                state_input = node.INPUT_TYPES()["optional"]["project_state"]
                self.assertEqual(state_input[0], "STRING")
                self.assertIs(state_input[1]["forceInput"], True)

    def test_project_dependent_entrypoints_accept_advertised_state_input(self):
        for node in (self.nodes.LongCasterIdentityAnchor, self.nodes.LongCasterTimelineExport):
            with self.subTest(node=node.__name__):
                entrypoint = getattr(node, node.FUNCTION)
                self.assertIn("project_state", inspect.signature(entrypoint).parameters)

    def test_card_lora_node_exposes_model_and_receipt(self):
        inputs = self.nodes.LongCasterCardLoras.INPUT_TYPES()["required"]
        self.assertEqual(inputs["model"][0], "MODEL")
        self.assertEqual(inputs["project_name"][0], "STRING")
        self.assertEqual(
            self.nodes.LongCasterCardLoras.RETURN_TYPES,
            ("MODEL", "LONGCASTER_LORA_STATE"),
        )

    def test_card_lora_loader_receives_a_string_path(self):
        source = (ROOT / "nodes.py").read_text(encoding="utf-8")
        self.assertIn(
            'comfy.utils.load_torch_file(\n                str(path),',
            source,
        )

    def test_card_lora_receipt_must_match_active_card(self):
        stack = [{
            "lora_name": "hero.safetensors", "strength_model": 0.7,
            "activation_words": "heroToken", "enabled": True,
        }]
        card = {"id": "card-one", "timeline_index": 0, "lora_stack": stack}
        manifest = {"project_name": "film", "cards": [card]}
        receipt = {
            "project": "film", "card_id": "card-one",
            "stack_fingerprint": self.nodes._card_lora_stack_fingerprint(card),
            "resolved_loras": [{**stack[0], "asset_sha256": "a" * 64}],
        }
        self.assertEqual(
            self.nodes._validate_card_lora_state(manifest, card, receipt),
            receipt["resolved_loras"],
        )
        receipt["card_id"] = "another-card"
        with self.assertRaisesRegex(self.nodes.ProjectError, "another active card"):
            self.nodes._validate_card_lora_state(manifest, card, receipt)

    def test_invalid_connected_state_fails_clearly(self):
        with self.assertRaisesRegex(self.nodes.ProjectError, "not valid LongCaster JSON"):
            self.nodes._project_from_state("fallback", "not-json")
        with self.assertRaisesRegex(self.nodes.ProjectError, "does not identify"):
            self.nodes._project_from_state("fallback", "{}")

    def test_new_project_widgets_are_appended_after_legacy_refine_widgets(self):
        samplers = types.ModuleType("comfy.samplers")
        samplers.SCHEDULER_NAMES = ["normal"]
        samplers.SAMPLER_NAMES = ["euler"]
        comfy = types.ModuleType("comfy")
        comfy.samplers = samplers
        with patch.dict(sys.modules, {"comfy": comfy, "comfy.samplers": samplers}):
            input_types = self.nodes.LongCasterProject.INPUT_TYPES()
            required = input_types["required"]
        self.assertEqual(
            list(required)[-4:],
            ["refine_cadence", "refine_sampler_name", "refine_seed_mode", "continuation_treatment"],
        )
        sampler_choices = required["refine_sampler_name"][0]
        self.assertIn("manual", sampler_choices)
        self.assertNotIn("manual", list(sampler_choices))
        self.assertEqual(input_types["optional"]["card_lora_state"][0], "LONGCASTER_LORA_STATE")
        self.assertEqual(input_types["optional"]["refine_card_lora_state"][0], "LONGCASTER_LORA_STATE")

    def test_displaced_refine_values_normalize_before_project_actions(self):
        normalize = self.nodes._normalize_shifted_treatment_widgets
        self.assertEqual(
            normalize("off", "manual", "inherit", "off", ["euler", "dpmpp_2m"]),
            ("manual", "euler", "inherit", "off"),
        )
        self.assertEqual(
            normalize("off", "manual", "euler", "inherit", ["euler", "dpmpp_2m"]),
            ("manual", "euler", "inherit", "off"),
        )
        self.assertEqual(
            normalize("euler", "inherit", "inherit", "manual", ["euler", "dpmpp_2m"]),
            ("manual", "euler", "inherit", "off"),
        )


if __name__ == "__main__":
    unittest.main()
