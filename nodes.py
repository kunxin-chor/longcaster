from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Any
import uuid

import folder_paths
from comfy_execution.graph_utils import ExecutionBlocker

from .longcaster.duration import H3_CONTINUATION_CONTEXT_FRAMES, resolve_duration
from .longcaster.continuation import DIRECT_LATENT_CONTINUATION
from .longcaster.fingerprint import generation_fingerprint
from .longcaster.h3_runtime import (
    build_conditioning,
    generated_sigmas,
    runtime_capabilities,
    sample_h3,
    sigma_values,
)
from .longcaster.mmh3_adapter import (
    load_packet,
    pack_and_save_card,
    pack_and_save_refine_derivative,
    primary_latent,
    reference_snapshot,
)
from .longcaster.project import ProjectError, ProjectStore, sha256_file
from .longcaster.prompt_sections import hash_prompt, inject_lora_activation_words
from .longcaster.preview import encode_project_preview
from .longcaster.provenance import capture_generation_provenance
from .longcaster.refine import (
    assert_protected_prefix_unchanged,
    prepare_refine_latent,
    refine_seed,
    should_refine_on_accept,
)
from .longcaster.state_anchor import (
    IDENTITY_NATIVE_MECHANISM,
    NATIVE_MECHANISM,
    apply_native_anchor,
    cache_preview_anchor,
    cached_last_frame_anchor,
    continuation_anchor_frame,
    current_state_anchor,
    decode_identity_frame,
    decode_identity_preview,
    extract_last_frame_anchor,
    extract_identity_frame_anchor,
    load_anchor_image,
    persist_identity_image_anchor,
    reinforce_prompt,
)
from .longcaster.timeline_export import export_timeline_nvenc


LOGGER = logging.getLogger("longcaster")


def _projects_root() -> Path:
    return Path(folder_paths.get_output_directory()) / "longcaster_projects"


def _project_from_state(project_name: str, project_state: str | None) -> str:
    if not project_state:
        return project_name
    try:
        state = json.loads(project_state)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProjectError("project_state is not valid LongCaster JSON") from exc
    authoritative_name = state.get("project") if isinstance(state, dict) else None
    if not isinstance(authoritative_name, str) or not authoritative_name:
        raise ProjectError("project_state does not identify a LongCaster project")
    return authoritative_name


def _model_snapshot(model: Any) -> dict[str, Any]:
    model_object = getattr(model, "model", None)
    options = getattr(model, "model_options", {})
    patches = getattr(model, "patches", {})
    return {
        "patcher_type": type(model).__name__,
        "model_type": type(model_object).__name__ if model_object is not None else None,
        "model_option_keys": sorted(str(key) for key in options) if isinstance(options, dict) else [],
        "patch_key_count": len(patches) if isinstance(patches, dict) else None,
    }


def _status(manifest: dict[str, Any], store: ProjectStore, message: str) -> dict[str, Any]:
    card = store.active_card(manifest)
    identity = store.active_identity_anchor(manifest, include_disabled=True)
    return {
        "message": message,
        "project": manifest["project_name"],
        "revision": manifest["revision"],
        "generation_mode": manifest["generation_mode"],
        "width": manifest["width"],
        "height": manifest["height"],
        "active_card": card,
        "card_count": len(manifest["cards"]),
        "active_identity_anchor": identity,
        "artifact_errors": store.validate_artifacts(manifest),
        "runtime": runtime_capabilities(),
    }


def _packet_for_card(store: ProjectStore, card: dict[str, Any]) -> tuple[Any | None, dict[str, Any] | None]:
    relative = card.get("master_path") or card.get("draft_path")
    if not relative:
        return None, None
    path = store.absolute_path(relative)
    if not path.is_file() or sha256_file(path) != card.get("artifact_sha256"):
        raise ProjectError(f"card {card['artifact_number']} archive is missing or corrupt")
    packet = load_packet(path, verify="on_access")
    latent, _ = primary_latent(packet)
    return packet, latent


def _packet_for_artifact(
    store: ProjectStore, *, relative: str, artifact_sha256: str, label: str
) -> tuple[Any, dict[str, Any]]:
    path = store.absolute_path(relative)
    if not path.is_file() or sha256_file(path) != artifact_sha256:
        raise ProjectError(f"{label} archive is missing or corrupt")
    packet = load_packet(path, verify="on_access")
    latent, _ = primary_latent(packet)
    return packet, latent


def _anchor_by_id(manifest: dict[str, Any], anchor_id: str | None) -> dict[str, Any] | None:
    if not anchor_id:
        return None
    return next(
        (
            anchor
            for card in manifest.get("cards", [])
            for anchor in card.get("anchors", [])
            if anchor.get("anchor_id") == anchor_id
        ),
        None,
    )


