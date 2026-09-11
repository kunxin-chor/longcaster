"""Embedded-Python smoke for Stage 2B extraction and native reference injection."""

from pathlib import Path
import sys
import tempfile

import torch

sys.path.append(str(Path(__file__).resolve().parents[1]))

from longcaster import h3_runtime, mmh3_adapter, state_anchor
from longcaster.project import ProjectStore


class FakeVideoVAE:
    def encode(self, images):
        return torch.zeros((1, 24, 1, max(1, images.shape[1] // 16), max(1, images.shape[2] // 16)))


class FakeClip:
    def tokenize(self, prompt, **kwargs):
        return prompt, kwargs

    def encode_from_tokens_scheduled(self, tokens):
        return [[torch.zeros((1, 1, 1)), {"smoke_tokens": tokens}]]


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        store = ProjectStore(Path(temporary), "identity_smoke")
        card = {
            "id": "caa9bcdf-c156-4107-a7ee-532e45f04176",
            "artifact_number": 2,
            "status": "ACCEPTED",
            "master_path": "clips/card_0002.mmh3",
            "context_frame_count": 39,
            "actual_new_frame_count": 3,
        }
        decoded = torch.zeros((42, 8, 8, 3), dtype=torch.float32)
        decoded[40, ..., 0] = 1.0
        original_decode = state_anchor._decoded_images
        state_anchor._decoded_images = lambda packet, video_vae: decoded
        try:
            anchor = state_anchor.extract_identity_frame_anchor(
                store=store,
                card=card,
                packet=object(),
                video_vae=object(),
                preview_frame_index=1,
                subject_id="<Subject 1>",
                label="clear face",
            )
        finally:
            state_anchor._decoded_images = original_decode
        assert anchor["source_preview_frame_index"] == 1
        assert anchor["source_frame_index"] == 40
        assert anchor["mode"] == "MiniMaxH3ReferenceToVideo/minimax_refs"
        assert store.absolute_path(anchor["asset_path"]).is_file()
        external = state_anchor.persist_identity_image_anchor(
            store=store,
            card=card,
            image=decoded[40:41],
            preview_frame_index=1,
            subject_id="<Subject 1>",
            label="external picker",
        )
        assert external["selection_source"] == "external_image"
        assert external["source_frame_index"] == 40
        assert store.absolute_path(external["asset_path"]).is_file()

    original_materialize = mmh3_adapter.materialize_native_references
    mmh3_adapter.materialize_native_references = lambda *args, **kwargs: mmh3_adapter.NativeReferences(
        {"ref_image_0": torch.zeros((1, 32, 32, 3))}, {}, {}, {}, {"ready": True}
    )
    try:
        positive, _, report = h3_runtime.build_conditioning(
            clip=FakeClip(),
            video_vae=FakeVideoVAE(),
            audio_vae=object(),
            prompt="Preserve <Picture 2> identity.",
            width=32,
            height=32,
            frames=56,
            reference_packet=object(),
            ref_image_size="match",
            additional_reference_images={"identity": torch.ones((1, 32, 32, 3))},
        )
    finally:
        mmh3_adapter.materialize_native_references = original_materialize
    refs = positive[0][1]["minimax_refs"]
    assert len(refs) == 2
    assert all(item["kind"] == "image" for item in refs)
    assert report["longcaster_additional_reference_images"] == 1
    print("PASS: historical/external frame selection + native minimax_refs identity injection")


if __name__ == "__main__":
    main()
