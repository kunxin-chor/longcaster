from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


HYPERFLOW_COMPENSATION_VERSION = "longcaster_hyperflow_prompt_offset_v1"
_HYPERFLOW_NODE_TYPES = {"applyhyperflow", "applyhyperflowadvanced"}
_CARD_CLOCK_RE = re.compile(
    r"(?<![\d:])00:(?P<seconds>[0-5]\d)(?P<fraction>\.\d{1,3})?(?![\d:])"
)


def detect_hyperflow(model: Any, provenance: Any = None) -> dict[str, Any]:
    """Detect the ComfyUI HyperFlow patch without importing its custom node."""
    sources: list[str] = []
    object_patches = getattr(model, "object_patches", None)
    if isinstance(object_patches, Mapping):
        forward = object_patches.get("diffusion_model.time_embedder.forward")
        if getattr(forward, "_hyperflow_two_time", False):
            sources.append("model_two_time_patch")

    if isinstance(provenance, Mapping):
        model_nodes = set((provenance.get("branches") or {}).get("model") or [])
        for node in provenance.get("nodes") or []:
            if not isinstance(node, Mapping) or node.get("node_id") not in model_nodes:
                continue
            if str(node.get("class_type", "")).casefold() in _HYPERFLOW_NODE_TYPES:
                sources.append("upstream_apply_node")
                break

    return {"detected": bool(sources), "sources": sources}


def _shift_card_timestamps(prompt: str, offset_milliseconds: int) -> tuple[str, int]:
    shifted = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal shifted
        fraction = (match.group("fraction") or "")[1:]
        milliseconds = int(fraction.ljust(3, "0") or "0")
        total = int(match.group("seconds")) * 1000 + milliseconds + offset_milliseconds
        minutes, remainder = divmod(total, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        shifted += 1
        return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    return _CARD_CLOCK_RE.sub(replace, prompt), shifted


def compensate_hyperflow_continuation_prompt(
    prompt: str,
    *,
    model: Any,
    provenance: Any,
    continuation_strategy: str,
    context_frames: int,
    fps: int = 24,
) -> tuple[str, dict[str, Any]]:
    """Move visible-card prompt timing past HyperFlow's protected prefix."""
    detection = detect_hyperflow(model, provenance)
    report: dict[str, Any] = {
        "version": HYPERFLOW_COMPENSATION_VERSION,
        **detection,
        "applied": False,
        "reason": "hyperflow_not_detected",
        "context_frames": int(context_frames),
        "fps": int(fps),
        "offset_seconds": 0.0,
        "explicit_timestamps_shifted": 0,
    }
    if not detection["detected"]:
        return prompt, report
    if continuation_strategy != "direct_mmh3" or int(context_frames) <= 0:
        report["reason"] = "not_a_direct_continuation"
        return prompt, report
    if int(fps) <= 0:
        raise ValueError("HyperFlow continuation compensation requires a positive fps")

    offset_milliseconds = round(int(context_frames) * 1000 / int(fps))
    shifted_prompt, shifted_count = _shift_card_timestamps(prompt, offset_milliseconds)
    boundary = f"00:{offset_milliseconds // 1000:02d}.{offset_milliseconds % 1000:03d}"
    instruction = (
        f"The interval from 00:00.000 through {boundary} is inherited, protected "
        "continuation context from the previous card. Treat it only as context. "
        f"Begin every newly requested action, line of dialogue, sound cue, and camera "
        f"change at or after the continuation boundary at {boundary}. Explicit "
        "timestamps below are generation-time timestamps and already include this offset."
    )
    compensated = f"{instruction}\n\n{shifted_prompt}" if shifted_prompt else instruction
    report.update({
        "applied": True,
        "reason": "protected_prefix_timing_compensated",
        "offset_seconds": offset_milliseconds / 1000.0,
        "explicit_timestamps_shifted": shifted_count,
        "instruction": instruction,
    })
    return compensated, report


__all__ = [
    "HYPERFLOW_COMPENSATION_VERSION",
    "compensate_hyperflow_continuation_prompt",
    "detect_hyperflow",
]
