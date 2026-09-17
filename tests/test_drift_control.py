import sys
import types
import unittest

try:
    import torch
    from longcaster.drift_control import (
        apply_dynamic_prefix_mask,
        install_drift_control_model,
        matched_noise_ratio,
        temporal_prefix_weights,
    )
except ModuleNotFoundError:
    torch = None


@unittest.skipIf(torch is None, "PyTorch required for drift-control tensor tests")
class DriftControlTests(unittest.TestCase):
    def test_exact_prefix_recipe_and_schedule_ratio(self):
        self.assertEqual(
            temporal_prefix_weights(),
            (1.0,) * 8 + (0.75, 0.5, 0.25, 0.0),
        )
        self.assertAlmostEqual(matched_noise_ratio(3.0, [12.0, 3.0, 1.0, 0.0]), 1.0 / 3.0)

    def test_dynamic_mask_changes_video_only_and_quantizes_h3_mask(self):
        original = torch.full((1, 1, 35), 0.42)
        packed, h3 = apply_dynamic_prefix_mask(original, (1, 2, 15, 1, 1), 12, 0.5)
        self.assertTrue(torch.equal(packed[..., 30:], original[..., 30:]))
        self.assertTrue(torch.equal(original, torch.full_like(original, 0.42)))
        expected = torch.tensor((1.0,) * 8 + (0.75, 0.5, 0.25, 0.0)) * 0.5
        self.assertTrue(torch.allclose(packed[:, 0, :15].reshape(1, 15)[0, :12], expected))
        self.assertTrue(torch.equal(h3 * 256, torch.ceil(h3 * 256)))

    def test_install_clones_without_mutating_input_model(self):
        class ModelType:
            name = "FLOW_AV"

        class Inner:
            model_type = ModelType()

        class FakeModel:
            def __init__(self, options=None):
                self.model = Inner()
                self.model_options = {} if options is None else options
                self.wrappers = []

            def clone(self):
                return FakeModel(self.model_options)

            def set_model_denoise_mask_function(self, callback):
                self.model_options["denoise_mask_function"] = callback

            def add_wrapper_with_key(self, kind, key, callback):
                self.wrappers.append((kind, key, callback))

        patcher = types.ModuleType("comfy.patcher_extension")
        patcher.WrappersMP = types.SimpleNamespace(APPLY_MODEL="apply_model")
        comfy = types.ModuleType("comfy")
        comfy.patcher_extension = patcher
        previous = {name: sys.modules.get(name) for name in ("comfy", "comfy.patcher_extension")}
        sys.modules["comfy"] = comfy
        sys.modules["comfy.patcher_extension"] = patcher
        try:
            source = FakeModel()
            latent = {"samples": (torch.zeros(1, 2, 15, 1, 1), torch.zeros(1, 2, 1, 5))}
            patched = install_drift_control_model(source, latent, torch.tensor([2.0, 1.0, 0.0]))
        finally:
            for name, value in previous.items():
                if value is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = value
        self.assertIsNot(patched, source)
        self.assertEqual(source.model_options, {})
        self.assertIn("denoise_mask_function", patched.model_options)
        self.assertEqual(len(patched.wrappers), 1)


if __name__ == "__main__":
    unittest.main()