class LongCasterProject:
    """Persistent fixed-mode H3 card controller with direct AV continuation."""

    @classmethod
    def INPUT_TYPES(cls):
        import comfy.samplers

        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "video_vae": ("VAE",),
                "audio_vae": ("VAE",),
                "project_name": ("STRING", {"default": "longcaster_project"}),
                "action": (["resume", "cancel", "generate", "retry", "accept", "create_refine", "unpublish", "invalidate", "invalidate_all", "remove_draft", "append"], {"default": "resume"}),
                "generation_mode": (["ref2va", "t2va"], {"default": "ref2va"}),
                "prompt": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": True}),
                "duration_seconds": ("FLOAT", {"default": 5.0, "min": 0.1, "max": 120.0, "step": 0.1}),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 0xFFFFFFFFFFFFFFFF,
                        "control_after_generate": True,
                    },
                ),
                "width": ("INT", {"default": 1344, "min": 32, "max": 8192, "step": 32}),
                "height": ("INT", {"default": 768, "min": 32, "max": 8192, "step": 32}),
                "steps": (
                    "INT",
                    {
                        "default": 20,
                        "min": 1,
                        "max": 10000,
                        "tooltip": "Used only when SIGMAS is not connected. PDD step count is set on PDD Apply.",
                    },
                ),
                "scheduler": (
                    comfy.samplers.SCHEDULER_NAMES,
                    {"tooltip": "Used only when SIGMAS is not connected. External PDD SIGMAS bypass this scheduler."},
                ),
                "sampler_name": (comfy.samplers.SAMPLER_NAMES, {"default": "euler"}),
                "require_external_sigmas": ("BOOLEAN", {"default": True}),
                "ref_image_size": (["match", "max"], {"default": "match"}),
                "command_id": ("STRING", {"default": "resume-1"}),
                "auto_state_anchor": ("BOOLEAN", {"default": True}),
                "reinforce_state_prompt": ("BOOLEAN", {"default": True}),
                "use_identity_anchor": ("BOOLEAN", {"default": True}),
                "refine_cadence": (
                    ["off", "every_accepted_card", "manual"],
                    {"default": "off"},
                ),
                "refine_sampler_name": (comfy.samplers.SAMPLER_NAMES, {"default": "euler"}),
                "refine_seed_mode": (["inherit", "offset"], {"default": "inherit"}),
            },
            "optional": {
                "sigmas": ("SIGMAS",),
                "reference_packet": ("MMH3_MEDIA",),
                "refine_sigmas": ("SIGMAS",),
                "refine_model": ("MODEL",),
            },
            "hidden": {
                "prompt_graph": "PROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    RETURN_TYPES = ("MMH3_MEDIA", "LATENT", "STRING", "STRING")
    RETURN_NAMES = ("card_packet", "card_latent", "project_state", "project_path")
    FUNCTION = "execute"
    CATEGORY = "MiniMax H3/LongCaster"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Runs one persistent H3 card operation. Connect PDD-ACC's patched MODEL and exact SIGMAS; "
        "connect an MMH3 reference packet for fixed REF2VA projects."
    )

    @classmethod
    def IS_CHANGED(cls, command_id="", action="", project_name="", **kwargs):
        return f"{project_name}:{action}:{command_id}"

    def execute(
        self,
        model,
        clip,
        video_vae,
        audio_vae,
        project_name,
        action,
        generation_mode,
        prompt,
        duration_seconds,
        seed,
        width,
        height,
        steps,
        scheduler,
        sampler_name,
        require_external_sigmas,
        ref_image_size,
        command_id,
        auto_state_anchor=True,
        reinforce_state_prompt=True,
        use_identity_anchor=True,
        refine_cadence="off",
        refine_sampler_name="euler",
        refine_seed_mode="inherit",
        sigmas=None,
        reference_packet=None,
        refine_sigmas=None,
        refine_model=None,
        prompt_graph=None,
        unique_id=None,
    ):
        store = ProjectStore(_projects_root(), project_name)
        if not store.manifest_path.exists():
            manifest = store.create(
                prompt=prompt,
                duration_seconds=duration_seconds,
                seed=seed,
                width=width,
                height=height,
                generation_mode=generation_mode,
                refine_cadence=refine_cadence,
            )
        else:
            manifest = store.load()

        # Linked resolution inputs configure new projects; saved projects own their canvas.
        width, height = int(manifest["width"]), int(manifest["height"])

        if action == "cancel":
            manifest, cancelled = store.cancel_pending()
            message = "Render stopped and project unlocked." if cancelled else "Project was already unlocked."
            return self._result(None, None, _status(manifest, store, message), store)

        if manifest["generation_mode"] != generation_mode:
            raise ProjectError(
                f"project mode is fixed at {manifest['generation_mode']}; the node requested {generation_mode}"
            )
        manifest = store.set_refine_cadence(refine_cadence)
        if action == "resume":
            return self._result(None, None, _status(manifest, store, "Project resumed."), store)

        if action == "accept":
            draft_card = store.active_card(manifest)
            anchor = cached_last_frame_anchor(store, draft_card)
            if anchor is None:
                draft_packet, _ = _packet_for_card(store, draft_card)
                if draft_packet is None:
                    raise ProjectError("accept requires a decoded draft for current-state anchor extraction")
                anchor = extract_last_frame_anchor(
                    store=store,
                    card=draft_card,
                    packet=draft_packet,
                    video_vae=video_vae,
                )
            manifest = store.accept(anchor=anchor)
            accepted_card = store.active_card(manifest)
            cadence = manifest.get("refine_cadence", "off")
            should_refine = should_refine_on_accept(
                cadence, accepted_card.get("refine_enabled", False)
            )
            if should_refine:
                try:
                    manifest = self._create_refine(
                        store=store,
                        manifest=manifest,
                        model=refine_model or model,
                        model_source=("refine_model" if refine_model is not None else "first_pass_model"),
                        clip=clip,
                        video_vae=video_vae,
                        audio_vae=audio_vae,
                        reference_packet=reference_packet,
                        ref_image_size=ref_image_size,
                        refine_sigmas=refine_sigmas,
                        sampler_name=refine_sampler_name,
                        seed_mode=refine_seed_mode,
                        cadence=cadence,
                        use_as_default=True,
                    )
                    message = (
                        "Draft accepted as an immutable master; continuation refine is ready "
                        "and selected for the next card."
                    )
                except Exception as exc:
                    LOGGER.exception("LongCaster continuation refine failed after acceptance")
                    manifest = store.load()
                    message = (
                        "Draft accepted as an immutable master, but continuation refine failed; "
                        f"the accepted master remains the continuation fallback. {exc}"
                    )
            else:
                message = "Draft accepted as an immutable master; current-state anchor saved."
            manifest, advanced = store.activate_next_invalidated(accepted_card["id"])
            if advanced:
                message += " The next invalidated card is now active for regeneration."
            return self._result(
                None,
                None,
                _status(manifest, store, message),
                store,
            )

        if action == "create_refine":
            cadence = manifest.get("refine_cadence", "off")
            if cadence == "off":
                raise ProjectError("Generate Refine is disabled while continuation refine is Off")
            accepted_card = store.active_card(manifest)
            use_as_default = cadence == "every_accepted_card" or bool(
                accepted_card.get("refine_enabled", False)
            )
            try:
                manifest = self._create_refine(
                    store=store,
                    manifest=manifest,
                    model=refine_model or model,
                    model_source=("refine_model" if refine_model is not None else "first_pass_model"),
                    clip=clip,
                    video_vae=video_vae,
                    audio_vae=audio_vae,
                    reference_packet=reference_packet,
                    ref_image_size=ref_image_size,
                    refine_sigmas=refine_sigmas,
                    sampler_name=refine_sampler_name,
                    seed_mode=refine_seed_mode,
                    cadence=cadence,
                    use_as_default=use_as_default,
                )
                message = (
                    "Continuation refine created and selected for continuation."
                    if use_as_default
                    else "Continuation refine created; choose it explicitly to use it for continuation."
                )
            except Exception as exc:
                LOGGER.exception("LongCaster manual continuation refine failed")
                manifest = store.load()
                message = f"Continuation refine failed; the accepted master remains selected. {exc}"
            return self._result(None, None, _status(manifest, store, message), store)

        if action == "unpublish":
            manifest = store.unpublish_tail()
            return self._result(
                None,
                None,
                _status(
                    manifest,
                    store,
                    "Latest accepted card reopened as a draft; its prior immutable master was retained and its refine derivatives were removed.",
                ),
                store,
            )

        if action == "invalidate":
            manifest = store.invalidate_render()
            return self._result(
                None,
                None,
                _status(
                    manifest,
                    store,
                    "Card render invalidated; its prompt was retained and its associated render artifacts were removed.",
                ),
                store,
            )

        if action == "invalidate_all":
            manifest = store.invalidate_all_renders()
            return self._result(
                None,
                None,
                _status(
                    manifest,
                    store,
                    "All card renders were invalidated; prompts were retained and Card 1 is active for regeneration.",
                ),
                store,
            )

        if action == "remove_draft":
            manifest = store.remove_draft_tail()
            return self._result(
                None,
                None,
                _status(manifest, store, "Draft card removed; previous accepted card is active."),
                store,
            )

        if action == "append":
            manifest = store.append(prompt="", duration_seconds=duration_seconds, seed=seed)
            return self._result(None, None, _status(manifest, store, "New empty card appended."), store)

        if action not in {"generate", "retry"}:
            raise ProjectError(f"unknown action: {action}")
        if generation_mode == "ref2va" and reference_packet is None:
            raise ProjectError("REF2VA project generation requires reference_packet")
        if generation_mode == "t2va" and reference_packet is not None:
            raise ProjectError("This project is fixed to T2VA; remove reference_packet or create a REF2VA project")
        if require_external_sigmas and sigmas is None:
            raise ProjectError(
                "External SIGMAS are required. Connect the exact SIGMAS output from PDD Apply, "
                "or disable require_external_sigmas for standard sampling."
            )
        if require_external_sigmas and sampler_name != "euler":
            raise ProjectError("PDD sampling requires sampler_name=euler")

        current = store.active_card(manifest)
        if current.get("prompt_format") == "structured_v1":
            prompt = current["assembled_prompt"]
        parent = (
            store.parent_card(manifest, current)
            if current.get("continuation_strategy") == "direct_mmh3"
            else None
        )
        continuation_source = store.continuation_artifact(manifest, current) if parent else None
        context_frames = H3_CONTINUATION_CONTEXT_FRAMES if parent else 0
        duration = resolve_duration(duration_seconds, context_frames=context_frames)
        selected_sigmas = sigmas if sigmas is not None else generated_sigmas(model, scheduler, steps)
        sigma_schedule = sigma_values(selected_sigmas)
        references = reference_snapshot(reference_packet)
        identity_anchor = (
            store.active_identity_anchor(manifest) if use_identity_anchor else None
        )
        identity_image = None
        identity_picture_index = None
        if identity_anchor is not None:
            if generation_mode != "ref2va" or reference_packet is None:
                raise ProjectError("identity anchors currently require a REF2VA project and reference packet")
            identity_image = load_anchor_image(store, identity_anchor)
            identity_picture_index = 1 + sum(
                1 for item in (references or {}).get("resources", []) if item.get("kind") == "image"
            )
        parent_packet = None
        anchor = None
        anchor_image = None
        anchor_frame_index = None
        effective_prompt = prompt
        prompt_reinforced = False
        prompt_sections: list[str] = []
        if parent and auto_state_anchor:
            parent_packet, _ = _packet_for_card(store, parent)
            if parent_packet is None:
                raise ProjectError("accepted continuation parent has no MMH3 master")
            anchor = current_state_anchor(parent)
            has_current_state_record = any(
                item.get("role") == "current_state" for item in parent.get("anchors", [])
            )
            if anchor is None and not has_current_state_record:
                anchor = extract_last_frame_anchor(
                    store=store,
                    card=parent,
                    packet=parent_packet,
                    video_vae=video_vae,
                )
                manifest = store.add_anchor(parent["id"], anchor)
                current = store.active_card(manifest)
                parent = store.parent_card(manifest, current)
            if anchor is not None:
                anchor_image = load_anchor_image(store, anchor)
                anchor_frame_index = continuation_anchor_frame(context_frames)
                LOGGER.info(
                    "LongCaster state anchor active=True source_card_id=%s frame_index=%s "
                    "timestamp=%.3fs asset=%s mechanism=%s target_frame_index=%d "
                    "prompt_reinforcement=pending",
                    anchor["source_card_id"],
                    anchor["source_frame_index"],
                    float(anchor["source_timestamp_seconds"]),
                    store.absolute_path(anchor["asset_path"]),
                    NATIVE_MECHANISM,
                    anchor_frame_index,
                )
            else:
                LOGGER.info(
                    "LongCaster state anchor active=False reason=no_enabled_current_state_anchor "
                    "source_card_id=%s prompt_reinforcement=False",
                    parent["id"],
                )
        else:
            reason = "first_card" if parent is None else "disabled"
            LOGGER.info("LongCaster state anchor active=False reason=%s prompt_reinforcement=False", reason)
        if reinforce_state_prompt and (anchor is not None or identity_anchor is not None):
            effective_prompt, prompt_reinforced, prompt_sections = reinforce_prompt(
                prompt,
                current_state_active=anchor is not None,
                identity_subject_id=identity_anchor.get("subject_id") if identity_anchor else None,
                identity_picture_index=identity_picture_index,
                identity_scope=identity_anchor.get("identity_scope", "face_only") if identity_anchor else "face_only",
                custom_identity_instruction=identity_anchor.get("custom_identity_instruction") if identity_anchor else None,
            )
        lora_activation_words = manifest.get("lora_activation_words", "")
        effective_prompt = inject_lora_activation_words(
            effective_prompt, lora_activation_words
        )
        LOGGER.info(
            "LongCaster anchor prompt reinforcement injected=%s sections=%s",
            prompt_reinforced, ",".join(prompt_sections) or "none",
        )
        identity_diagnostic = {
            "active": identity_anchor is not None,
            "anchor_id": identity_anchor.get("anchor_id") if identity_anchor else None,
            "source_card_id": identity_anchor.get("source_card_id") if identity_anchor else None,
            "source_preview_frame_index": identity_anchor.get("source_preview_frame_index") if identity_anchor else None,
            "source_frame_index": identity_anchor.get("source_frame_index") if identity_anchor else None,
            "source_timestamp_seconds": identity_anchor.get("source_timestamp_seconds") if identity_anchor else None,
            "asset_path": identity_anchor.get("asset_path") if identity_anchor else None,
            "subject_id": identity_anchor.get("subject_id") if identity_anchor else None,
            "mechanism": IDENTITY_NATIVE_MECHANISM if identity_anchor else None,
            "target_frame_index": None,
            "reference_picture_index": identity_picture_index,
            "identity_scope": identity_anchor.get("identity_scope", "face_only") if identity_anchor else None,
            "prompt_reinforcement_injected": bool(prompt_reinforced and identity_anchor),
        }
        LOGGER.info(
            "LongCaster identity anchor active=%s anchor_id=%s source_card_id=%s "
            "preview_frame_index=%s source_frame_index=%s asset=%s subject_id=%s scope=%s "
            "mechanism=%s target_frame_index=none picture_index=%s both_anchors_active=%s "
            "prompt_reinforcement=%s",
            identity_anchor is not None,
            identity_diagnostic["anchor_id"], identity_diagnostic["source_card_id"],
            identity_diagnostic["source_preview_frame_index"], identity_diagnostic["source_frame_index"],
            store.absolute_path(identity_anchor["asset_path"]) if identity_anchor else None,
            identity_diagnostic["subject_id"], identity_diagnostic["identity_scope"], identity_diagnostic["mechanism"],
            identity_picture_index, bool(identity_anchor and anchor),
            identity_diagnostic["prompt_reinforcement_injected"],
        )
        anchor_diagnostic = {
            "active": anchor is not None,
            "role": "current_state" if anchor is not None else None,
            "source_card_id": anchor.get("source_card_id") if anchor else None,
            "source_frame_index": anchor.get("source_frame_index") if anchor else None,
            "source_timestamp_seconds": anchor.get("source_timestamp_seconds") if anchor else None,
            "asset_path": anchor.get("asset_path") if anchor else None,
            "anchor_id": anchor.get("anchor_id") if anchor else None,
            "mechanism": NATIVE_MECHANISM if anchor else None,
            "target_frame_index": anchor_frame_index,
            "prompt_reinforcement_injected": bool(prompt_reinforced and anchor),
            "prompt_sections": prompt_sections,
        }
        upstream_provenance = capture_generation_provenance(prompt_graph, unique_id)
        recipe = {
            "version": 1,
            "project_mode": generation_mode,
            "prompt": prompt,
            "prompt_hash": hash_prompt(prompt),
            "effective_prompt": effective_prompt,
            "effective_prompt_hash": hash_prompt(effective_prompt),
            "lora_activation_words": lora_activation_words,
            "requested_duration_seconds": float(duration_seconds),
            "duration_plan": duration.__dict__,
            "seed": int(seed),
            "canvas": {"width": int(width), "height": int(height)},
            "sampler": sampler_name,
            "scheduler": scheduler if sigmas is None else "external",
            "sigmas": sigma_schedule,
            "external_sigmas": sigmas is not None,
            "model": _model_snapshot(model),
            "upstream_provenance": upstream_provenance,
            "parent_sha256": (
                continuation_source.get("artifact_sha256") if continuation_source else None
            ),
            "continuation_source": continuation_source,
            "references": references,
            "ref_image_size": ref_image_size,
            "state_anchor": anchor_diagnostic,
            "identity_anchor": identity_diagnostic,
            "runtime": runtime_capabilities(),
        }
        fingerprint = generation_fingerprint(recipe)
        manifest, started_card = store.begin_generation(
            action=action,
            prompt=prompt,
            duration_seconds=duration_seconds,
            seed=seed,
            recipe=recipe,
            fingerprint=fingerprint,
        )
        operation_id = manifest["pending_operation"]["id"]
        destination = store.draft_destination(started_card)
        try:
            positive, empty_latent, reference_report = build_conditioning(
                clip=clip,
                video_vae=video_vae,
                audio_vae=audio_vae,
                prompt=effective_prompt,
                width=width,
                height=height,
                frames=duration.generated_frames,
                reference_packet=reference_packet,
                ref_image_size=ref_image_size,
                additional_reference_images={"identity": identity_image} if identity_image is not None else None,
            )
            continuation_plan = None
            target_latent = empty_latent
            if parent:
                if continuation_source is None:
                    raise ProjectError("direct continuation has no resolved source")
                parent_packet, _ = _packet_for_artifact(
                    store,
                    relative=continuation_source["path"],
                    artifact_sha256=continuation_source["artifact_sha256"],
                    label="continuation source",
                )
                target_latent, continuation_plan = DIRECT_LATENT_CONTINUATION.prepare(
                    parent_packet,
                    target_frames=duration.generated_frames,
                    width=width,
                    height=height,
                    context_frames=context_frames,
                )
            if anchor_image is not None:
                positive = apply_native_anchor(
                    positive=positive,
                    latent=target_latent,
                    video_vae=video_vae,
                    image=anchor_image,
                    frame_index=anchor_frame_index,
                )
            sampled = sample_h3(
                model=model,
                positive=positive,
                latent=target_latent,
                sigmas=selected_sigmas,
                seed=seed,
                sampler_name=sampler_name,
            )
            card_metadata = {
                "schema_version": 1,
                "project_name": project_name,
                "card_id": started_card["id"],
                "artifact_number": started_card["artifact_number"],
                "timeline_index": started_card["timeline_index"],
                "attempt": started_card["attempt"] + 1,
                "generation_parent_id": started_card.get("generation_parent_id"),
                "timeline_predecessor_id": started_card.get("timeline_predecessor_id"),
                "continuation_strategy": started_card.get("continuation_strategy"),
                "continuation_source": continuation_source,
                "prompt": prompt,
                "prompt_hash": hash_prompt(prompt),
                "prompt_sections": started_card.get("prompt_sections") if started_card.get("prompt_format") == "structured_v1" else None,
                "seed": int(seed),
                "generation_fingerprint": fingerprint,
                "context_frame_count": context_frames,
                "generated_frame_count": duration.generated_frames,
                "actual_new_frame_count": duration.actual_new_frames,
                "actual_duration_seconds": duration.actual_new_seconds,
                "reference_packet_id": references.get("packet_id") if references else None,
            }
            process_info = {
                "contract": "longcaster_card_v1",
                "external_sigmas": sigmas is not None,
                "sigmas_sha256": generation_fingerprint({"sigmas": sigma_schedule}),
                "reference_resolution": reference_report,
                "continuation": continuation_plan,
                "state_anchor": anchor_diagnostic,
                "identity_anchor": identity_diagnostic,
                "effective_prompt": effective_prompt,
                "recipe_fingerprint": fingerprint,
            }
            saved_packet, final_path = pack_and_save_card(
                path=destination,
                latent=sampled,
                card_metadata=card_metadata,
                process_info=process_info,
                mode=generation_mode,
                name=f"{project_name} card {started_card['artifact_number']:04d}",
            )
            artifact_hash = sha256_file(Path(final_path))
            manifest = store.finish_generation(
                operation_id=operation_id,
                draft_path=final_path,
                artifact_sha256=artifact_hash,
                context_frame_count=context_frames,
                generated_frame_count=duration.generated_frames,
                actual_new_frame_count=duration.actual_new_frames,
                actual_duration_seconds=duration.actual_new_seconds,
            )
            return self._result(
                saved_packet,
                {key: value for key, value in sampled.items() if key != "noise_mask"},
                _status(manifest, store, "Draft generated. Review it, then Retry or Accept."),
                store,
            )
        except Exception as exc:
            store.fail_generation(operation_id, str(exc))
            destination.unlink(missing_ok=True)
            raise

    def _create_refine(
        self,
        *,
        store: ProjectStore,
        manifest: dict[str, Any],
        model: Any,
        model_source: str,
        clip: Any,
        video_vae: Any,
        audio_vae: Any,
        reference_packet: Any | None,
        ref_image_size: str,
        refine_sigmas: Any,
        sampler_name: str,
        seed_mode: str,
        cadence: str,
        use_as_default: bool,
    ) -> dict[str, Any]:
        card = store.active_card(manifest)
        if card.get("status") != "ACCEPTED":
            raise ProjectError("continuation refine requires the active card to be accepted")
        first_recipe = card.get("recipe") or {}
        schedule = []
        sigma_error = None
        if refine_sigmas is not None:
            try:
                schedule = sigma_values(refine_sigmas)
            except Exception as exc:
                sigma_error = exc
        seed_value = refine_seed(int(card["seed"]), seed_mode)
        audio_handling = (
            "joint_av_second_sample_with_reconstructed_audio_prefix_lock"
            if int(card.get("context_frame_count") or 0) > 0
            else "joint_av_second_sample_no_inherited_prefix"
        )
        refine_recipe = {
            "version": 1,
            "passes": 1,
            "resolution": {
                "width": int(manifest["width"]),
                "height": int(manifest["height"]),
            },
            "model_source": str(model_source),
            "model": _model_snapshot(model),
            "sampler": str(sampler_name),
            "sigmas": schedule,
            "sigmas_sha256": generation_fingerprint({"sigmas": schedule}),
            "steps": max(0, len(schedule) - 1),
            "schedule": "external_sigmas",
            "denoise": "encoded_by_external_sigmas",
            "seed": seed_value,
            "seed_mode": str(seed_mode),
            "conditioning": "reconstructed_first_pass",
            "lora_activation_words": first_recipe.get("lora_activation_words", ""),
            "audio_handling": audio_handling,
        }
        manifest, derivative = store.begin_refine(
            card_id=card["id"],
            cadence=cadence,
            recipe=refine_recipe,
            use_as_default=use_as_default,
        )
        operation_id = manifest["pending_operation"]["id"]
        destination = store.derivative_destination(card, derivative["id"])
        started = time.perf_counter()
        try:
            if refine_sigmas is None:
                raise ProjectError(
                    "refine requires a separate SIGMAS connection; first-pass SIGMAS are not reused implicitly"
                )
            if sigma_error is not None:
                raise ProjectError(f"invalid refine SIGMAS: {sigma_error}")
            expected_references = (first_recipe.get("references") or {}).get("packet_id")
            conditioning_packet = reference_packet if manifest["generation_mode"] == "ref2va" else None
            if manifest["generation_mode"] == "ref2va":
                if conditioning_packet is None:
                    raise ProjectError("REF2VA refine requires the original reference_packet connection")
                observed_references = (reference_snapshot(conditioning_packet) or {}).get("packet_id")
                if expected_references and observed_references != expected_references:
                    raise ProjectError(
                        "connected reference packet does not match the accepted card recipe"
                    )

            identity_info = first_recipe.get("identity_anchor") or {}
            identity_anchor = (
                _anchor_by_id(manifest, identity_info.get("anchor_id"))
                if identity_info.get("active")
                else None
            )
            if identity_info.get("active") and identity_anchor is None:
                raise ProjectError("accepted card identity anchor is no longer available")
            identity_image = load_anchor_image(store, identity_anchor) if identity_anchor else None

            effective_prompt = str(first_recipe.get("effective_prompt") or card.get("prompt", ""))
            positive, _unused_empty, reference_report = build_conditioning(
                clip=clip,
                video_vae=video_vae,
                audio_vae=audio_vae,
                prompt=effective_prompt,
                width=int(manifest["width"]),
                height=int(manifest["height"]),
                frames=int(card["generated_frame_count"]),
                reference_packet=conditioning_packet,
                ref_image_size=str(first_recipe.get("ref_image_size") or ref_image_size),
                additional_reference_images=(
                    {"identity": identity_image} if identity_image is not None else None
                ),
            )

            state_info = first_recipe.get("state_anchor") or {}
            state_anchor = None
            if state_info.get("active"):
                state_anchor = _anchor_by_id(manifest, state_info.get("anchor_id"))
                if state_anchor is None:
                    raise ProjectError("accepted card state anchor is no longer available")

            accepted_packet, accepted_latent = _packet_for_card(store, card)
            if accepted_packet is None or accepted_latent is None:
                raise ProjectError("accepted refine source is unavailable")
            continuation_lock = None
            lock_plan = None
            if int(card.get("context_frame_count") or 0) > 0:
                source = store.continuation_artifact(manifest, card)
                if source is None:
                    raise ProjectError("continued card has no recorded continuation source")
                source_packet, _ = _packet_for_artifact(
                    store,
                    relative=source["path"],
                    artifact_sha256=source["artifact_sha256"],
                    label="refine prefix source",
                )
                continuation_lock, lock_plan = DIRECT_LATENT_CONTINUATION.prepare(
                    source_packet,
                    target_frames=int(card["generated_frame_count"]),
                    width=int(manifest["width"]),
                    height=int(manifest["height"]),
                    context_frames=int(card["context_frame_count"]),
                )

            refine_input, protection = prepare_refine_latent(
                accepted_latent, continuation_lock
            )
            if state_anchor is not None:
                positive = apply_native_anchor(
                    positive=positive,
                    latent=refine_input,
                    video_vae=video_vae,
                    image=load_anchor_image(store, state_anchor),
                    frame_index=int(state_info["target_frame_index"]),
                )
            sampled = sample_h3(
                model=model,
                positive=positive,
                latent=refine_input,
                sigmas=refine_sigmas,
                seed=seed_value,
                sampler_name=str(sampler_name),
            )
            assert_protected_prefix_unchanged(refine_input, sampled)

            prefix_report = protection.to_dict()
            prefix_report["continuation_plan"] = lock_plan
            derivative_metadata = {
                "schema_version": 1,
                "derivative_id": derivative["id"],
                "type": "refine",
                "source_card_id": card["id"],
                "source_master_path": card["master_path"],
                "source_artifact_sha256": card["artifact_sha256"],
                "cadence": cadence,
                "model": refine_recipe["model"],
                "model_source": refine_recipe["model_source"],
                "sampler": str(sampler_name),
                "sigmas_sha256": refine_recipe["sigmas_sha256"],
                "sigmas": schedule,
                "steps": refine_recipe["steps"],
                "seed": seed_value,
                "seed_mode": str(seed_mode),
                "resolution": refine_recipe["resolution"],
                "prefix_protection": prefix_report,
                "audio_handling": refine_recipe["audio_handling"],
            }
            process_info = {
                "contract": "longcaster_refine_derivative_v1",
                "source_recipe_fingerprint": card.get("generation_fingerprint"),
                "refine_recipe_fingerprint": generation_fingerprint(refine_recipe),
                "reference_resolution": reference_report,
                "prefix_protection": prefix_report,
                "audio_handling": refine_recipe["audio_handling"],
            }
            card_metadata = {
                "schema_version": 1,
                "project_name": manifest["project_name"],
                "card_id": card["id"],
                "artifact_number": card["artifact_number"],
                "timeline_index": card["timeline_index"],
                "generation_parent_id": card.get("generation_parent_id"),
                "continuation_source": card.get("continuation_source"),
                "prompt": card.get("prompt", ""),
                "prompt_hash": card.get("prompt_hash"),
                "seed": card.get("seed"),
                "context_frame_count": card.get("context_frame_count"),
                "generated_frame_count": card.get("generated_frame_count"),
                "actual_new_frame_count": card.get("actual_new_frame_count"),
                "actual_duration_seconds": card.get("actual_duration_seconds"),
            }
            destination.parent.mkdir(parents=True, exist_ok=True)
            _saved_packet, final_path = pack_and_save_refine_derivative(
                path=destination,
                latent=sampled,
                card_metadata=card_metadata,
                derivative_metadata=derivative_metadata,
                process_info=process_info,
                mode=manifest["generation_mode"],
                name=(
                    f"{manifest['project_name']} card {card['artifact_number']:04d} "
                    "continuation refine"
                ),
            )
            derivative_hash = sha256_file(Path(final_path))
            return store.finish_refine(
                operation_id=operation_id,
                derivative_path=final_path,
                artifact_sha256=derivative_hash,
                execution_seconds=time.perf_counter() - started,
                prefix_protection=prefix_report,
                audio_handling=refine_recipe["audio_handling"],
            )
        except Exception as exc:
            store.fail_refine(operation_id, str(exc))
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _result(packet, latent, status, store):
        state_json = json.dumps(status, indent=2, ensure_ascii=False, sort_keys=True)
        # A new or interrupted project may legitimately have no draft/master yet.
        # Block only consumers of those unavailable outputs so status/path outputs
        # still update and downstream decode/export nodes do not receive None.
        packet_output = packet if packet is not None else ExecutionBlocker(None)
        latent_output = latent if latent is not None else ExecutionBlocker(None)
        return {
            "ui": {"longcaster_state": [status]},
            "result": (packet_output, latent_output, state_json, str(store.path)),
        }


