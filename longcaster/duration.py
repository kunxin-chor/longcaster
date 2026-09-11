from __future__ import annotations

from dataclasses import dataclass
import math


H3_FRAME_BASE = 5
H3_FRAME_STRIDE = 17
H3_FPS = 24
H3_CONTINUATION_CONTEXT_FRAMES = 39


@dataclass(frozen=True)
class DurationPlan:
    requested_seconds: float
    context_frames: int
    target_total_frames: int
    generated_frames: int
    actual_new_frames: int
    actual_new_seconds: float


def _nearest_valid_frame_count(target: float, *, minimum: int) -> int:
    """Return the nearest N satisfying N = 17k + 5 and N >= minimum."""
    lowest_k = max(0, math.ceil((minimum - H3_FRAME_BASE) / H3_FRAME_STRIDE))
    position = (target - H3_FRAME_BASE) / H3_FRAME_STRIDE
    lower_k = max(lowest_k, math.floor(position))
    upper_k = max(lowest_k, math.ceil(position))
    candidates = {H3_FRAME_BASE + H3_FRAME_STRIDE * lower_k,
                  H3_FRAME_BASE + H3_FRAME_STRIDE * upper_k}
    # Prefer the longer result on an exact tie so a card does not undershoot.
    return min(candidates, key=lambda value: (abs(value - target), -value))


def resolve_duration(
    requested_seconds: float,
    *,
    context_frames: int = 0,
    fps: int = H3_FPS,
) -> DurationPlan:
    if not math.isfinite(requested_seconds) or requested_seconds <= 0:
        raise ValueError("duration_seconds must be a finite positive number")
    if context_frames < 0:
        raise ValueError("context_frames cannot be negative")
    if fps <= 0:
        raise ValueError("fps must be positive")

    requested_new_frames = requested_seconds * fps
    total_target = requested_new_frames + context_frames
    generated = _nearest_valid_frame_count(
        total_target,
        minimum=max(H3_FRAME_BASE, context_frames + 1),
    )
    actual_new_frames = generated - context_frames
    return DurationPlan(
        requested_seconds=float(requested_seconds),
        context_frames=int(context_frames),
        target_total_frames=int(round(total_target)),
        generated_frames=generated,
        actual_new_frames=actual_new_frames,
        actual_new_seconds=actual_new_frames / fps,
    )
