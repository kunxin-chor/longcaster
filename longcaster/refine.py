from __future__ import annotations

from dataclasses import dataclass
from typing import Any


REFINE_CADENCES = {"off", "every_accepted_card", "manual"}
CONTINUATION_SOURCE_TYPES = {"accepted_master", "derivative"}


class RefineError(RuntimeError):
    pass


def validate_refine_cadence(value: str) -> str:
    cadence = str(value or "off")
    if cadence not in REFINE_CADENCES:
        raise RefineError(
            "refine_cadence must be off, every_accepted_card, or manual"
        )
    return cadence


def refine_seed(seed: int, seed_mode: str) -> int:
    mode = str(seed_mode or "inherit")
    if mode == "inherit":
        return int(seed)
    if mode == "offset":
        return (int(seed) + 1) & 0xFFFFFFFFFFFFFFFF
    raise RefineError("refine seed_mode must be inherit or offset")


def should_refine_on_accept(cadence: str, card_enabled: bool) -> bool:
    """Resolve project precedence: Off > Auto > the Manual card checkbox."""
    mode = validate_refine_cadence(cadence)
    if not isinstance(card_enabled, bool):
        raise RefineError("card refine_enabled must be boolean")
    if mode == "off":
        return False
    if mode == "every_accepted_card":
        return True
    return card_enabled


def _streams(value: Any, *, label: str) -> list[Any]:
    if value is None:
        raise RefineError(f"{label} is missing")
    try:
        import torch
    except ImportError:
        torch = None
    if torch is not None and torch.is_tensor(value):
        streams = [value]
    elif hasattr(value, "unbind"):
        streams = list(value.unbind())
    elif isinstance(value, (tuple, list)):
        streams = list(value)
    else:
        streams = [value]
    if not streams:
        raise RefineError(f"{label} has no streams")
    return streams


def _rebuild_like(template: Any, streams: list[Any]) -> Any:
    if len(streams) == 1 and hasattr(template, "shape"):
        return streams[0]
    if isinstance(template, tuple):
        return tuple(streams)
    if isinstance(template, list):
        return list(streams)
    try:
        return type(template)(tuple(streams))
    except Exception as exc:
        raise RefineError(
            f"cannot rebuild joint latent container {type(template).__name__}"
        ) from exc


def _expanded_mask(mask: Any, sample: Any) -> Any:
    try:
        import torch
    except ImportError as exc:
        raise RefineError("Torch is required to validate H3 refine masks") from exc
    if not torch.is_tensor(mask) or not torch.is_tensor(sample):
        raise RefineError("H3 refine samples and masks must be tensors")
    candidate = mask.to(device=sample.device)
    try:
        return torch.broadcast_to(candidate, sample.shape)
    except RuntimeError as exc:
        raise RefineError(
            f"refine mask shape {tuple(mask.shape)} does not broadcast to "
            f"sample shape {tuple(sample.shape)}"
        ) from exc


@dataclass(frozen=True)
class PrefixProtection:
    method: str
    video_values: int
    audio_values: int

    @property
    def total_values(self) -> int:
        return self.video_values + self.audio_values

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "video_protected_values": self.video_values,
            "audio_protected_values": self.audio_values,
            "total_protected_values": self.total_values,
        }


def prepare_refine_latent(
    accepted_latent: dict[str, Any],
    continuation_lock: dict[str, Any] | None,
) -> tuple[dict[str, Any], PrefixProtection]:
    """Attach a reconstructed joint AV mask and restore its hard-locked values.

    The accepted latent is copied at the dictionary/tensor level. The immutable
    MMH3 archive is never modified. When a continuation prefix exists, the mask
    and prefix values come from a newly reconstructed direct-MMH3 handover.
    """
    if not isinstance(accepted_latent, dict) or "samples" not in accepted_latent:
        raise RefineError("accepted MMH3 primary resource is not a latent dictionary")
    work = {key: value for key, value in accepted_latent.items() if key != "noise_mask"}
    if continuation_lock is None:
        return work, PrefixProtection("none_first_card", 0, 0)
    if not isinstance(continuation_lock, dict) or "noise_mask" not in continuation_lock:
        raise RefineError("continuation prefix reconstruction produced no noise_mask")

    accepted_streams = _streams(accepted_latent["samples"], label="accepted latent")
    lock_streams = _streams(continuation_lock.get("samples"), label="continuation latent")
    mask_streams = _streams(continuation_lock["noise_mask"], label="continuation noise_mask")
    if len(accepted_streams) != 2 or len(lock_streams) != 2 or len(mask_streams) != 2:
        raise RefineError("H3 refine requires exactly two joint video/audio streams")

    try:
        import torch
    except ImportError as exc:
        raise RefineError("Torch is required to prepare an H3 refine latent") from exc

    restored: list[Any] = []
    counts: list[int] = []
    for index, (accepted, source, mask) in enumerate(
        zip(accepted_streams, lock_streams, mask_streams)
    ):
        if not torch.is_tensor(accepted) or not torch.is_tensor(source):
            raise RefineError("H3 refine samples must be tensors")
        if tuple(accepted.shape) != tuple(source.shape):
            name = "video" if index == 0 else "audio"
            raise RefineError(
                f"reconstructed {name} latent shape {tuple(source.shape)} does not "
                f"match accepted shape {tuple(accepted.shape)}"
            )
        expanded = _expanded_mask(mask, accepted)
        protected = expanded <= 1e-8
        count = int(protected.sum().item())
        if count < 1:
            name = "video" if index == 0 else "audio"
            raise RefineError(f"reconstructed {name} continuation mask has no hard-protected values")
        source_on_target = source.to(device=accepted.device, dtype=accepted.dtype)
        restored.append(torch.where(protected, source_on_target, accepted))
        counts.append(count)

    work["samples"] = _rebuild_like(accepted_latent["samples"], restored)
    work["noise_mask"] = continuation_lock["noise_mask"]
    return work, PrefixProtection(
        "reconstructed_direct_mmh3_joint_av_mask", counts[0], counts[1]
    )


def assert_protected_prefix_unchanged(
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    """Fail closed if a sampler changed any hard-protected joint AV value."""
    if "noise_mask" not in before:
        return
    before_streams = _streams(before.get("samples"), label="refine input")
    after_streams = _streams(after.get("samples"), label="refine output")
    mask_streams = _streams(before.get("noise_mask"), label="refine noise_mask")
    if not (len(before_streams) == len(after_streams) == len(mask_streams) == 2):
        raise RefineError("refine prefix verification requires joint video/audio streams")

    try:
        import torch
    except ImportError as exc:
        raise RefineError("Torch is required to verify an H3 refine result") from exc
    for index, (source, result, mask) in enumerate(
        zip(before_streams, after_streams, mask_streams)
    ):
        if tuple(source.shape) != tuple(result.shape):
            raise RefineError("refine changed the joint latent stream shape")
        protected = _expanded_mask(mask, source) <= 1e-8
        source_values = source[protected]
        result_values = result.to(device=source.device, dtype=source.dtype)[protected]
        if not torch.equal(source_values, result_values):
            name = "video" if index == 0 else "audio"
            maximum = float((source_values - result_values).abs().max().item())
            raise RefineError(
                f"refine changed the protected {name} continuation prefix "
                f"(maximum absolute difference {maximum:g})"
            )
