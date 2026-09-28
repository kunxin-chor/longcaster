"""Accepted-card hires execution. This module never selects a native continuation source."""
from __future__ import annotations

from copy import deepcopy
import inspect
import logging
from pathlib import Path
import time

from .card_reference import prepare_hires_reference
from .continuation import DIRECT_LATENT_CONTINUATION
from .fingerprint import generation_fingerprint
from .h3_latent_upscale import upscale_video
from .h3_runtime import build_conditioning, sample_h3, sigma_values
from .hires import (AUDIO_HANDLING, assert_hires_unchanged, joint_streams,
                    prepare_hires_latent, protection_snapshot)
from .mmh3_adapter import load_packet, primary_latent, reference_snapshot, pack_and_save_refine_derivative
from .project import ProjectError, sha256_file
from .state_anchor import apply_native_anchor, load_anchor_image

LOGGER = logging.getLogger("longcaster")


def _frozen_anchor(manifest, info):
    anchors = [*manifest.get("imported_identity_anchors", []),
               *(a for c in manifest["cards"] for a in c.get("anchors", []))]
    anchor = next((a for a in anchors if a["anchor_id"] == info.get("anchor_id")), None)
    if anchor is None:
        raise ProjectError("accepted card's frozen anchor is no longer available")
    if info.get("asset_path") and info["asset_path"] != anchor.get("asset_path"):
        raise ProjectError("accepted card's frozen anchor asset changed")
    if info.get("asset_sha256") and info["asset_sha256"] != anchor["asset_sha256"]:
        raise ProjectError("accepted card's frozen anchor hash changed")
    return anchor


def apply_frozen_loras(model, recipe):
    """The explicitly connected hires MODEL is a base; apply accepted card LoRAs once."""
    import folder_paths
    import comfy.sd
    import comfy.utils
    patched = model
    for item in recipe.get("card_loras", []):
        path = Path(folder_paths.get_full_path_or_raise("loras", item["lora_name"]))
        if sha256_file(path) != item.get("asset_sha256"):
            raise ProjectError(f"accepted card LoRA hash changed: {item['lora_name']}")
        lora, metadata = comfy.utils.load_torch_file(str(path), safe_load=True, return_metadata=True)
        kwargs = {"lora_metadata": metadata} if "lora_metadata" in inspect.signature(comfy.sd.load_lora_for_models).parameters else {}
        patched, _ = comfy.sd.load_lora_for_models(patched, None, lora, item["strength_model"], 0.0, **kwargs)
    return patched


