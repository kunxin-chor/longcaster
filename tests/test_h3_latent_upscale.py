"""CPU-only tests for checkpoint/layout checks and mandatory model release."""
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, "CPU torch required")
class LearnedUpscalerTests(unittest.TestCase):
    def setUp(self):
        from longcaster.vendor.h3_resizer import LatentResizer3D
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.checkpoint = Path(self.temp.name) / "small.pth"
        self.checkpoint.write_bytes(b"synthetic checkpoint")
        self.model = LatentResizer3D(in_blocks=2, out_blocks=2, channels=32)
        self.comfy = types.ModuleType("comfy")
        self.mm = types.ModuleType("comfy.model_management")
        self.mm.get_torch_device = lambda: torch.device("cpu")
        self.mm.unload_all_models = Mock()
        self.mm.soft_empty_cache = Mock()
        self.mm.throw_exception_if_processing_interrupted = Mock()
        self.utils = types.ModuleType("comfy.utils")
        self.utils.load_torch_file = Mock(return_value=self.model.state_dict())
        self.comfy.model_management = self.mm
        self.comfy.utils = self.utils
        patcher = patch.dict(sys.modules, {"comfy": self.comfy, "comfy.model_management": self.mm,
                                           "comfy.utils": self.utils})
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch("longcaster.h3_latent_upscale.model_path", return_value=self.checkpoint)
        patcher.start(); self.addCleanup(patcher.stop)

    def upscale(self):
        from longcaster.h3_latent_upscale import upscale_video
        return upscale_video(torch.zeros(1, 24, 2, 2, 2), model_name="small.pth",
                             target_width=64, target_height=64)

    def test_small_real_3d_model_runs_on_cpu_and_unloads(self):
        output, report = self.upscale()
        self.assertEqual(output.shape, (1, 24, 2, 4, 4))
        self.assertEqual(output.dtype, torch.float32)
        self.assertTrue(torch.isfinite(output).all())
        self.assertEqual(report["upscaler_config"]["channels"], 32)
        self.assertEqual(report["temporal_chunk_count"], 1)
        self.mm.unload_all_models.assert_called_once()
        self.assertEqual(self.mm.soft_empty_cache.call_count, 2)
        self.utils.load_torch_file.assert_called_once_with(str(self.checkpoint), safe_load=True)

    def test_inference_failure_still_offloads_the_model(self):
        from longcaster.vendor.h3_resizer import LatentResizer3D
        moves = []
        original_to = LatentResizer3D.to
        def track(model, *args, **kwargs):
            moves.append((args, kwargs))
            return original_to(model, *args, **kwargs)
        with patch.object(LatentResizer3D, "forward", side_effect=RuntimeError("interrupted")), \
                patch.object(LatentResizer3D, "to", track):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.upscale()
        self.assertEqual(moves[-1][0], ("cpu",))
        self.assertEqual(self.mm.soft_empty_cache.call_count, 2)

    def test_2d_checkpoint_is_rejected_without_interpolation_fallback(self):
        weights = self.model.state_dict()
        weights["conv_in.weight"] = weights["conv_in.weight"][:, :, 0]
        self.utils.load_torch_file.return_value = weights
        with self.assertRaisesRegex(ValueError, "24-channel 3D"):
            self.upscale()
        self.assertEqual(self.mm.soft_empty_cache.call_count, 2)


if __name__ == "__main__":
    unittest.main()
