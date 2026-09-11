from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import uuid
from typing import Any

from .project import ProjectError, ProjectStore, sha256_file, utc_now


LOGGER = logging.getLogger("longcaster.state_anchor")
FPS = 24.0
ANCHOR_ROLE = "current_state"
NATIVE_MECHANISM = "MiniMaxH3AddGuide/minimax_keyframes"
IDENTITY_ROLE = "identity"
IDENTITY_NATIVE_MECHANISM = "MiniMaxH3ReferenceToVideo/minimax_refs"
IDENTITY_SCOPES = ("face_only", "face_clothing", "face_body", "everything", "custom")
CONTINUITY_INSTRUCTION = (
    "Continue <Subject 1> from the current visual state shown in the continuation anchor. "
    "This state is authoritative where it conflicts with earlier reference appearance."
)


def current_state_anchor(card: dict[str, Any]) -> dict[str, Any] | None:
    anchors = card.get("anchors") or []
    matches = [
        item for item in anchors
        if item.get("role") == ANCHOR_ROLE and bool(item.get("enabled", True))
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: str(item.get("created_at", "")))[-1]


def continuation_anchor_frame(context_frame_count: int) -> int:
    if int(context_frame_count) < 1:
        raise ValueError("a continuation state anchor requires at least one handover frame")
    return int(context_frame_count) - 1


def identity_source_frame(card: dict[str, Any], preview_frame_index: int) -> int:
    """Translate a visible-preview frame to the physical accepted-MMH3 frame."""
    preview_index = int(preview_frame_index)
    preview_count = int(card.get("actual_new_frame_count") or 0)
    if preview_index < 0 or preview_index >= preview_count:
        raise ProjectError(
            f"identity frame_index must be between 0 and {max(0, preview_count - 1)} "
            f"for accepted card {card.get('artifact_number', '?')}"
        )
    return int(card.get("context_frame_count") or 0) + preview_index


def _decoded_images(packet: Any, video_vae: Any) -> Any:
    from .mmh3_adapter import primary_latent

    latent, _ = primary_latent(packet)
    parts = latent["samples"].unbind()
    if len(parts) != 2:
        raise ProjectError("state anchor extraction expects a joint H3 video/audio latent")
    images = video_vae.decode(parts[0])
    if len(images.shape) == 5:
        images = images.reshape(-1, images.shape[-3], images.shape[-2], images.shape[-1])
    if int(images.shape[0]) < 1:
        raise ProjectError("accepted card decoded to no video frames")
    return images


def extract_last_frame_anchor(
    *,
    store: ProjectStore,
    card: dict[str, Any],
    packet: Any,
    video_vae: Any,
) -> dict[str, Any]:
    """Decode an accepted candidate and atomically persist its final frame as PNG."""
    images = _decoded_images(packet, video_vae)
    return persist_last_frame_anchor(store=store, card=card, images=images)


def persist_last_frame_anchor(
    *, store: ProjectStore, card: dict[str, Any], images: Any
) -> dict[str, Any]:
    """Persist the final frame from an existing decode without invoking the VAE."""
    if int(images.shape[0]) < 1:
        raise ProjectError("card preview decoded to no video frames")
    frame_index = int(images.shape[0]) - 1
    anchor_id = str(uuid.uuid4())
    relative, destination = _persist_frame_png(
        store=store, card_id=card["id"], anchor_id=anchor_id, frame=images[frame_index]
    )

    anchor = {
        "anchor_id": anchor_id,
        "source_card_id": card["id"],
        "source_card_artifact_number": int(card["artifact_number"]),
        "source_frame_index": frame_index,
        "source_timestamp_seconds": frame_index / FPS,
        "role": ANCHOR_ROLE,
        "asset_path": relative,
        "asset_sha256": sha256_file(destination),
        "media_type": "image/png",
        "created_at": utc_now(),
        "enabled": True,
        "mode": NATIVE_MECHANISM,
    }
    LOGGER.info(
        "LongCaster state anchor extracted: source_card_id=%s frame_index=%d "
        "timestamp=%.3fs asset=%s mechanism=%s",
        card["id"], frame_index, anchor["source_timestamp_seconds"], destination, NATIVE_MECHANISM,
    )
    return anchor