class LongCasterIdentityAnchor:
    """Select and persist a generated frame as the project's facial identity checkpoint."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project_name": ("STRING", {"default": "longcaster_project"}),
                "action": (["inspect", "build_preview", "preview", "set", "enable", "disable", "clear"], {"default": "inspect"}),
                "source_card": (
                    "STRING",
                    {
                        "default": "1",
                        "tooltip": "Accepted card number or UUID. Numbers are resolved to stable card UUIDs.",
                    },
                ),
                "frame_index": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": 100000,
                        "tooltip": "Frame in the visible card preview; continuation-prefix frames are excluded.",
                    },
                ),
                "subject_id": ("STRING", {"default": "<Subject 1>"}),
                "label": ("STRING", {"default": "identity checkpoint"}),
                "command_id": ("STRING", {"default": "identity-1"}),
                "identity_scope": (
                    ["face_only", "face_clothing", "face_body", "everything", "custom"],
                    {
                        "default": "face_only",
                        "tooltip": "Controls which visible attributes the prompt may inherit from the identity picture.",
                    },
                ),
                "custom_identity_instruction": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "tooltip": "Required when identity_scope=custom. Describe exactly what may be inherited from the anchor.",
                    },
                ),
            },
            "optional": {
                "video_vae": ("VAE",),
                "selected_image": (
                    "IMAGE",
                    {"tooltip": "Optional single frame from VHS or another image selector. Avoids decoding the MMH3 source."},
                ),
                "project_state": (
                    "STRING",
                    {
                        "forceInput": True,
                        "tooltip": "Connect LongCaster Project project_state. It overrides project_name.",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "IMAGE")
    RETURN_NAMES = ("identity_state", "anchor_path", "selected_frame")
    FUNCTION = "execute"
    CATEGORY = "MiniMax H3/LongCaster"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Selects a preview-relative frame from an immutable accepted card and persists it as "
        "the active native H3 facial-identity reference."
    )

    @classmethod
    def IS_CHANGED(cls, project_name="", action="", command_id="", **kwargs):
        return f"{project_name}:{action}:{command_id}"

    def execute(
        self, project_name, action, source_card, frame_index,
        subject_id, label, command_id, identity_scope, custom_identity_instruction,
        video_vae=None, selected_image=None, project_state=None,
    ):
        del command_id
        project_name = _project_from_state(project_name, project_state)
        store = ProjectStore(_projects_root(), project_name)
        manifest = store.load()
        subject = str(subject_id).strip()
        if not subject:
            raise ProjectError("subject_id cannot be empty")

        message = "Identity anchor state inspected."
        preview_image = None
        if action in {"build_preview", "preview", "set"}:
            if manifest.get("generation_mode") != "ref2va":
                raise ProjectError("manual identity anchors currently require a REF2VA project")
            source = store.accepted_card(manifest, source_card)
            if action == "build_preview":
                if video_vae is None:
                    raise ProjectError("building a card preview requires a connected video_vae")
                packet, _ = _packet_for_card(store, source)
                if packet is None:
                    raise ProjectError("accepted identity source has no MMH3 master")
                images = decode_identity_preview(card=source, packet=packet, video_vae=video_vae)
                manifest, preview_path = encode_project_preview(store=store, card=source, images=images)
                message = f"Project preview built for accepted card {source['artifact_number']}: {preview_path}"
            elif action == "preview":
                if selected_image is not None:
                    if len(selected_image.shape) != 4 or int(selected_image.shape[0]) != 1:
                        raise ProjectError("selected_image must contain exactly one IMAGE frame")
                    preview_image = selected_image
                    preview_index = int(frame_index)
                else:
                    if video_vae is None:
                        raise ProjectError("preview requires selected_image or a connected video_vae")
                    packet, _ = _packet_for_card(store, source)
                    if packet is None:
                        raise ProjectError("accepted identity source has no MMH3 master")
                    preview_image, preview_index, _ = decode_identity_frame(
                        card=source,
                        packet=packet,
                        video_vae=video_vae,
                        preview_frame_index=frame_index,
                    )
                message = (
                    f"Previewing accepted card {source['artifact_number']}, frame {preview_index} "
                    f"({preview_index / 24.0:.3f}s)."
                )
            else:
                if selected_image is not None:
                    anchor = persist_identity_image_anchor(
                        store=store,
                        card=source,
                        image=selected_image,
                        preview_frame_index=frame_index,
                        subject_id=subject,
                        label=label,
                        identity_scope=identity_scope,
                        custom_identity_instruction=custom_identity_instruction,
                    )
                else:
                    if video_vae is None:
                        raise ProjectError("setting an identity anchor requires selected_image or video_vae")
                    packet, _ = _packet_for_card(store, source)
                    if packet is None:
                        raise ProjectError("accepted identity source has no MMH3 master")
                    anchor = extract_identity_frame_anchor(
                        store=store,
                        card=source,
                        packet=packet,
                        video_vae=video_vae,
                        preview_frame_index=frame_index,
                        subject_id=subject,
                        label=label,
                        identity_scope=identity_scope,
                        custom_identity_instruction=custom_identity_instruction,
                    )
                try:
                    manifest = store.add_identity_anchor(source["id"], anchor)
                except Exception:
                    store.absolute_path(anchor["asset_path"]).unlink(missing_ok=True)
                    raise
                preview_image = load_anchor_image(store, anchor)
                message = (
                    f"Identity anchor set from accepted card {source['artifact_number']}, "
                    f"preview frame {int(frame_index)} ({int(frame_index) / 24.0:.3f}s)."
                )
        elif action == "enable":
            manifest = store.set_identity_anchor_enabled(subject, True)
            message = f"Identity anchor enabled for {subject}."
        elif action == "disable":
            manifest = store.set_identity_anchor_enabled(subject, False)
            message = f"Identity anchor disabled for {subject}."
        elif action == "clear":
            manifest = store.clear_identity_anchor(subject)
            message = f"Active identity anchor cleared for {subject}; its historical asset was retained."
        elif action != "inspect":
            raise ProjectError(f"unknown identity-anchor action: {action}")

        active = store.active_identity_anchor(manifest, subject, include_disabled=True)
        accepted = [
            {
                "card": card["artifact_number"],
                "card_id": card["id"],
                "preview_frames": card.get("actual_new_frame_count"),
                "max_frame_index": max(0, int(card.get("actual_new_frame_count") or 1) - 1),
                "preview_available": bool(card.get("preview", {}).get("asset_path")),
            }
            for card in manifest["cards"] if card["status"] == "ACCEPTED"
        ]
        state = {
            "message": message,
            "project": project_name,
            "subject_id": subject,
            "active_identity_anchor": active,
            "active_source": ({
                "card": active.get("source_card_artifact_number"),
                "card_id": active.get("source_card_id"),
                "preview_frame_index": active.get("source_preview_frame_index"),
                "label": active.get("label"),
                "identity_scope": active.get("identity_scope", "face_only"),
                "custom_identity_instruction": active.get("custom_identity_instruction"),
                "enabled": active.get("enabled", True),
            } if active else None),
            "accepted_sources": accepted,
        }
        path = store.absolute_path(active["asset_path"]) if active else None
        if preview_image is None and active is not None:
            preview_image = load_anchor_image(store, active)
        LOGGER.info(
            "LongCaster identity control action=%s subject_id=%s active_anchor_id=%s path=%s",
            action, subject, active.get("anchor_id") if active else None, path,
        )
        image_output = preview_image if preview_image is not None else ExecutionBlocker(None)
        return {
            "ui": {"longcaster_identity": [state]},
            "result": (
                json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True),
                str(path or ""),
                image_output,
            ),
        }


class LongCasterRegisterPreview:
    """Copy a VHS-rendered card preview into its project and record the stable path."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "filenames": ("VHS_FILENAMES",),
                "project_state": ("STRING", {"forceInput": True}),
            }
        }

    RETURN_TYPES = ("STRING", "VHS_FILENAMES")
    RETURN_NAMES = ("preview_path", "project_preview_files")
    FUNCTION = "register"
    CATEGORY = "MiniMax H3/LongCaster"
    OUTPUT_NODE = True
    DESCRIPTION = "Stores the latest VHS card preview under the LongCaster project for reuse by video/frame picker nodes."

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def register(self, filenames, project_state):
        try:
            state = json.loads(project_state)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectError("project_state is not valid LongCaster JSON") from exc
        card = state.get("active_card") or {}
        project_name = state.get("project")
        card_id = card.get("id")
        artifact_hash = card.get("artifact_sha256")
        if not project_name or not card_id or not artifact_hash:
            raise ProjectError("the active card has no completed draft or accepted artifact")
        if not isinstance(filenames, (tuple, list)) or len(filenames) != 2:
            raise ProjectError("filenames must come from a Video Helper Suite Video Combine node")
        output_files = filenames[1]
        if not output_files:
            raise ProjectError("Video Helper Suite did not return a preview file")
        source = Path(output_files[-1]).resolve()
        if not source.is_file():
            raise ProjectError(f"preview source does not exist: {source}")
        store = ProjectStore(_projects_root(), str(project_name))
        destination = store.absolute_path(
            f"previews/{card_id}/card_{int(card['artifact_number']):04d}_{artifact_hash[:12]}.mp4"
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            preview_hash = sha256_file(temporary)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        store.record_preview(
            card_id=card_id,
            preview_path=destination,
            preview_sha256=preview_hash,
            source_artifact_sha256=artifact_hash,
        )
        LOGGER.info("LongCaster project preview registered: card_id=%s path=%s", card_id, destination)
        preview_state = {
            "project": str(project_name),
            "card_id": str(card_id),
            "source_artifact_sha256": str(artifact_hash),
            "preview_sha256": preview_hash,
            "path": str(destination),
        }
        return {
            "ui": {"longcaster_preview": [preview_state]},
            "result": (str(destination), (True, [str(destination)])),
        }


class LongCasterDecode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "card_packet": ("MMH3_MEDIA",),
                "video_vae": ("VAE",),
                "audio_vae": ("VAE",),
                "trim_context": ("BOOLEAN", {"default": True}),
            }
        }

    RETURN_TYPES = ("IMAGE", "AUDIO", "INT")
    RETURN_NAMES = ("images", "audio", "frame_count")
    FUNCTION = "decode"
    CATEGORY = "MiniMax H3/LongCaster"
    DESCRIPTION = "Decodes a card packet for review and optionally removes its continuation prefix."

    def decode(self, card_packet, video_vae, audio_vae, trim_context):
        from comfy_extras.nodes_audio import vae_decode_audio

        if card_packet is None:
            # Defensive compatibility for a cached result produced before empty
            # project outputs were converted to ExecutionBlocker instances.
            blocker = ExecutionBlocker(None)
            return blocker, blocker, blocker

        latent, _ = primary_latent(card_packet)
        parts = latent["samples"].unbind()
        if len(parts) != 2:
            raise ValueError("LongCasterDecode expects a joint MiniMax H3 video/audio latent")
        video_latent, audio_latent = parts
        images = video_vae.decode(video_latent)
        if len(images.shape) == 5:
            images = images.reshape(-1, images.shape[-3], images.shape[-2], images.shape[-1])
        metadata = card_packet.manifest.get("extensions", {}).get("longcaster", {}).get("card", {})
        project_name = metadata.get("project_name")
        card_id = metadata.get("card_id")
        if project_name and card_id:
            try:
                store = ProjectStore(_projects_root(), project_name)
                manifest = store.load()
                card = next((item for item in manifest["cards"] if item["id"] == card_id), None)
                if card is not None and card.get("status") == "DRAFT":
                    cache_preview_anchor(store=store, card=card, images=images)
            except Exception as exc:
                # Preview must remain usable; Accept retains a VAE-decode fallback.
                LOGGER.warning("Could not cache LongCaster draft state anchor: %s", exc)
        audio = vae_decode_audio(audio_vae, {"samples": audio_latent})
        context = int(metadata.get("context_frame_count", 0)) if trim_context else 0
        if context:
            images = images[context:]
            samples_to_trim = round(context * audio["sample_rate"] / 24)
            audio = {**audio, "waveform": audio["waveform"][..., samples_to_trim:]}
        return images, audio, int(images.shape[0])


