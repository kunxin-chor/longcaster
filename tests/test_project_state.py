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

    def test_invalid_connected_state_fails_clearly(self):
        with self.assertRaisesRegex(self.nodes.ProjectError, "not valid LongCaster JSON"):
            self.nodes._project_from_state("fallback", "not-json")
        with self.assertRaisesRegex(self.nodes.ProjectError, "does not identify"):
            self.nodes._project_from_state("fallback", "{}")


if __name__ == "__main__":
    unittest.main()
