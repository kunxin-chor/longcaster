from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from longcaster.card_reference import (
    CachedCardVAE, clean_cache_key, prepare_reference, prepare_refine_reference,
    substitute_card_latent, vae_identity, _CACHE,
)
from longcaster.duplicate import duplicate_project
from longcaster.fingerprint import generation_fingerprint
from longcaster.h3_runtime import build_conditioning
from longcaster.mmh3_adapter import NativeReferences
from longcaster.project import ProjectError, ProjectStore, sha256_file

try:
    from PIL import Image
except ImportError:
    Image = None
try:
    import torch
except ImportError:
    torch = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(Image is None, "Pillow required for PNG persistence tests")
class CardReferenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root, "references")
        self.store.create(prompt="pose <Picture 2>", duration_seconds=5, seed=1,
                          width=64, height=32, generation_mode="ref2va")

    def upload(self, color="red"):
        data = io.BytesIO()
        Image.new("RGB", (64, 32), color).save(data, "PNG")
        manifest = self.store.load()
        return self.store.update_card_reference(
            card_id=manifest["active_card_id"], expected_revision=manifest["revision"], image_bytes=data.getvalue())

    def edit(self, **settings):
        manifest = self.store.load()
        return self.store.update_card_editor(
            card_id=manifest["active_card_id"], expected_revision=manifest["revision"],
            card_reference_settings=settings)

    def draft(self, action="generate"):
        card = self.store.active_card(self.store.load())
        recipe = {"card_reference": deepcopy(card.get("card_reference")),
                  "references": {"packet_id": "packet", "resources": [{"kind": "image", "id": "a"}]}}
        if recipe["card_reference"]:
            recipe["card_reference"]["resolved_picture_index"] = 2
        manifest, card = self.store.begin_generation(
            action=action, prompt=card["prompt"], duration_seconds=5, seed=1,
            recipe=recipe, fingerprint=generation_fingerprint(recipe))
        path = self.store.draft_destination(card)
        path.write_bytes(json.dumps(recipe).encode())
        return self.store.finish_generation(
            operation_id=manifest["pending_operation"]["id"], draft_path=path,
            artifact_sha256=sha256_file(path), context_frame_count=0,
            generated_frame_count=124, actual_new_frame_count=124, actual_duration_seconds=124 / 24)

    def test_old_manifest_migrates_without_reference(self):
        manifest = self.store.load()
        manifest["schema_version"] = 14
        del manifest["cards"][0]["card_reference"]
        self.store.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded["schema_version"], 15)
        self.assertIsNone(loaded["cards"][0]["card_reference"])

    def test_upload_replace_remove_keep_take_sources(self):
        first = self.upload()["cards"][0]["card_reference"]
        self.assertEqual(sha256_file(self.store.absolute_path(first["asset_path"])), first["asset_sha256"])
        second = self.upload("blue")["cards"][0]["card_reference"]
        self.assertNotEqual(first["id"], second["id"])
        manifest = self.store.load()
        removed = self.store.update_card_reference(card_id=manifest["active_card_id"], expected_revision=manifest["revision"], remove=True)
        self.assertIsNone(removed["cards"][0]["card_reference"])
        self.assertTrue(self.store.absolute_path(first["asset_path"]).is_file())

    def test_strength_dirties_retry_freezes_and_selection_restores(self):
        self.upload()
        first = self.draft()["cards"][0]
        first_take = first["draft_takes"][0]
        changed = self.edit(latent_mode="soften", latent_strength=0.35)
        self.assertTrue(changed["cards"][0]["draft_inputs_dirty"])
        with self.assertRaises(ProjectError):
            self.store.accept()
        second = self.draft("retry")["cards"][0]
        self.assertEqual(second["recipe"]["card_reference"]["latent_strength"], 0.35)
        self.assertNotEqual(first["generation_fingerprint"], second["generation_fingerprint"])
        self.upload("blue")
        manifest = self.store.load()
        restored = self.store.select_draft_take(card_id=first["id"], take_id=first_take["id"], expected_revision=manifest["revision"])
        self.assertEqual(restored["cards"][0]["card_reference"], first_take["card_reference"])
        self.assertFalse(restored["cards"][0]["draft_inputs_dirty"])

    def test_invalidate_unpublish_and_duplicates_retain_definition(self):
        reference = self.upload()["cards"][0]["card_reference"]
        self.draft()
        self.store.accept()
        manifest = self.store.load()
        with self.assertRaisesRegex(ProjectError, "unaccepted"):
            self.store.update_card_reference(card_id=manifest["active_card_id"], expected_revision=manifest["revision"], remove=True)
        unpublish = self.store.unpublish_tail()
        self.assertEqual(unpublish["cards"][0]["card_reference"], reference)
        for invalidate in (False, True):
            def clone(src, dst, *_args):
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())
                return sha256_file(dst)
            copied = duplicate_project(self.root, "references", f"copy_{invalidate}", archive_cloner=clone, invalidate_renders=invalidate)
            self.assertEqual(copied["cards"][0]["card_reference"], reference)
            self.assertTrue((self.root / f"copy_{invalidate}" / reference["asset_path"]).is_file())
        self.store.invalidate_render()
        self.assertEqual(self.store.load()["cards"][0]["card_reference"], reference)
        self.assertTrue(self.store.absolute_path(reference["asset_path"]).is_file())

    def test_invalidate_all_retains_reference(self):
        reference = self.upload()["cards"][0]["card_reference"]
        self.draft()
        self.store.invalidate_all_renders()
        self.assertEqual(self.store.load()["cards"][0]["card_reference"], reference)
        self.assertTrue(self.store.absolute_path(reference["asset_path"]).is_file())

    def test_remove_card_cleans_all_owned_images_without_inheriting(self):
        self.upload()
        self.draft()
        self.store.accept()
        appended = self.store.append(prompt="next", duration_seconds=5, seed=2)
        self.assertIsNone(appended["cards"][-1]["card_reference"])
        reference = self.upload()["cards"][-1]["card_reference"]
        self.store.remove_draft_tail()
        self.assertFalse(self.store.absolute_path(reference["asset_path"]).exists())

    def test_packet_change_dirties_without_modifying_take_provenance(self):
        self.upload()
        card = self.draft()["cards"][0]
        takes = deepcopy(card["draft_takes"])
        changed = self.store.observe_reference_packet({"packet_id": "other", "resources": []})
        self.assertTrue(changed["cards"][0]["draft_inputs_dirty"])
        self.assertEqual(changed["cards"][0]["draft_takes"], takes)

    def test_t2va_rejects_and_settings_are_validated(self):
        self.upload()
        for strength in (-0.1, 1.01, float("nan"), True, "0.35"):
            with self.subTest(strength=strength), self.assertRaises(ProjectError):
                self.edit(latent_strength=strength)
        manifest = self.store.load()
        with self.assertRaisesRegex(ProjectError, "disable or remove"):
            self.store.update_project_settings(expected_revision=manifest["revision"], generation_mode="t2va")
        self.edit(enabled=False)
        manifest = self.store.load()
        self.store.update_project_settings(expected_revision=manifest["revision"], generation_mode="t2va")
        with self.assertRaisesRegex(ProjectError, "REF2VA"):
            self.upload()

    def test_stale_revision_and_generation_race_rejected(self):
        manifest = self.upload()
        reference = deepcopy(manifest["cards"][0]["card_reference"])
        self.edit(latent_strength=0.5)
        with self.assertRaisesRegex(ProjectError, "stale project revision"):
            self.store.update_card_reference(card_id=manifest["active_card_id"], expected_revision=manifest["revision"], remove=True)
        with self.assertRaisesRegex(ProjectError, "changed while preparing"):
            self.store.begin_generation(action="generate", prompt="", duration_seconds=5, seed=1,
                                        recipe={"card_reference": reference}, fingerprint="unused")


