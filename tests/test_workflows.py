import json
from pathlib import Path
import unittest


WORKFLOWS = Path(__file__).resolve().parents[1] / "example_workflows"


class WorkflowSerializationTests(unittest.TestCase):
    def _load(self, name):
        return json.loads((WORKFLOWS / name).read_text(encoding="utf-8"))

    def test_longcaster_widgets_include_seed_control_slot(self):
        for name in ("longcaster_pdd_ref2va.json", "longcaster_standard_t2va.json"):
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
        for name in ("longcaster_pdd_ref2va.json", "longcaster_standard_t2va.json"):
            with self.subTest(name=name):
                workflow = self._load(name)
                saver = next(item for item in workflow["nodes"] if item["type"] == "VHS_VideoCombine")
                values = saver["widgets_values"]
                self.assertEqual(values["format"], "video/longcaster_nvenc_h264-mp4.json")
                self.assertEqual(values["preset"], "p4")
                self.assertEqual(values["cq"], 17)

    def test_pdd_workflow_includes_disabled_timeline_export(self):
        workflow = self._load("longcaster_pdd_ref2va.json")
        exporter = next(item for item in workflow["nodes"] if item["type"] == "LongCasterTimelineExport")
        self.assertEqual(exporter["widgets_values"], [
            "episode_01", False, "video/longcaster_joined", False, "p4", 17, "192k"
        ])
        links = {item[0]: item for item in workflow["links"]}
        self.assertEqual(links[exporter["inputs"][0]["link"]][1], 3)
        self.assertEqual(links[exporter["inputs"][1]["link"]][1], 4)


if __name__ == "__main__":
    unittest.main()
