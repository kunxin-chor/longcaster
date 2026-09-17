import unittest

try:
    import torch
    import torch.nn.functional as functional
    from longcaster.color_stable import (
        apply_color_delta,
        temporal_delta_weights,
        tensor_scene_color_stats,
    )
    from longcaster.continuation import prepare_continuation_policy
except ModuleNotFoundError:
    torch = None


@unittest.skipIf(torch is None, "PyTorch required for color-stable tensor tests")
class ColorStableTests(unittest.TestCase):
    class FakeVAE:
        def __init__(self):
            self.decode_calls = 0
            self.encode_calls = 0

        def decode(self, latent):
            self.decode_calls += 1
            values = functional.interpolate(
                latent[:, :1, :, :1, :1].float(), size=(39, 1, 1), mode="trilinear",
                align_corners=True,
            )[0, 0, :, 0, 0]
            return values[:, None, None, None].expand(39, 2, 2, 3).contiguous()

        def encode(self, images):
            self.encode_calls += 1
            values = images[..., :3].mean(dim=(1, 2, 3)).view(1, 1, 39, 1, 1)
            return functional.interpolate(
                values, size=(12, 2, 2), mode="trilinear", align_corners=True,
            )

    def test_temporal_taper_starts_at_zero_and_finishes_at_one(self):
        weights = temporal_delta_weights(12)
        self.assertEqual(float(weights[0]), 0.0)
        self.assertEqual(float(weights[-1]), 1.0)
        self.assertTrue(bool(torch.all(weights[1:] >= weights[:-1])))

    def test_delta_rule_preserves_first_step_and_changes_later_steps(self):
        vae = self.FakeVAE()
        prefix = torch.full((1, 1, 12, 2, 2), 0.25)
        current_images = torch.full((39, 2, 2, 3), 0.25)
        anchor_stats = tensor_scene_color_stats(torch.full((39, 2, 2, 3), 0.75))
        current_stats = tensor_scene_color_stats(current_images)
        corrected, report = apply_color_delta(
            prefix, vae, anchor_stats=anchor_stats, current_stats=current_stats,
            decoded_current=current_images,
        )
        self.assertTrue(report["applied"])
        self.assertTrue(torch.equal(corrected[:, :, 0], prefix[:, :, 0]))
        self.assertFalse(torch.equal(corrected[:, :, -1], prefix[:, :, -1]))

    def test_card_two_neutral_path_skips_decode_encode_and_keeps_audio_exact(self):
        vae = self.FakeVAE()
        video = torch.randn(1, 2, 15, 2, 2)
        audio = torch.randn(1, 3, 2, 8)
        state = prepare_continuation_policy(
            {"samples": (video, audio)}, {"context_frames": 39},
            mode="color_stable", video_vae=vae,
            source_sha256="a" * 64, anchor_sha256="a" * 64,
            source_card_id="card-1", anchor_card_id="card-1",
        )
        out_video, out_audio = state.latent["samples"]
        self.assertEqual((vae.decode_calls, vae.encode_calls), (0, 0))
        self.assertTrue(torch.equal(out_video, video))
        self.assertTrue(torch.equal(out_audio, audio))


if __name__ == "__main__":
    unittest.main()