def _persist_frame_png(
    *, store: ProjectStore, card_id: str, anchor_id: str, frame: Any
) -> tuple[str, Path]:
    from PIL import Image

    pixels = frame[..., :3].detach().float().clamp(0, 1).cpu()
    array = (pixels * 255.0).round().byte().contiguous().numpy()
    relative = f"anchors/{card_id}/{anchor_id}.png"
    destination = store.absolute_path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            Image.fromarray(array, mode="RGB").save(handle, format="PNG")
            handle.flush()
            os.fsync(handle.fileno())
        if destination.exists():
            raise ProjectError(f"anchor already exists: {relative}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return relative, destination


def extract_identity_frame_anchor(
    *, store: ProjectStore, card: dict[str, Any], packet: Any, video_vae: Any,
    preview_frame_index: int, subject_id: str, label: str = "",
    identity_scope: str = "face_only", custom_identity_instruction: str = "",
) -> dict[str, Any]:
    """Extract a preview-relative frame from an immutable accepted MMH3 card."""
    if card.get("status") != "ACCEPTED" or not card.get("master_path"):
        raise ProjectError("identity anchors require an accepted MMH3 source card")
    subject = str(subject_id).strip()
    if not subject:
        raise ProjectError("identity anchor subject_id cannot be empty")
    scope, custom_instruction = validate_identity_scope(
        identity_scope, custom_identity_instruction
    )
    image, preview_index, physical_index = decode_identity_frame(
        card=card,
        packet=packet,
        video_vae=video_vae,
        preview_frame_index=preview_frame_index,
    )
    anchor_id = str(uuid.uuid4())
    relative, destination = _persist_frame_png(
        store=store, card_id=card["id"], anchor_id=anchor_id, frame=image[0]
    )
    anchor = {
        "anchor_id": anchor_id,
        "source_card_id": card["id"],
        "source_card_artifact_number": int(card["artifact_number"]),
        "source_preview_frame_index": preview_index,
        "source_frame_index": physical_index,
        "source_preview_timestamp_seconds": preview_index / FPS,
        "source_timestamp_seconds": physical_index / FPS,
        "source_timestamp_sec": physical_index / FPS,
        "role": IDENTITY_ROLE,
        "subject_id": subject,
        "label": str(label).strip() or None,
        "asset_path": relative,
        "asset_sha256": sha256_file(destination),
        "media_type": "image/png",
        "created_at": utc_now(),
        "enabled": True,
        "mode": IDENTITY_NATIVE_MECHANISM,
        "strength": None,
        "identity_scope": scope,
        "custom_identity_instruction": custom_instruction,
    }
    LOGGER.info(
        "LongCaster identity anchor extracted: anchor_id=%s source_card_id=%s "
        "preview_frame_index=%d source_frame_index=%d timestamp=%.3fs asset=%s "
        "subject_id=%s mechanism=%s",
        anchor_id, card["id"], preview_index, physical_index,
        anchor["source_timestamp_seconds"], destination, subject, IDENTITY_NATIVE_MECHANISM,
    )
    return anchor


def persist_identity_image_anchor(
    *, store: ProjectStore, card: dict[str, Any], image: Any,
    preview_frame_index: int, subject_id: str, label: str = "",
    identity_scope: str = "face_only", custom_identity_instruction: str = "",
) -> dict[str, Any]:
    """Persist one externally selected image with accepted-card provenance."""
    if card.get("status") != "ACCEPTED" or not card.get("master_path"):
        raise ProjectError("identity anchors require an accepted MMH3 source card")
    subject = str(subject_id).strip()
    if not subject:
        raise ProjectError("identity anchor subject_id cannot be empty")
    scope, custom_instruction = validate_identity_scope(
        identity_scope, custom_identity_instruction
    )
    if not hasattr(image, "shape") or len(image.shape) != 4 or int(image.shape[0]) != 1:
        raise ProjectError("selected_image must contain exactly one IMAGE frame")
    preview_index = int(preview_frame_index)
    physical_index = identity_source_frame(card, preview_index)
    anchor_id = str(uuid.uuid4())
    relative, destination = _persist_frame_png(
        store=store, card_id=card["id"], anchor_id=anchor_id, frame=image[0]
    )
    anchor = {
        "anchor_id": anchor_id,
        "source_card_id": card["id"],
        "source_card_artifact_number": int(card["artifact_number"]),
        "source_preview_frame_index": preview_index,
        "source_frame_index": physical_index,
        "source_preview_timestamp_seconds": preview_index / FPS,
        "source_timestamp_seconds": physical_index / FPS,
        "source_timestamp_sec": physical_index / FPS,
        "role": IDENTITY_ROLE,
        "subject_id": subject,
        "label": str(label).strip() or None,
        "asset_path": relative,
        "asset_sha256": sha256_file(destination),
        "media_type": "image/png",
        "created_at": utc_now(),
        "enabled": True,
        "mode": IDENTITY_NATIVE_MECHANISM,
        "strength": None,
        "identity_scope": scope,
        "custom_identity_instruction": custom_instruction,
        "selection_source": "external_image",
    }
    LOGGER.info(
        "LongCaster external identity image persisted: anchor_id=%s source_card_id=%s "
        "preview_frame_index=%d source_frame_index=%d asset=%s subject_id=%s",
        anchor_id, card["id"], preview_index, physical_index, destination, subject,
    )
    return anchor


def decode_identity_frame(
    *, card: dict[str, Any], packet: Any, video_vae: Any, preview_frame_index: int,
) -> tuple[Any, int, int]:
    """Decode one exact, preview-relative identity frame without persisting it."""
    if card.get("status") != "ACCEPTED" or not card.get("master_path"):
        raise ProjectError("identity previews require an accepted MMH3 source card")
    preview_index = int(preview_frame_index)
    physical_index = identity_source_frame(card, preview_index)
    images = _decoded_images(packet, video_vae)
    if physical_index >= int(images.shape[0]):
        raise ProjectError(
            f"identity frame resolves to MMH3 frame {physical_index}, but only {int(images.shape[0])} decoded"
        )
    return images[physical_index:physical_index + 1], preview_index, physical_index


def decode_identity_preview(*, card: dict[str, Any], packet: Any, video_vae: Any) -> Any:
    """Decode only the visible portion of an accepted card for picker playback."""
    if card.get("status") != "ACCEPTED" or not card.get("master_path"):
        raise ProjectError("identity previews require an accepted MMH3 source card")
    images = _decoded_images(packet, video_vae)
    context = int(card.get("context_frame_count") or 0)
    count = int(card.get("actual_new_frame_count") or 0)
    end = context + count
    if count < 1 or end > int(images.shape[0]):
        raise ProjectError("accepted card has invalid visible preview frame metadata")
    return images[context:end]


def _candidate_path(store: ProjectStore, card: dict[str, Any]) -> Path:
    artifact_hash = card.get("artifact_sha256")
    if not isinstance(artifact_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_hash):
        raise ProjectError("draft has no valid artifact hash for anchor caching")
    return store.absolute_path(f"anchor_candidates/{card['id']}/{artifact_hash}.json")


def cached_last_frame_anchor(store: ProjectStore, card: dict[str, Any]) -> dict[str, Any] | None:
    """Return the anchor cached by draft preview for this exact draft artifact."""
    path = _candidate_path(store, card)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring invalid state-anchor candidate %s: %s", path, exc)
        return None
    if payload.get("artifact_sha256") != card.get("artifact_sha256"):
        return None
    anchor = payload.get("anchor")
    if not isinstance(anchor, dict) or anchor.get("source_card_id") != card.get("id"):
        return None
    asset_path = anchor.get("asset_path")
    if not isinstance(asset_path, str) or not asset_path:
        return None
    asset = store.absolute_path(asset_path)
    if not asset.is_file() or sha256_file(asset) != anchor.get("asset_sha256"):
        return None
    return anchor


def cache_preview_anchor(
    *, store: ProjectStore, card: dict[str, Any], images: Any
) -> dict[str, Any]:
    """Cache a preview's final frame so Accept does not decode the draft again."""
    existing = cached_last_frame_anchor(store, card)
    if existing is not None:
        return existing
    anchor = persist_last_frame_anchor(store=store, card=card, images=images)
    path = _candidate_path(store, card)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = {"artifact_sha256": card["artifact_sha256"], "anchor": anchor}
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    LOGGER.info(
        "LongCaster state anchor cached from draft preview: source_card_id=%s asset=%s",
        card["id"], store.absolute_path(anchor["asset_path"]),
    )
    return anchor


def load_anchor_image(store: ProjectStore, anchor: dict[str, Any]) -> Any:
    import numpy as np
    import torch
    from PIL import Image

    path = store.absolute_path(anchor["asset_path"])
    if not path.is_file():
        raise ProjectError(f"state anchor is missing: {anchor['asset_path']}")
    if sha256_file(path) != anchor.get("asset_sha256"):
        raise ProjectError(f"state anchor hash mismatch: {anchor['asset_path']}")
    with Image.open(path) as source:
        array = np.asarray(source.convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(array.copy()).unsqueeze(0)


def apply_native_anchor(
    *, positive: Any, latent: dict[str, Any], video_vae: Any, image: Any, frame_index: int
) -> Any:
    from comfy_extras.nodes_minimax_h3 import MiniMaxH3AddGuide

    output = MiniMaxH3AddGuide.execute(
        positive,
        latent,
        int(frame_index),
        vae=video_vae,
        image=image,
    )
    return output[0]


def validate_identity_scope(scope: str, custom_instruction: str = "") -> tuple[str, str | None]:
    normalized = str(scope or "face_only").strip().lower()
    if normalized not in IDENTITY_SCOPES:
        raise ProjectError(
            f"identity_scope must be one of: {', '.join(IDENTITY_SCOPES)}"
        )
    custom = str(custom_instruction or "").strip()
    if normalized == "custom" and not custom:
        raise ProjectError("custom identity scope requires a custom identity instruction")
    return normalized, custom if normalized == "custom" else None


def identity_instruction(
    subject_id: str,
    picture_index: int,
    identity_scope: str = "face_only",
    custom_identity_instruction: str | None = None,
) -> str:
    scope, custom = validate_identity_scope(identity_scope, custom_identity_instruction or "")
    picture = f"<Picture {int(picture_index)}>"
    if scope == "custom":
        return f"For {subject_id}, interpret identity anchor {picture} as follows: {custom}"
    if scope == "face_only":
        return (
            f"Use identity anchor {picture} only for {subject_id}'s facial identity and facial proportions. "
            "Do not copy its pose, expression, hairstyle or hair condition, body, clothing, logos, "
            "accessories, lighting, or background."
        )
    if scope == "face_clothing":
        return (
            f"Use identity anchor {picture} only for {subject_id}'s facial identity and clothing design. "
            "Do not copy its pose, expression, hairstyle or hair condition, body pose, lighting, or background."
        )
    if scope == "face_body":
        return (
            f"Use identity anchor {picture} for {subject_id}'s facial identity, body proportions, and visible "
            "body details such as tattoos, scars, and wounds. Do not copy its pose, expression, hairstyle or "
            "hair condition, clothing, logos, accessories, lighting, or background."
        )
    return (
        f"Use identity anchor {picture} for {subject_id}'s full visible appearance, including face, hair, body, "
        "clothing, and accessories. Do not copy its pose, camera composition, lighting, or background."
    )


def reinforce_prompt(
    prompt: str,
    *,
    current_state_active: bool = True,
    identity_subject_id: str | None = None,
    identity_picture_index: int | None = None,
    identity_scope: str = "face_only",
    custom_identity_instruction: str | None = None,
) -> tuple[str, bool, list[str]]:
    """Add concise active-anchor semantics without rewriting the user prompt."""
    state_instruction = CONTINUITY_INSTRUCTION if current_state_active else None
    identity_text = None
    if identity_subject_id is not None and identity_picture_index is not None:
        identity_text = identity_instruction(
            identity_subject_id,
            identity_picture_index,
            identity_scope,
            custom_identity_instruction,
        )

    candidates = [item for item in (state_instruction, identity_text) if item]
    candidates = [item for item in candidates if item.casefold() not in prompt.casefold()]
    if not candidates:
        return prompt, False, []

    updated = prompt
    injected: list[str] = []
    inserted: set[str] = set()
    section_instructions = {
        "summary": [item for item in (state_instruction,) if item in candidates],
        # Identity scope is deliberately first and appears only in retention_analysis.
        "retention_analysis": [item for item in (identity_text, state_instruction) if item in candidates],
    }
    for section, instructions in section_instructions.items():
        if not instructions:
            continue
        addition = "\n".join(instructions)
        xml = re.compile(rf"(<{section}\b[^>]*>)(.*?)(</{section}\s*>)", re.IGNORECASE | re.DOTALL)
        if xml.search(updated):
            updated = xml.sub(
                lambda match: f"{match.group(1)}\n{addition}\n{match.group(2).lstrip()}{match.group(3)}",
                updated,
                count=1,
            )
            injected.append(section)
            inserted.update(instructions)
            continue

        header = re.compile(
            rf"(?im)^(?P<header>\s*(?:#{{1,6}}\s*|\[)?{section.replace('_', '[_ ]')}(?:\])?\s*:?\s*)$"
        )
        if header.search(updated):
            updated = header.sub(
                lambda match: f"{match.group('header')}\n{addition}", updated, count=1
            )
            injected.append(section)
            inserted.update(instructions)

    missing = [item for item in candidates if item not in inserted]
    if missing:
        separator = "\n\n" if updated.strip() else ""
        updated = updated.rstrip() + separator + "\n".join(missing)
        injected.append("appended")
    return updated, True, injected
