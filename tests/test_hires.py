import unittest
from unittest.mock import patch

from longcaster.hires import (resolve_target, joint_streams, prepare_hires_latent,
                              protection_snapshot, assert_hires_unchanged)
from longcaster.refine import RefineError

try:
    import torch
except ImportError:
    torch = None


class HiresDimensionsTests(unittest.TestCase):
    def test_initial_target(self):
        target = resolve_target(544, 960)
        self.assertEqual((target["target_width"], target["target_height"]), (1088, 1920))
        self.assertEqual(target["scale"], 2.0)

    def test_reject_invalid_targets(self):
        for kwargs in ({"scale": float("nan")}, {"scale": 1}, {"target_width": 1088},
                       {"target_width": 1080, "target_height": 1920},
                       {"target_width": 32, "target_height": 32}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                resolve_target(544, 960, **kwargs)


@unittest.skipIf(torch is None, "CPU torch is required for numerical hires tests")
class HiresProtectionTests(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(7)
        self.native = {"samples": (torch.randn(1, 24, 8, 2, 3), torch.randn(1, 32, 10))}
        self.lifted = torch.randn(1, 24, 8, 4, 6)
        self.parent = torch.randn(1, 24, 9, 4, 6)
        video = torch.zeros_like(self.lifted)
        video[:, :, :3] = self.parent[:, :, -3:]
        mask = torch.ones(1, 1, 8, 1, 1)
        mask[:, :, :3] = 0
        self.lock = {"samples": (video, torch.zeros(1, 32, 10)),
                     "noise_mask": (mask, torch.ones(1, 32, 10))}

    def test_parent_tail_equals_every_protected_head_value(self):
        original = self.lifted.clone()
        work, report = prepare_hires_latent(self.native, self.lifted, self.lock, parent_video=self.parent)
        video, audio = joint_streams(work)
        self.assertTrue(torch.equal(video[:, :, :3], self.parent[:, :, -3:]))
        self.assertTrue(torch.equal(video[:, :, 3:], original[:, :, 3:]))
        self.assertTrue(torch.equal(self.lifted, original))
        self.assertTrue(torch.equal(audio, self.native["samples"][1]))
        self.assertEqual(report["video_protected_values"], 24 * 3 * 4 * 6)
        self.assertEqual(report["audio_protected_values"], audio.numel())
        self.assertEqual(report["dc_correction"]["compared_latent_tokens"], 3 * 4 * 6)
        self.assertLessEqual(report["dc_correction"]["maximum_absolute_correction"], 0.050001)
        assert_hires_unchanged(protection_snapshot(work), work)

    def test_audio_is_fully_locked_even_without_parent(self):
        work, _ = prepare_hires_latent(self.native, self.lifted)
        before = protection_snapshot(work)
        work["samples"][1][0, 0, -1] += 1
        with self.assertRaisesRegex(RefineError, "protected audio"):
            assert_hires_unchanged(before, work)
        self.assertFalse(torch.equal(work["samples"][1], self.native["samples"][1]))

    def test_video_prefix_mutation_is_rejected(self):
        work, _ = prepare_hires_latent(self.native, self.lifted, self.lock)
        before = protection_snapshot(work)
        work["samples"][0][0, 23, 2, -1, -1] += 0.001
        with self.assertRaisesRegex(RefineError, "protected video"):
            assert_hires_unchanged(before, work)

    def test_retained_body_is_allowed_to_change(self):
        work, _ = prepare_hires_latent(self.native, self.lifted, self.lock)
        before = protection_snapshot(work)
        work["samples"][0][:, :, 3:] += 1
        assert_hires_unchanged(before, work)

    def test_signed_zero_and_dtype_are_strict(self):
        work, _ = prepare_hires_latent(self.native, self.lifted)
        work["samples"][1][0, 0, 0] = 0.0
        before = protection_snapshot(work)
        work["samples"][1][0, 0, 0] = -0.0
        with self.assertRaisesRegex(RefineError, "protected audio"):
            assert_hires_unchanged(before, work)
        work["samples"] = tuple(t.half() for t in work["samples"])
        with self.assertRaisesRegex(RefineError, "dtype"):
            assert_hires_unchanged(before, work)

    def test_median_ignores_outlier_and_never_changes_body(self):
        self.lifted.zero_()
        self.lock["samples"][0][:, :, :3] = 0.01
        self.lock["samples"][0][0, 0, 0, 0, 0] = 100
        work, report = prepare_hires_latent(self.native, self.lifted, self.lock)
        self.assertAlmostEqual(report["dc_correction"]["maximum_absolute_correction"], 0.01)
        self.assertEqual(report["dc_correction"]["channels_corrected"], 24)
        self.assertEqual(torch.count_nonzero(work["samples"][0][:, :, 3:]).item(), 0)

    def test_reject_wrong_channel_layout_and_non_prefix_mask(self):
        bad = {"samples": (torch.zeros(1, 16, 8, 2, 3), self.native["samples"][1])}
        with self.assertRaisesRegex(RefineError, "24-channel"):
            prepare_hires_latent(bad, self.lifted)
        self.lock["noise_mask"][0][:, :, 5] = 0
        with self.assertRaisesRegex(RefineError, "contiguous"):
            prepare_hires_latent(self.native, self.lifted, self.lock)

    def test_reject_handover_that_is_not_actual_parent_tail(self):
        with self.assertRaisesRegex(RefineError, "bit-exact parent tail"):
            prepare_hires_latent(self.native, self.lifted, self.lock, parent_video=self.parent + 1)

    def test_vendor_architecture_and_chunking_preserve_time(self):
        from longcaster.vendor.h3_resizer import LatentResizer3D, _detect_arch, _make_norm_tensors
        model = LatentResizer3D(in_blocks=2, out_blocks=2, channels=32).eval()
        cfg = _detect_arch(model.state_dict())
        self.assertEqual(cfg["in_channels"], 24)
        self.assertEqual(cfg["in_blocks"], 2)
        replica = LatentResizer3D(**cfg)
        replica.load_state_dict(model.state_dict(), strict=True)
        mean, std = _make_norm_tensors("cpu", torch.float32)
        self.assertEqual(mean.numel(), 24)
        self.assertTrue((std > 0).all())
        # Test chunk assembly without expensive real learned weights.
        def resize(x, scale, size):
            return torch.nn.functional.interpolate(x, size=size, mode="nearest")
        x = torch.arange(35.0).view(1, 1, 35, 1, 1).expand(1, 24, 35, 2, 3).clone()
        with patch.object(model, "_forward_seg", side_effect=resize):
            y = model(x, scale=2.0, target_size=(35, 4, 6), enable_chunking=True)
        self.assertEqual(y.shape, (1, 24, 35, 4, 6))
        torch.testing.assert_close(y[:, :, :, 0, 0], x[:, :, :, 0, 0], rtol=1e-6, atol=1e-6)


if __name__ == "__main__":
    unittest.main()