def create_hires(*, store, manifest, card_id, model, sigmas, clip, video_vae, audio_vae,
                 reference_packet, upscaler_model, scale=2.0, target_width=0, target_height=0,
                 temporal_chunking=True, precision="bf16", model_snapshot=None):
    card = deepcopy(store.card(manifest, card_id))
    first = deepcopy(card.get("recipe") or {})
    schedule, sigma_error = [], None
    if sigmas is not None:
        try:
            schedule = sigma_values(sigmas)
        except Exception as exc:
            sigma_error = str(exc)
    recipe = {"version": 1, "source_recipe": first,
              "source_recipe_fingerprint": card.get("generation_fingerprint"),
              "model": model_snapshot, "model_source": "explicit_hires_model",
              "sampler": "euler", "sigmas": schedule,
              "sigmas_sha256": generation_fingerprint({"sigmas": schedule}),
              "seed": int(first.get("seed", card["seed"])),
              "audio_handling": AUDIO_HANDLING}
    manifest, derivative = store.begin_hires_refine(
        card_id=card_id, recipe=recipe, upscaler_model=upscaler_model, scale=scale,
        target_width=target_width, target_height=target_height,
        temporal_chunking=temporal_chunking, precision=precision,
    )
    operation_id = manifest["pending_operation"]["id"]
    destination = store.hires_destination(card, derivative["id"])
    started = time.perf_counter()
    try:
        current = store.card(manifest, card_id)
        if (card.get("master_path") != derivative["source_master_path"]
                or card.get("artifact_sha256") != derivative["source_artifact_sha256"]
                or card.get("recipe") != current.get("recipe")):
            raise ProjectError("accepted card changed while preparing hires; retry")
        if model is None or sigmas is None:
            raise ProjectError("connect explicit hires_model and hires_sigmas; hires never borrows another pass's inputs")
        if sigma_error is not None:
            raise ProjectError(f"invalid hires SIGMAS: {sigma_error}")
        width, height = derivative["target_width"], derivative["target_height"]
        source_packet = load_packet(store.absolute_path(card["master_path"]), verify="full")
        accepted, _ = primary_latent(source_packet)
        native_video, _ = joint_streams(accepted)
        if (native_video.shape[-1] * 16, native_video.shape[-2] * 16) != (manifest["width"], manifest["height"]):
            raise ProjectError("accepted H3 latent dimensions do not match the native project canvas")
        lifted, diagnostics = upscale_video(
            native_video, model_name=upscaler_model, target_width=width, target_height=height,
            temporal_chunking=temporal_chunking, precision=precision, force_unload=True,
        )
        parent = store.hires_parent(manifest, card)
        lock = plan = parent_video = None
        if parent:
            parent_packet = load_packet(store.absolute_path(parent["artifact_path"]), verify="full")
            parent_latent, _ = primary_latent(parent_packet)
            parent_video, _ = joint_streams(parent_latent)
            lock, plan = DIRECT_LATENT_CONTINUATION.prepare(
                parent_packet, target_frames=int(card["generated_frame_count"]),
                width=width, height=height, context_frames=int(card["context_frame_count"]),
            )
        work, protection = prepare_hires_latent(accepted, lifted, lock, parent_video=parent_video)
        protection["continuation_plan"] = plan
        before = protection_snapshot(work)
        del lifted, native_video, accepted, source_packet, lock, parent_video

        conditioning_packet = reference_packet if manifest["generation_mode"] == "ref2va" else None
        observed = reference_snapshot(conditioning_packet) if conditioning_packet is not None else None
        expected = first.get("references")
        if manifest["generation_mode"] == "ref2va":
            if conditioning_packet is None or not expected:
                raise ProjectError("hires requires the accepted card's original reference packet")
            if any((observed or {}).get(k) != expected.get(k) for k in ("packet_id", "resources")):
                raise ProjectError("hires reference packet differs from the accepted frozen recipe")
        identity_infos = first.get("identity_anchors")
        if not isinstance(identity_infos, list):
            legacy = first.get("identity_anchor") or {}
            identity_infos = [legacy] if legacy.get("active") else []
        identities = {f"identity_{i}": load_anchor_image(store, _frozen_anchor(manifest, info))
                      for i, info in enumerate(identity_infos)}
        ref_size = str(first.get("ref_image_size") or card.get("ref_image_size", "match"))
        card_reference, card_image = prepare_hires_reference(
            store, first, references=observed, width=width, height=height,
            ref_image_size=ref_size, video_vae=video_vae,
        )
        positive, unused_empty, reference_report = build_conditioning(
            clip=clip, video_vae=video_vae, audio_vae=audio_vae,
            prompt=str(first.get("effective_prompt") or card.get("prompt", "")),
            width=width, height=height, frames=int(card["generated_frame_count"]),
            reference_packet=conditioning_packet, ref_image_size=ref_size,
            card_reference=card_reference, card_reference_image=card_image,
            additional_reference_images=identities or None,
        )
        del unused_empty
        state = first.get("state_anchor") or {}
        if state.get("active"):
            positive = apply_native_anchor(
                positive=positive, latent=work, video_vae=video_vae,
                image=load_anchor_image(store, _frozen_anchor(manifest, state)),
                frame_index=int(state["target_frame_index"]),
            )
        sampling_model = apply_frozen_loras(model, first)
        import torch
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        refine_started = time.perf_counter()
        sampled = sample_h3(model=sampling_model, positive=positive, latent=work,
                            sigmas=sigmas, seed=recipe["seed"], sampler_name="euler")
        diagnostics["refine_seconds"] = time.perf_counter() - refine_started
        assert_hires_unchanged(before, sampled)
        if torch.cuda.is_available():
            diagnostics.update(refine_peak_vram_bytes=torch.cuda.max_memory_allocated(),
                               refine_current_vram_bytes=torch.cuda.memory_allocated())
        LOGGER.info("Hires refinement: %s", diagnostics)
        metadata = {**derivative, "status": "READY", "prefix_protection": protection,
                    "diagnostics": diagnostics, "seed": recipe["seed"],
                    "upscaler_config": diagnostics.get("upscaler_config"),
                    "upscaler_sha256": diagnostics.get("upscaler_sha256"),
                    "precision": diagnostics.get("precision", precision),
                    "resolution": {"width": width, "height": height}}
        process_info = {"contract": "longcaster_hires_derivative_v1",
                        "reference_resolution": reference_report,
                        "audio_handling": AUDIO_HANDLING,
                        "prefix_protection": protection, "diagnostics": diagnostics}
        card_metadata = {k: deepcopy(card.get(k)) for k in (
            "id", "artifact_number", "timeline_index", "generation_parent_id", "prompt", "prompt_hash",
            "seed", "context_frame_count", "generated_frame_count", "actual_new_frame_count", "actual_duration_seconds")}
        card_metadata.update(card_id=card["id"], project_name=manifest["project_name"], schema_version=1)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _, final_path = pack_and_save_refine_derivative(
            path=destination, latent=sampled, card_metadata=card_metadata,
            derivative_metadata=metadata, process_info=process_info, mode=manifest["generation_mode"],
            name=f"{manifest['project_name']} card {card['timeline_index'] + 1} high resolution",
        )
        # Verify serialization preserved the protected AV bytes too.
        saved, _ = primary_latent(load_packet(final_path, verify="full"))
        assert_hires_unchanged(before, saved)
        return store.finish_hires_refine(
            operation_id=operation_id, derivative_path=final_path, artifact_sha256=sha256_file(Path(final_path)),
            execution_seconds=time.perf_counter() - started, prefix_protection=protection, diagnostics=diagnostics,
        )
    except Exception as exc:
        store.fail_hires_refine(operation_id, str(exc))
        destination.unlink(missing_ok=True)
        raise
