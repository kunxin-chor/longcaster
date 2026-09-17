"""Schedule-matched treatment for a disposable MiniMax H3 AV prefix.

The saved continuation source is never modified. Comfy's inpaint sampler and
H3's own timestep conditioning receive the same dynamic video mask while the
audio portion of the joint mask remains unchanged.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable

import torch


LOGGER = logging.getLogger("longcaster.drift_control")

DRIFT_CONTROL_RECIPE = {
    "version": "longcaster_h3_drift_control_av_v1",
    "context_frames": 39,
    "video_steps": 12,
    "matched_steps": 8,
    "taper_steps": 4,
    "sigma_rule": "next_strictly_lower_sigma_over_current_sigma",
    "mask_quantization": 256,
    "audio": "unchanged_hard_continuation",
}

_WRAPPER_KEY = "longcaster_h3_drift_control_av"
_MODEL_OPTION_MARKER = "longcaster_h3_drift_control_recipe"


def schedule_values(sigmas: Any) -> tuple[float, ...]:
    """Return finite non-negative schedule levels, deduplicated descending."""
    if torch.is_tensor(sigmas):
        values: Iterable[Any] = sigmas.detach().float().reshape(-1).cpu()
    else:
        values = sigmas or ()
    normalized = []
    for value in values:
        number = float(value)
        if math.isfinite(number) and number >= 0.0:
            normalized.append(number)
    return tuple(sorted(set(normalized), reverse=True))


def next_schedule_sigma(current_sigma: float, sigmas: Any) -> float:
    current = float(current_sigma)
    if not math.isfinite(current) or current <= 0.0:
        return 0.0
    tolerance = max(1e-7, abs(current) * 1e-6)
    return next(
        (candidate for candidate in schedule_values(sigmas) if candidate < current - tolerance),
        0.0,
    )


def matched_noise_ratio(current_sigma: float, sigmas: Any) -> float:
    current = float(current_sigma)
    if not math.isfinite(current) or current <= 0.0:
        return 0.0
    return max(0.0, min(1.0, next_schedule_sigma(current, sigmas) / current))


def temporal_prefix_weights(
    prefix_steps: int = DRIFT_CONTROL_RECIPE["video_steps"],
    taper_steps: int = DRIFT_CONTROL_RECIPE["taper_steps"],
) -> tuple[float, ...]:
    count, taper = int(prefix_steps), int(taper_steps)
    if count < 1 or taper < 1 or taper > count:
        raise ValueError("drift-control prefix/taper step counts are invalid")
    weights = [1.0] * (count - taper)
    weights.extend(float(taper - offset - 1) / float(taper) for offset in range(taper))
    return tuple(weights)


def apply_dynamic_prefix_mask(
    packed_mask: torch.Tensor,
    video_shape: tuple[int, ...],
    prefix_steps: int,
    ratio: float,
) -> tuple[torch.Tensor, torch.Tensor]:
    """Create synchronized packed-sampler and one-channel H3 video masks."""
    if not torch.is_tensor(packed_mask) or packed_mask.ndim != 3:
        raise ValueError("drift-control sampler mask must be packed as [B,1,N]")
    shape = tuple(int(value) for value in video_shape)
    if len(shape) != 5 or shape[0] != int(packed_mask.shape[0]):
        raise ValueError(f"drift-control expected H3 video [B,C,T,H,W], got {shape}")
    steps = int(prefix_steps)
    if steps != int(DRIFT_CONTROL_RECIPE["video_steps"]) or steps > shape[2]:
        raise ValueError("drift-control requires the 12-step H3 video prefix")
    video_elements = math.prod(shape[1:])
    if int(packed_mask.shape[-1]) < video_elements:
        raise ValueError("drift-control packed mask is shorter than its video stream")
    output = packed_mask.clone()
    video = output[..., :video_elements].reshape(shape)
    weights = torch.tensor(
        temporal_prefix_weights(steps), device=video.device, dtype=video.dtype,
    ).mul_(float(max(0.0, min(1.0, ratio))))
    video[:, :, :steps] = weights.view(1, 1, steps, 1, 1)
    quantization = float(DRIFT_CONTROL_RECIPE["mask_quantization"])
    h3_video_mask = torch.ceil(video[:, :1].float() * quantization) / quantization
    return output, h3_video_mask


class DriftControlMaskState:
    """Runtime state that can be rebound to each future sampling stage."""

    def __init__(self, video_shape: tuple[int, ...], prefix_steps: int, sigmas: Any):
        self.video_shape = tuple(int(value) for value in video_shape)
        self.prefix_steps = int(prefix_steps)
        if self.prefix_steps != (
            int(DRIFT_CONTROL_RECIPE["matched_steps"])
            + int(DRIFT_CONTROL_RECIPE["taper_steps"])
        ):
            raise ValueError("drift-control requires 8 matched + 4 taper video steps")
        self.schedule = schedule_values(sigmas)
        if len(self.schedule) < 2 or self.schedule[-1] != 0.0:
            raise ValueError("drift-control requires the active sigma schedule ending at zero")
        self.current_video_mask: torch.Tensor | None = None
        self.last_sigma: float | None = None
        self.last_ratio: float | None = None

    def denoise_mask_function(self, sigma, denoise_mask, extra_options=None):
        current = float(torch.as_tensor(sigma).detach().float().reshape(-1)[0])
        ratio = matched_noise_ratio(current, self.schedule)
        output, video_mask = apply_dynamic_prefix_mask(
            denoise_mask, self.video_shape, self.prefix_steps, ratio,
        )
        self.current_video_mask = video_mask
        self.last_sigma = current
        self.last_ratio = ratio
        return output

    def apply_model_wrapper(self, executor, *args, **kwargs):
        if self.current_video_mask is not None:
            kwargs["denoise_mask"] = self.current_video_mask
        return executor(*args, **kwargs)


def _joint_video_shape(latent: dict[str, Any]) -> tuple[int, ...]:
    samples = latent.get("samples") if isinstance(latent, dict) else None
    if hasattr(samples, "unbind"):
        streams = list(samples.unbind())
    elif hasattr(samples, "tensors"):
        streams = list(samples.tensors)
    elif isinstance(samples, (tuple, list)):
        streams = list(samples)
    else:
        streams = []
    if len(streams) != 2 or not all(torch.is_tensor(item) for item in streams):
        raise ValueError("drift-control requires a valid two-stream H3 AV latent")
    video, audio = streams
    if video.ndim != 5 or audio.ndim != 4 or int(video.shape[0]) != 1 or int(audio.shape[0]) != 1:
        raise ValueError("drift-control received inconsistent H3 video/audio prefix shapes")
    return tuple(int(value) for value in video.shape)


def install_drift_control_model(model: Any, latent: dict[str, Any], sigmas: Any) -> Any:
    """Clone MODEL and install coupled sampler/H3 dynamic-mask hooks."""
    if model is None or not callable(getattr(model, "clone", None)):
        raise ValueError("Drift-Control AV requires a ComfyUI MiniMax H3 MODEL")
    inner = getattr(model, "model", None)
    model_type = str(getattr(getattr(inner, "model_type", None), "name", ""))
    if model_type != "FLOW_AV" and getattr(getattr(inner, "__class__", None), "__name__", "") != "MiniMaxH3":
        raise ValueError("Drift-Control AV requires a MiniMax H3 AV MODEL")
    video_shape = _joint_video_shape(latent)
    patched = model.clone()
    options = getattr(patched, "model_options", None)
    if not isinstance(options, dict):
        raise ValueError("Drift-Control AV MODEL clone has no model_options")
    if options.get("denoise_mask_function") is not None:
        raise ValueError(
            "Drift-Control AV cannot combine with another dynamic denoise-mask patch"
        )
    if not callable(getattr(patched, "set_model_denoise_mask_function", None)):
        raise RuntimeError("Drift-Control AV requires dynamic denoise-mask support")
    if not callable(getattr(patched, "add_wrapper_with_key", None)):
        raise RuntimeError("Drift-Control AV requires ComfyUI apply-model wrappers")
    # Some lightweight/custom patchers shallow-copy model_options in clone().
    # Detach it before installing hooks so the caller's MODEL is never mutated.
    if options is getattr(model, "model_options", None):
        patched.model_options = dict(options)
        options = patched.model_options
    from comfy.patcher_extension import WrappersMP

    state = DriftControlMaskState(
        video_shape, int(DRIFT_CONTROL_RECIPE["video_steps"]), sigmas,
    )
    patched.set_model_denoise_mask_function(state.denoise_mask_function)
    patched.add_wrapper_with_key(
        WrappersMP.APPLY_MODEL, _WRAPPER_KEY, state.apply_model_wrapper,
    )
    patched.model_options[_MODEL_OPTION_MARKER] = dict(DRIFT_CONTROL_RECIPE)
    patched.model_options[_WRAPPER_KEY] = state
    LOGGER.info(
        "LongCaster Drift-Control AV installed: 12 video steps (8 matched + 4 taper), "
        "%d exact sigma levels, audio unchanged", len(state.schedule),
    )
    return patched


__all__ = [
    "DRIFT_CONTROL_RECIPE", "DriftControlMaskState", "apply_dynamic_prefix_mask",
    "install_drift_control_model", "matched_noise_ratio", "next_schedule_sigma",
    "schedule_values", "temporal_prefix_weights",
]
