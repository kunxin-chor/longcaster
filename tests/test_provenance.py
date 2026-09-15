import unittest

from longcaster.provenance import capture_generation_provenance, provenance_for_display


class GenerationProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.prompt = {
            "1": {
                "class_type": "UNETLoader",
                "inputs": {"unet_name": "minimax_h3.safetensors", "weight_dtype": "default"},
            },
            "2": {
                "class_type": "LoraLoaderModelOnly",
                "_meta": {"title": "Character LoRA"},
                "inputs": {"model": ["1", 0], "lora_name": "hero.safetensors", "strength_model": 0.8},
            },
            "3": {
                "class_type": "PDDModelPatch",
                "inputs": {"model": ["2", 0], "shift": 3.0},
            },
            "4": {
                "class_type": "CLIPLoader",
                "inputs": {"clip_name": "clip.safetensors", "api_key": "must-not-leak"},
            },
            "5": {"class_type": "VAELoader", "inputs": {"vae_name": "video.vae"}},
            "6": {"class_type": "VAELoader", "inputs": {"vae_name": "audio.vae"}},
            "7": {"class_type": "PDDApply", "inputs": {"model": ["3", 0], "steps": 4}},
            "99": {
                "class_type": "LongCasterProject",
                "inputs": {
                    "model": ["3", 0], "clip": ["4", 0], "video_vae": ["5", 0],
                    "audio_vae": ["6", 0], "sigmas": ["7", 1], "refine_model": ["42", 0],
                },
            },
            "42": {"class_type": "UnusedRefineLoader", "inputs": {"model_name": "refine.safetensors"}},
        }

    def test_captures_generation_branches_and_display_summary(self):
        result = capture_generation_provenance(self.prompt, "99")
        self.assertIsNotNone(result)
        self.assertEqual(set(result["roots"]), {"model", "clip", "video_vae", "audio_vae", "sigmas"})
        self.assertNotIn("42", {node["node_id"] for node in result["nodes"]})
        self.assertEqual(result["summary"]["models"][0]["name"], "minimax_h3.safetensors")
        self.assertEqual(result["summary"]["loras"][0]["name"], "hero.safetensors")
        self.assertEqual(result["summary"]["loras"][0]["settings"]["strength_model"], 0.8)
        self.assertEqual(result["summary"]["patches"][0]["class_type"], "PDDModelPatch")
        self.assertEqual(len(result["graph_sha256"]), 64)
        self.assertGreater(result["stored_bytes"], 0)
        self.assertLess(result["stored_bytes"], 10_000)

        display = provenance_for_display({"upstream_provenance": result})
        self.assertEqual(display["node_count"], 7)
        self.assertEqual(display["models"][0]["name"], "minimax_h3.safetensors")

    def test_redacts_secrets_and_returns_none_without_execution_context(self):
        result = capture_generation_provenance(self.prompt, 99)
        clip = next(node for node in result["nodes"] if node["node_id"] == "4")
        self.assertEqual(clip["inputs"]["api_key"], "[redacted]")
        self.assertTrue(result["truncated"])
        self.assertIsNone(capture_generation_provenance(None, "99"))
        self.assertIsNone(capture_generation_provenance(self.prompt, "missing"))

    def test_non_link_two_item_lists_remain_widget_values(self):
        self.prompt["1"]["inputs"]["range"] = [544, 960]
        result = capture_generation_provenance(self.prompt, "99")
        loader = next(node for node in result["nodes"] if node["node_id"] == "1")
        self.assertEqual(loader["inputs"]["range"], [544, 960])


if __name__ == "__main__":
    unittest.main()