class LongCasterTimelineExport:
    """Streams the accepted project timeline to an NVENC H.264 MP4."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "video_vae": ("VAE",),
                "audio_vae": ("VAE",),
                "project_name": ("STRING", {"default": "longcaster_project"}),
                "enabled": ("BOOLEAN", {"default": False}),
                "filename_prefix": ("STRING", {"default": "video/longcaster_joined"}),
                "include_active_draft": ("BOOLEAN", {"default": False}),
                "preset": (["p1", "p2", "p3", "p4", "p5", "p6", "p7"], {"default": "p4"}),
                "cq": ("INT", {"default": 17, "min": 0, "max": 51, "step": 1}),
                "audio_bitrate": (["128k", "192k", "256k", "320k"], {"default": "192k"}),
            },
            "optional": {
                "project_state": (
                    "STRING",
                    {
                        "forceInput": True,
                        "tooltip": "Connect LongCaster Project project_state. It overrides project_name.",
                    },
                ),
            }
        }

    RETURN_TYPES = ("STRING", "INT", "INT")
    RETURN_NAMES = ("video_path", "frame_count", "card_count")
    FUNCTION = "export"
    CATEGORY = "MiniMax H3/LongCaster"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Joins accepted cards in timeline order, removes each continuation prefix, and streams "
        "an NVENC H.264 MP4 without holding the complete decoded timeline in memory."
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def export(
        self,
        video_vae,
        audio_vae,
        project_name,
        enabled,
        filename_prefix,
        include_active_draft,
        preset,
        cq,
        audio_bitrate,
        project_state=None,
    ):
        project_name = _project_from_state(project_name, project_state)
        if not enabled:
            return {"ui": {"longcaster_timeline": [{"message": "Timeline export is disabled."}]},
                    "result": ("", 0, 0)}
        path, subfolder, frame_count, card_count = export_timeline_nvenc(
            projects_root=_projects_root(),
            project_name=project_name,
            video_vae=video_vae,
            audio_vae=audio_vae,
            filename_prefix=filename_prefix,
            include_active_draft=include_active_draft,
            preset=preset,
            cq=cq,
            audio_bitrate=audio_bitrate,
        )
        preview = {
            "filename": path.name,
            "subfolder": subfolder,
            "type": "output",
            "format": "video/h264-mp4",
            "fullpath": str(path),
            "frame_rate": 24,
        }
        status = {
            "message": f"Exported {card_count} cards and {frame_count} frames.",
            "path": str(path),
            "card_count": card_count,
            "frame_count": frame_count,
        }
        return {
            "ui": {"gifs": [preview], "longcaster_timeline": [status]},
            "result": (str(path), frame_count, card_count),
        }


NODE_CLASS_MAPPINGS = {
    "LongCasterProject": LongCasterProject,
    "LongCasterIdentityAnchor": LongCasterIdentityAnchor,
    "LongCasterRegisterPreview": LongCasterRegisterPreview,
    "LongCasterDecode": LongCasterDecode,
    "LongCasterTimelineExport": LongCasterTimelineExport,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LongCasterProject": "MiniMax H3 LongCaster Project",
    "LongCasterIdentityAnchor": "MiniMax H3 LongCaster Identity Anchor",
    "LongCasterRegisterPreview": "MiniMax H3 LongCaster Project Preview",
    "LongCasterDecode": "MiniMax H3 LongCaster Decode",
    "LongCasterTimelineExport": "MiniMax H3 LongCaster Timeline Export",
}
