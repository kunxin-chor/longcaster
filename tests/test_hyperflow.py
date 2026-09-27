import unittest

from longcaster.hyperflow import (
    compensate_hyperflow_continuation_prompt,
    detect_hyperflow,
)


class _MarkedForward:
    _hyperflow_two_time = True


class _Model:
    def __init__(self, marked=False):
        self.object_patches = (
            {"diffusion_model.time_embedder.forward": _MarkedForward()}
            if marked else {}
        )


def _provenance(class_type="ApplyHyperFlow"):
    return {
        "branches": {"model": ["2"]},
        "nodes": [
            {"node_id": "1", "class_type": "UNETLoader"},
            {"node_id": "2", "class_type": class_type},
        ],
    }


class HyperFlowCompensationTests(unittest.TestCase):
    def test_detects_full_model_patch(self):
        result = detect_hyperflow(_Model(marked=True), None)
        self.assertTrue(result["detected"])
        self.assertEqual(result["sources"], ["model_two_time_patch"])

    def test_detects_apply_node_for_pruned_variant(self):
        result = detect_hyperflow(_Model(), _provenance("ApplyHyperFlowAdvanced"))
        self.assertTrue(result["detected"])
        self.assertEqual(result["sources"], ["upstream_apply_node"])

    def test_ignores_hyperflow_node_outside_model_branch(self):
        provenance = _provenance()
        provenance["branches"]["model"] = ["1"]
        self.assertFalse(detect_hyperflow(_Model(), provenance)["detected"])

    def test_direct_continuation_shifts_card_timestamps_and_adds_boundary(self):
        prompt, report = compensate_hyperflow_continuation_prompt(
            "At 00:00 she turns. At 00:02.500 she speaks.",
            model=_Model(marked=True),
            provenance=None,
            continuation_strategy="direct_mmh3",
            context_frames=39,
        )
        self.assertTrue(report["applied"])
        self.assertEqual(report["offset_seconds"], 1.625)
        self.assertEqual(report["explicit_timestamps_shifted"], 2)
        self.assertIn("00:01.625 she turns", prompt)
        self.assertIn("00:04.125 she speaks", prompt)
        self.assertTrue(prompt.startswith("The interval from 00:00.000 through 00:01.625"))

    def test_compensation_is_limited_to_hyperflow_direct_continuation(self):
        original = "At 00:00 she turns."
        for model, strategy, frames in (
            (_Model(), "direct_mmh3", 39),
            (_Model(marked=True), "image_restart", 0),
            (_Model(marked=True), "independent", 0),
        ):
            with self.subTest(strategy=strategy, detected=bool(model.object_patches)):
                prompt, report = compensate_hyperflow_continuation_prompt(
                    original,
                    model=model,
                    provenance=None,
                    continuation_strategy=strategy,
                    context_frames=frames,
                )
                self.assertEqual(prompt, original)
                self.assertFalse(report["applied"])


if __name__ == "__main__":
    unittest.main()
