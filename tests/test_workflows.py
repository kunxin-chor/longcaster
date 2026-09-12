import json
from pathlib import Path
import unittest


WORKFLOWS = Path(__file__).resolve().parents[1] / "example_workflows"
WEB_EXTENSION = Path(__file__).resolve().parents[1] / "web" / "longcaster.js"
WORKFLOW_NAMES = (
    "longcaster_pdd_ref2va.json",
    "longcaster_pdd_refpatch.json",
    "longcaster_hybrid_ref2va.json",
    "longcaster_standard_t2va.json",
)
REFERENCE_WORKFLOW_NAMES = WORKFLOW_NAMES[:3]
PDD_REFERENCE_WORKFLOW_NAMES = WORKFLOW_NAMES[:2]


class WorkflowSerializationTests(unittest.TestCase):
    def _load(self, name):
        return json.loads((WORKFLOWS / name).read_text(encoding="utf-8"))

    def test_longcaster_widgets_include_seed_control_slot(self):
        for name in WORKFLOW_NAMES:
            with self.subTest(name=name):
                workflow = self._load(name)
                node = next(item for item in workflow["nodes"] if item["type"] == "LongCasterProject")
                values = node["widgets_values"]
                self.assertEqual(values[6], "fixed")
                self.assertIsInstance(values[7], int)   # width
                self.assertIsInstance(values[8], int)   # height
                self.assertIsInstance(values[9], int)   # steps
                self.assertEqual(values[10], "simple")
                self.assertEqual(values[11], "euler")
                self.assertIsInstance(values[12], bool)
                self.assertIn(values[13], ("match", "max"))
                self.assertIs(values[15], True)
                self.assertIs(values[16], True)
                self.assertIs(values[17], True)

    def test_pdd_workflow_enforces_partition_check(self):
        workflow = self._load("longcaster_pdd_ref2va.json")
        node = next(item for item in workflow["nodes"] if item["type"] == "MiniMaxH3PDDAccApply")
        self.assertEqual(node["widgets_values"][-1], "error")
        self.assertEqual(node["widgets_values"][0], "MiniMax-H3-Ref2VA-Acc-8Step.safetensors")

    def test_pdd_workflow_uses_explicit_attention_backend(self):
        workflow = self._load("longcaster_pdd_ref2va.json")
        nodes = {item["id"]: item for item in workflow["nodes"]}
        links = {item[0]: item for item in workflow["links"]}
        longcaster = next(item for item in workflow["nodes"] if item["type"] == "LongCasterProject")
        model_input = next(item for item in longcaster["inputs"] if item["name"] == "model")
        backend_link = links[model_input["link"]]
        backend = nodes[backend_link[1]]
        self.assertEqual(backend["type"], "ModelAttentionBackend")
        self.assertEqual(backend["widgets_values"], ["comfy kitchen attention"])
        backend_input = next(item for item in backend["inputs"] if item["name"] == "model")
        pdd_link = links[backend_input["link"]]
        self.assertEqual(nodes[pdd_link[1]]["type"], "MiniMaxH3PDDAccApply")

    def test_workflows_use_nvenc_preview_profile(self):
        for name in WORKFLOW_NAMES:
            with self.subTest(name=name):
                workflow = self._load(name)
                saver = next(item for item in workflow["nodes"] if item["type"] == "VHS_VideoCombine")
                values = saver["widgets_values"]
                self.assertEqual(values["format"], "video/longcaster_nvenc_h264-mp4.json")
                self.assertEqual(values["preset"], "p4")
                self.assertEqual(values["cq"], 17)
                self.assertIs(values["videopreview"]["paused"], True)

    def test_workflows_register_vhs_previews_in_the_project(self):
        for name in WORKFLOW_NAMES:
            with self.subTest(name=name):
                workflow = self._load(name)
                nodes = {item["id"]: item for item in workflow["nodes"]}
                links = {item[0]: item for item in workflow["links"]}
                registrar = next(item for item in workflow["nodes"] if item["type"] == "LongCasterRegisterPreview")
                filename_link = links[registrar["inputs"][0]["link"]]
                state_link = links[registrar["inputs"][1]["link"]]
                self.assertEqual(nodes[filename_link[1]]["type"], "VHS_VideoCombine")
                self.assertEqual(nodes[state_link[1]]["type"], "LongCasterProject")

    def test_every_workflow_uses_resolution_selector(self):
        for name in WORKFLOW_NAMES:
            with self.subTest(name=name):
                workflow = self._load(name)
                nodes = {item["id"]: item for item in workflow["nodes"]}
                links = {item[0]: item for item in workflow["links"]}
                selector = next(item for item in workflow["nodes"] if item["type"] == "ResolutionSelector")
                longcaster = next(item for item in workflow["nodes"] if item["type"] == "LongCasterProject")
                self.assertEqual(selector["widgets_values"], ["9:16 (Portrait Widescreen)", 0.5, 32])
                for input_name, output_slot in (("width", 0), ("height", 1)):
                    target = next(item for item in longcaster["inputs"] if item["name"] == input_name)
                    link = links[target["link"]]
                    self.assertEqual(nodes[link[1]]["type"], "ResolutionSelector")
                    self.assertEqual(link[2], output_slot)

    def test_hybrid_workflow_is_reference_patch_standard_sampling_baseline(self):
        workflow = self._load("longcaster_hybrid_ref2va.json")
        nodes = {item["id"]: item for item in workflow["nodes"]}
        links = {item[0]: item for item in workflow["links"]}
        loader = next(item for item in workflow["nodes"] if item["type"] == "MiniMaxH3HybridLoader")
        self.assertEqual(loader["widgets_values"][:3], [
            "minimax_h3_fl2va_int8_convrot.safetensors",
            "minimax_h3_ref2va_int8_convrot.safetensors",
            "ref2va_adaln_over_fl2va",
        ])
        longcaster = next(item for item in workflow["nodes"] if item["type"] == "LongCasterProject")
        model_input = next(item for item in longcaster["inputs"] if item["name"] == "model")
        self.assertEqual(nodes[links[model_input["link"]][1]]["type"], "MiniMaxH3HybridLoader")
        self.assertIsNone(next(item for item in longcaster["inputs"] if item["name"] == "sigmas")["link"])
        self.assertEqual(longcaster["widgets_values"][2], "ref2va")
        self.assertIs(longcaster["widgets_values"][12], False)
        self.assertTrue(any(item["type"] == "MMH3Put" for item in workflow["nodes"]))
        self.assertFalse(any(item["type"] == "MiniMaxH3PDDAccApply" for item in workflow["nodes"]))

    def test_refpatch_workflow_pairs_fl2va_patch_with_fl2va_pdd(self):
        workflow = self._load("longcaster_pdd_refpatch.json")
        nodes = {item["id"]: item for item in workflow["nodes"]}
        links = {item[0]: item for item in workflow["links"]}
        loader = next(item for item in workflow["nodes"] if item["type"] == "UNETLoader")
        patcher = next(item for item in workflow["nodes"] if item["type"] == "MiniMaxH3RefPatchLoader")
        pdd = next(item for item in workflow["nodes"] if item["type"] == "MiniMaxH3PDDAccApply")
        self.assertEqual(loader["widgets_values"][0], "minimax_h3_fl2va_int8_convrot.safetensors")
        self.assertEqual(patcher["widgets_values"], ["minimax_h3_ref_patch.safetensors", 1.0])
        self.assertEqual(pdd["widgets_values"][0], "MiniMax-H3-FL2VA-Acc-8Step.safetensors")
        patch_input = next(item for item in patcher["inputs"] if item["name"] == "model")
        pdd_input = next(item for item in pdd["inputs"] if item["name"] == "model")
        self.assertEqual(nodes[links[patch_input["link"]][1]]["type"], "UNETLoader")
        sigma_shift = nodes[links[pdd_input["link"]][1]]
        self.assertEqual(sigma_shift["type"], "MiniMaxH3SigmaShift")
        self.assertEqual(nodes[links[sigma_shift["inputs"][0]["link"]][1]]["type"], "MiniMaxH3RefPatchLoader")

    def test_pdd_reference_workflows_include_disabled_timeline_stitcher(self):
        for name in PDD_REFERENCE_WORKFLOW_NAMES:
            with self.subTest(name=name):
                workflow = self._load(name)
                exporter = next(item for item in workflow["nodes"] if item["type"] == "LongCasterTimelineExport")
                self.assertEqual(exporter["widgets_values"][1:], [
                    False, "video/longcaster_joined", False, "p4", 17, "192k"
                ])
                links = {item[0]: item for item in workflow["links"]}
                self.assertEqual(links[exporter["inputs"][0]["link"]][1], 3)
                self.assertEqual(links[exporter["inputs"][1]["link"]][1], 4)

    def test_reference_workflows_connect_project_state_to_every_project_dependent_node(self):
        for name in REFERENCE_WORKFLOW_NAMES:
            with self.subTest(name=name):
                workflow = self._load(name)
                nodes = {item["id"]: item for item in workflow["nodes"]}
                links = {item[0]: item for item in workflow["links"]}
                project = next(item for item in workflow["nodes"] if item["type"] == "LongCasterProject")
                state_output = next(item for item in project["outputs"] if item["name"] == "project_state")
                for node_type in ("LongCasterIdentityAnchor", "LongCasterTimelineExport"):
                    dependent = next(item for item in workflow["nodes"] if item["type"] == node_type)
                    state_input = next(item for item in dependent["inputs"] if item["name"] == "project_state")
                    link = links[state_input["link"]]
                    self.assertEqual(nodes[link[1]]["type"], "LongCasterProject")
                    self.assertEqual(link[2], 2)
                    self.assertIn(state_input["link"], state_output["links"])

    def test_reference_workflows_include_identity_anchor_selector(self):
        for name in REFERENCE_WORKFLOW_NAMES:
            with self.subTest(name=name):
                workflow = self._load(name)
                selector = next(item for item in workflow["nodes"] if item["type"] == "LongCasterIdentityAnchor")
                self.assertEqual(selector["widgets_values"][1:6], [
                    "inspect", "1", 0, "<Subject 1>", "identity checkpoint"
                ])
                self.assertEqual(selector["widgets_values"][7:], ["face_only", ""])
                links = {item[0]: item for item in workflow["links"]}
                self.assertEqual(links[selector["inputs"][0]["link"]][1], 3)
                self.assertEqual(selector["outputs"][2]["name"], "selected_frame")

    def test_web_extension_exposes_stage_2c_cards_workspace(self):
        source = WEB_EXTENSION.read_text(encoding="utf-8")
        self.assertIn('"Open Cards Interface"', source)
        for section in (
            "subject_definitions", "summary", "retention_analysis", "detailed_description",
            "overall_soundscape", "non_diegetic_music",
        ):
            self.assertIn(f'["{section}"', source)
        for endpoint in (
            "/longcaster/cards/projects", "/longcaster/cards/state",
            "/longcaster/cards/card", "/longcaster/cards/copy", "/longcaster/cards/preview",
            "/longcaster/cards/project", "/longcaster/cards/anchor", "/longcaster/cards/identity",
        ):
            self.assertIn(endpoint, source)
        for control in (
            "New Project", "Identity Checkpoints", "Reference Resources",
            "Continue previous card (direct MMH3)", "Historical flat prompt",
            "Megapixels (MP)", "PROJECT_RESOLUTION_MULTIPLE = 32",
            "loadEpoch", "loadProjects(body.project, false)",
            "Paste Full Prompt", "import_prompt: pasted",
            "Stop Generation / Unlock", "execution_start", "execution_success",
            "execution_interrupted", "execution_error", "longcaster_log",
            "Show Execution Log", "Hide Execution Log", "longcaster.executionLogExpanded",
            "LongCasterRegisterPreview", "longcaster_preview", "preview_version",
        ):
            self.assertIn(control, source)


if __name__ == "__main__":
    unittest.main()
