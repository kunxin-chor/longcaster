"""CPU integration with synthetic archives/Comfy seams; never executes a graph."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from longcaster.project import ProjectStore, ProjectError, sha256_file
from longcaster.hires_runtime import create_hires, apply_frozen_loras

try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, "CPU torch required")
class HiresRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProjectStore(Path(self.temp.name), "runtime")
        self.store.create(prompt="source", duration_seconds=1, seed=9, width=32, height=32,
                          generation_mode="t2va")
        self.first = {"effective_prompt": "frozen template and activation words", "seed": 9,
                      "state_anchor": {"active": True, "anchor_id": "anchor", "target_frame_index": 2},
                      "lora_activation_words": "frozen words"}
        m, card = self.store.begin_generation(action="generate", prompt="source", duration_seconds=1,
                                               seed=9, recipe=self.first, fingerprint="frozen")
        path = self.store.draft_destination(card); path.write_bytes(b"native")
        self.store.finish_generation(operation_id=m["pending_operation"]["id"], draft_path=path,
                                     artifact_sha256=sha256_file(path), context_frame_count=0,
                                     generated_frame_count=22, actual_new_frame_count=22,
                                     actual_duration_seconds=22 / 24)
        self.manifest = self.store.accept()
        self.card = self.store.active_card(self.manifest)
        # Active child deliberately has different mutable inputs.
        self.manifest = self.store.append(prompt="mutable child", duration_seconds=1, seed=888)
        self.native = {"samples": (torch.randn(1, 24, 4, 2, 2), torch.randn(1, 32, 8))}
        self.outputs = {}
        self.sigmas = torch.tensor([0.3, 0.0])
        self.model = object()
        self.module = "longcaster.hires_runtime"
        def load(path, **kwargs):
            return str(path)
        def primary(packet):
            return self.outputs.get(str(packet), self.native), {}
        def save(**kwargs):
            path = kwargs["path"]; path.write_bytes(b"hires archive")
            self.outputs[str(path)] = kwargs["latent"]
            self.saved_metadata = kwargs["derivative_metadata"]
            return None, str(path)
        self.mocks = {}
        mocks = {
            "load_packet": dict(side_effect=load), "primary_latent": dict(side_effect=primary),
            "upscale_video": dict(return_value=(torch.randn(1, 24, 4, 4, 4), {"upscale_seconds": 0.1})),
            "build_conditioning": dict(return_value=(["positive"], {}, {})),
            "prepare_hires_reference": dict(return_value=(None, None)),
            "_frozen_anchor": dict(return_value={"id": "frozen anchor"}),
            "load_anchor_image": dict(return_value="frozen image"),
            "apply_native_anchor": dict(return_value=["anchored"]),
            "apply_frozen_loras": dict(side_effect=lambda model, recipe: model),
            "sample_h3": dict(side_effect=lambda **kw: kw["latent"]),
            "pack_and_save_refine_derivative": dict(side_effect=save),
        }
        for name, kwargs in mocks.items():
            patcher = patch(f"{self.module}.{name}", **kwargs)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def run_hires(self, **overrides):
        kwargs = dict(store=self.store, manifest=self.manifest, card_id=self.card["id"],
                      model=self.model, sigmas=self.sigmas, clip=object(), video_vae=object(),
                      audio_vae=object(), reference_packet=None, upscaler_model="learned.safetensors")
        kwargs.update(overrides)
        return create_hires(**kwargs)

    def test_frozen_source_target_resolution_and_explicit_schedule(self):
        result = self.run_hires()
        self.assertEqual(result["active_card_id"], self.manifest["active_card_id"])
        conditioning = self.mocks["build_conditioning"].call_args.kwargs
        self.assertEqual(conditioning["prompt"], self.first["effective_prompt"])
        self.assertEqual((conditioning["width"], conditioning["height"]), (64, 64))
        sample = self.mocks["sample_h3"].call_args.kwargs
        self.assertIs(sample["sigmas"], self.sigmas)
        self.assertIs(sample["model"], self.model)
        self.assertEqual(sample["sampler_name"], "euler")
        self.assertEqual(sample["seed"], 9)
        self.assertEqual(self.mocks["apply_frozen_loras"].call_args.args[1], self.first)
        guide = self.mocks["apply_native_anchor"].call_args.kwargs
        self.assertEqual(guide["frame_index"], 2)
        self.assertEqual(guide["image"], "frozen image")
        self.assertEqual(guide["latent"]["samples"][0].shape[-2:], (4, 4))
        self.assertEqual(self.saved_metadata["type"], "hires_refine")
        self.assertEqual(self.saved_metadata["recipe"]["source_recipe"], self.first)
        self.assertEqual((result["width"], result["height"]), (32, 32))
        self.assertEqual(self.store.absolute_path(self.card["master_path"]).read_bytes(), b"native")

    def test_missing_explicit_inputs_fail_without_sampling(self):
        for key in ("sigmas", "model"):
            with self.subTest(key=key), self.assertRaisesRegex(ProjectError, "explicit hires_model and hires_sigmas"):
                self.run_hires(**{key: None})
        self.mocks["sample_h3"].assert_not_called()
        m = self.store.load()
        self.assertEqual(m["cards"][0]["derivatives"][-1]["status"], "FAILED")
        self.assertEqual(m["cards"][0]["status"], "ACCEPTED")

    def test_audio_mutation_aborts_commit_and_preserves_native(self):
        def mutate(**kwargs):
            kwargs["latent"]["samples"][1][0, 0, -1] += 1
            return kwargs["latent"]
        self.mocks["sample_h3"].side_effect = mutate
        with self.assertRaisesRegex(RuntimeError, "protected audio"):
            self.run_hires()
        self.mocks["pack_and_save_refine_derivative"].assert_not_called()
        m = self.store.load()
        self.assertEqual(m["cards"][0]["derivatives"][-1]["status"], "FAILED")
        self.assertEqual(m["cards"][0]["artifact_sha256"], self.card["artifact_sha256"])

    def test_invalid_sigmas_record_a_failed_attempt(self):
        with self.assertRaisesRegex(ProjectError, "invalid hires SIGMAS"):
            self.run_hires(sigmas=torch.tensor([0.0]))
        self.mocks["upscale_video"].assert_not_called()
        self.assertEqual(self.store.load()["cards"][0]["derivatives"][-1]["status"], "FAILED")

    def test_archive_roundtrip_must_preserve_protected_audio(self):
        save = self.mocks["pack_and_save_refine_derivative"].side_effect
        def corrupt_archive(**kwargs):
            result = save(**kwargs)
            self.outputs[result[1]]["samples"][1][0, 0, -1] += 1
            return result
        self.mocks["pack_and_save_refine_derivative"].side_effect = corrupt_archive
        with self.assertRaisesRegex(RuntimeError, "protected audio"):
            self.run_hires()
        m = self.store.load()
        d = m["cards"][0]["derivatives"][-1]
        self.assertEqual(d["status"], "FAILED")
        self.assertFalse(self.store.hires_destination(self.card, d["id"]).exists())

    def test_export_uses_existing_hires_and_checks_ancestry(self):
        m = self.run_hires()
        with patch.dict(sys.modules, {"folder_paths": types.ModuleType("folder_paths")}):
            from longcaster.timeline_export import export_hires_sources
        selected = [m["cards"][0]]
        self.assertEqual(export_hires_sources(self.store, m, selected, source="native"), {})
        sources = export_hires_sources(self.store, m, selected, source="hires")
        self.assertEqual(sources[self.card["id"]]["type"], "hires_refine")
        with self.assertRaisesRegex(ProjectError, "accepted cards only"):
            export_hires_sources(self.store, m, m["cards"], source="hires")
        self.mocks["upscale_video"].assert_called_once()


class HiresFrozenLoraTests(unittest.TestCase):
    def test_changed_lora_weights_fail_before_loading_or_patching(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.safetensors"
            path.write_bytes(b"changed weights")
            folder = types.ModuleType("folder_paths")
            folder.get_full_path_or_raise = lambda *_: str(path)
            comfy = types.ModuleType("comfy")
            sd = types.ModuleType("comfy.sd")
            utils = types.ModuleType("comfy.utils")
            comfy.sd = sd; comfy.utils = utils
            with patch.dict(sys.modules, {"folder_paths": folder, "comfy": comfy,
                                           "comfy.sd": sd, "comfy.utils": utils}):
                with self.assertRaisesRegex(ProjectError, "LoRA hash changed"):
                    apply_frozen_loras(object(), {"card_loras": [{"lora_name": path.name, "asset_sha256": "a" * 64}]})


if __name__ == "__main__":
    unittest.main()
