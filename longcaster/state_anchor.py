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
    from PIL import Image

    if int(images.shape[0]) < 1:
        raise ProjectError("card preview decoded to no video frames")
    frame_index = int(images.shape[0]) - 1
    frame = images[frame_index, ..., :3].detach().float().clamp(0, 1).cpu()
    array = (frame * 255.0).round().byte().contiguous().numpy()
    anchor_id = str(uuid.uuid4())
    relative = f"anchors/{card['id']}/{anchor_id}.png"
    destination = store.absolute_path(relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            Image.fromarray(array, mode="RGB").save(handle, format="PNG")
            handle.flush()
            os.fsync(handle.fileno())
        if destination.exists():
            raise ProjectError(f"state anchor already exists: {relative}")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    anchor = {
        "anchor_id": anchor_id,
        "source_card_id": card["id"],
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


def reinforce_prompt(prompt: str) -> tuple[str, bool, list[str]]:
    """Add one continuity sentence to known sections, or append it once."""
    if CONTINUITY_INSTRUCTION.casefold() in prompt.casefold():
        return prompt, False, []

    updated = prompt
    injected: list[str] = []
    for section in ("summary", "retention_analysis"):
        xml = re.compile(rf"(<{section}\b[^>]*>)(.*?)(</{section}\s*>)", re.IGNORECASE | re.DOTALL)
        if xml.search(updated):
            updated = xml.sub(
                lambda match: f"{match.group(1)}{match.group(2).rstrip()}\n{CONTINUITY_INSTRUCTION}\n{match.group(3)}",
                updated,
                count=1,
            )
            injected.append(section)
            continue

        header = re.compile(
            rf"(?im)^(?P<header>\s*(?:#{{1,6}}\s*|\[)?{section.replace('_', '[_ ]')}(?:\])?\s*:?\s*)$"
        )
        if header.search(updated):
            updated = header.sub(
                lambda match: f"{match.group('header')}\n{CONTINUITY_INSTRUCTION}", updated, count=1
            )
            injected.append(section)

    if not injected:
        separator = "\n\n" if updated.strip() else ""
        updated = updated.rstrip() + separator + CONTINUITY_INSTRUCTION
        injected.append("appended")
    return updated, True, injected