@unittest.skipIf(torch is None, "PyTorch required for native tensor contract tests")
class CardReferenceRuntimeTests(unittest.TestCase):
    def setUp(self):
        _CACHE.clear()
        self.native = types.ModuleType("comfy_extras.nodes_minimax_h3")
        self.native.CANVAS_MULTIPLE = 32
        self.native.REF_IMAGE_SHORT_EDGE = 2048
        self.vae = self.new_vae()
        self.image = torch.ones(1, 32, 64, 3) * 0.5
        self.reference = {"id": "card-ref", "enabled": True, "asset_sha256": "a" * 64,
                          "latent_mode": "soften", "latent_strength": 0.35,
                          "resolved_picture_index": 2, "resized_width": 64, "resized_height": 32,
                          "ref_image_size": "match", "canvas": [64, 32], "cache_version": "test-v1",
                          "vae_identity": vae_identity(self.vae)}
        owner = self
        class NativeReferenceNode:
            @classmethod
            def execute(cls, *, clip, vae, audio_vae, prompt, width, height, length,
                        ref_image_size, ref_images, ref_videos, ref_video_audios, ref_audios):
                owner.presented = list(ref_images.values())
                owner.prompt = prompt
                blocks = [{"kind": "image", "latent_h": 2, "latent_w": 4,
                           "native_extra": "preserve", "latent": vae.encode(image)}
                          for image in ref_images.values()]
                for video in ref_videos.values():
                    blocks.append({"kind": "video", "latent": vae.encode(video), "latent_t": 1})
                blocks.append({"kind": "audio", "audio_latent": "unchanged"})
                owner.original = [["embedding", {"minimax_refs": blocks, "other": "native"}]]
                return owner.original, {"samples": "empty"}
        self.native.MiniMaxH3ReferenceToVideo = NativeReferenceNode
        self.native.MiniMaxH3ImageToVideo = types.SimpleNamespace(execute=lambda *_: ("t2va", {}))
        extras = types.ModuleType("comfy_extras")
        extras.nodes_minimax_h3 = self.native
        patcher = patch.dict(sys.modules, {"comfy_extras": extras, "comfy_extras.nodes_minimax_h3": self.native})
        patcher.start(); self.addCleanup(patcher.stop)
        self.refs = NativeReferences({"ref_image_0": self.image * 0.2}, {}, {}, {}, {})
        patcher = patch("longcaster.mmh3_adapter.materialize_native_references", return_value=self.refs)
        patcher.start(); self.addCleanup(patcher.stop)

    @staticmethod
    def new_vae():
        class VAE:
            calls = 0
            def encode(self, image):
                self.calls += 1
                return torch.full((1, 24, 1, image.shape[1] // 16, image.shape[2] // 16), float(image.mean()), dtype=torch.float32)
        return VAE()

    def build(self, reference=True, packet=True, identity=True):
        return build_conditioning(clip=None, video_vae=self.vae, audio_vae=None,
            prompt="Use <Picture 2>", width=64, height=32, frames=124,
            reference_packet=object() if packet else None, ref_image_size="match",
            additional_reference_images={"identity": self.image * 0.9} if identity else None,
            card_reference=self.reference if reference else None,
            card_reference_image=self.image if reference else None)

    def test_native_order_rgb_and_only_card_block_changes(self):
        self.refs.videos["ref_video_0"] = self.image.repeat(5, 1, 1, 1)
        positive, _, report = self.build()
        refs = positive[0][1]["minimax_refs"]
        original = self.original[0][1]["minimax_refs"]
        self.assertIs(self.presented[1], self.image)
        self.assertEqual(self.prompt, "Use <Picture 2>")
        for index in range(len(refs)):
            if index != 1:
                self.assertIs(refs[index], original[index])
        self.assertIsNot(refs[1], original[1])
        self.assertTrue(torch.equal(refs[1]["latent"], original[1]["latent"] * 0.35))
        self.assertTrue(torch.all(original[1]["latent"] == 0.5))
        self.assertEqual(refs[1]["native_extra"], "preserve")
        self.assertEqual(report["card_reference"]["resolved_picture_index"], 2)

    def test_strength_endpoints_and_dtype_device_shape(self):
        for dtype in (torch.float32, torch.float16):
            for strength in (0, 0.25, 0.35, 0.5, 1):
                with self.subTest(dtype=dtype, strength=strength):
                    clean = torch.randn(1, 24, 1, 2, 4, dtype=dtype)
                    positive = [[None, {"minimax_refs": [{"kind": "image", "latent": clean}]}]]
                    result = substitute_card_latent(positive, picture_index=1, clean=clean, mode="soften", strength=strength)[0][1]["minimax_refs"][0]["latent"]
                    self.assertTrue(torch.equal(result, clean * strength))
                    self.assertEqual((result.dtype, result.device, result.shape), (clean.dtype, clean.device, clean.shape))
                    if strength == 1:
                        self.assertIs(result, clean)

    def test_normal_ignores_strength_and_no_reference_uses_original_path(self):
        self.reference["latent_mode"] = "normal"
        result, _, _ = self.build()
        self.assertIs(result[0][1]["minimax_refs"][1]["latent"], self.original[0][1]["minimax_refs"][1]["latent"])
        result, _, _ = self.build(reference=False)
        self.assertIs(result, self.original)
        result, _, _ = self.build(reference=False, packet=False, identity=False)
        self.assertEqual(result, "t2va")

    def test_t2va_and_image_limit_rejected(self):
        with self.assertRaisesRegex(ValueError, "REF2VA"):
            self.build(packet=False)
        self.refs.images.update({f"ref_image_{i}": self.image for i in range(1, 8)})
        self.reference["resolved_picture_index"] = 9
        with self.assertRaisesRegex(ValueError, "at most 9"):
            self.build()

    def test_strength_change_avoids_card_encode_and_does_not_poison_cache(self):
        first, _, _ = self.build()
        self.assertEqual(self.vae.calls, 3)
        first[0][1]["minimax_refs"][1]["latent"].zero_()
        self.reference["latent_strength"] = 0.5
        second, _, _ = self.build()
        self.assertEqual(self.vae.calls, 5)  # global + identity only on retry
        self.assertTrue(torch.all(second[0][1]["minimax_refs"][1]["latent"] == 0.25))

    def test_cache_invalidates_each_encoding_input(self):
        original = deepcopy(self.reference)
        CachedCardVAE(self.vae, original, 1).encode(self.image)
        for field, value in (("asset_sha256", "b" * 64), ("ref_image_size", "max"),
                             ("canvas", [128, 64]), ("vae_identity", "another"), ("cache_version", "v2"),
                             ("resized_width", 32), ("resized_height", 64)):
            altered = {**original, field: value}
            image = torch.ones(1, altered["resized_height"], altered["resized_width"], 3)
            before = self.vae.calls
            CachedCardVAE(self.vae, altered, 1).encode(image)
            self.assertEqual(self.vae.calls, before + 1)
        self.assertNotEqual(vae_identity(self.vae), vae_identity(self.new_vae()))
        self.vae.first_stage_model = torch.nn.Linear(2, 2)
        before = vae_identity(self.vae)
        with torch.no_grad():
            self.vae.first_stage_model.weight.add_(1)
        self.assertNotEqual(before, vae_identity(self.vae))

    def test_cache_bound_and_wrong_native_mapping_fail_closed(self):
        for index in range(10):
            reference = {**self.reference, "asset_sha256": str(index) * 64}
            CachedCardVAE(self.vae, reference, 1).encode(self.image)
        self.assertLessEqual(len(_CACHE), 8)
        self.assertTrue(all(tensor.device.type == "cpu" for tensor, _ in _CACHE.values()))
        clean = torch.ones(1)
        with self.assertRaisesRegex(ValueError, "mapping changed"):
            substitute_card_latent([[None, {"minimax_refs": [{"kind": "image", "latent": clean.clone()}]}]],
                                   picture_index=1, clean=clean, mode="soften", strength=0.35)

    def test_zero_strength_still_presents_original_rgb(self):
        self.reference["latent_strength"] = 0
        positive, _, _ = self.build()
        self.assertIs(self.presented[1], self.image)
        self.assertEqual(torch.count_nonzero(positive[0][1]["minimax_refs"][1]["latent"]).item(), 0)

    @unittest.skipIf(Image is None, "Pillow required")
    def test_prepare_and_refine_use_frozen_source_and_validate_number(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            store = ProjectStore(directory, "frozen")
            manifest = store.create(prompt="", duration_seconds=5, seed=1, width=64, height=32, generation_mode="ref2va")
            data = io.BytesIO(); Image.new("RGB", (64, 32), "red").save(data, "PNG")
            manifest = store.update_card_reference(card_id=manifest["active_card_id"], expected_revision=manifest["revision"], image_bytes=data.getvalue())
            reference = manifest["cards"][0]["card_reference"]
            args = dict(references={"resources": [{"kind": "image"}]}, width=64, height=32, ref_image_size="match", video_vae=self.vae)
            frozen, rgb = prepare_reference(store, reference, **args)
            self.assertEqual(frozen["resolved_picture_index"], 2)
            self.assertEqual(frozen["clean_cache_key"], clean_cache_key(frozen))
            store.update_card_reference(card_id=manifest["active_card_id"], expected_revision=manifest["revision"], settings={"latent_strength": 0.25})
            rebuilt, image = prepare_refine_reference(store, {"card_reference": frozen}, **args)
            self.assertEqual(rebuilt["latent_strength"], 1)
            self.assertTrue(torch.equal(image, rgb))
            args["references"] = {"resources": []}
            with self.assertRaisesRegex(ProjectError, "accepted recipe"):
                prepare_refine_reference(store, {"card_reference": frozen}, **args)


if __name__ == "__main__":
    unittest.main()
