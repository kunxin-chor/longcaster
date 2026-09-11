"""Embedded-Python smoke for native H3 guide composition and PNG persistence."""

from pathlib import Path
import sys
import tempfile

import torch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from comfy_extras.nodes_minimax_h3 import _empty_av_latent
from longcaster.project import ProjectStore
from longcaster import state_anchor


class FakeVideoVAE:
    def encode(self, images):
        return torch.zeros((1, 24, 1, images.shape[1] // 16, images.shape[2] // 16))


def main() -> None:
    latent, _ = _empty_av_latent(32, 32, 56)
    existing_refs = [{"kind": "image", "latent": torch.zeros((1, 24, 1, 2, 2))}]
    positive = [[torch.zeros((1, 1, 1)), {"minimax_refs": existing_refs}]]
    image = torch.zeros((1, 32, 32, 3))
    conditioned = state_anchor.apply_native_anchor(
        positive=positive,
        latent=latent,
        video_vae=FakeVideoVAE(),
        image=image,
        frame_index=38,
    )
    keyframes = conditioned[0][1]["minimax_keyframes"]
    assert keyframes[0]["resolved_frame_index"] == 38
    assert conditioned[0][1]["minimax_refs"] is existing_refs

    with tempfile.TemporaryDirectory() as temporary:
        store = ProjectStore(Path(temporary), "anchor_smoke")
        manifest = store.create(
            prompt="test", duration_seconds=5, seed=1, width=32, height=32,
            generation_mode="ref2va",
        )
        card = store.active_card(manifest)
        decoded = torch.zeros((3, 8, 8, 3), dtype=torch.float32)
        decoded[-1, ..., 0] = 1.0
        original = state_anchor._decoded_images
        state_anchor._decoded_images = lambda packet, video_vae: decoded
        try:
            anchor = state_anchor.extract_last_frame_anchor(
                store=store, card=card, packet=object(), video_vae=object()
            )
        finally:
            state_anchor._decoded_images = original
        assert store.absolute_path(anchor["asset_path"]).is_file()
        assert anchor["source_card_id"] == card["id"]
        assert anchor["source_frame_index"] == 2
    print("PASS: native minimax_keyframes guide + current-state PNG persistence")


if __name__ == "__main__":
    main()
