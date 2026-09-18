"""Low-frequency scene-baseline correction for disposable H3 video prefixes."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from typing import Any

import torch
import torch.nn.functional as functional


COLOR_STABLE_RECIPE = {
    "version": "longcaster_h3_latent_color_delta_v1",
    "context_frames": 39,
    "video_steps": 12,
    "anchor": "selected_earlier_accepted_card",
    "source": "resolved_continuation_source_tail",
    "strength": 0.50,
    "minimum_luma_shift_code_values": 1.0,
    "minimum_saturation_ratio_change": 0.02,
    "neutral_brightness_threshold": 0.5 / 255.0,
    "neutral_saturation_threshold": 0.005,
    "max_luma_shift_code_values": 6.0,
    "max_saturation_change": 0.06,
    "spatial_lowpass_kernel": 3,
    "temporal_taper": "full_prefix_smoothstep_zero_to_one",
    "delta_rule": "E(graded_D(z))-E(D(z))",
    "audio": "unchanged",
}

_STATS_VERSION = "longcaster_h3_color_stats_v1"
_STATS_CACHE: dict[tuple[str, int, int, str, str], dict[str, Any]] = {}
_STATS_CACHE_LIMIT = 16


def vae_identity(vae: Any) -> dict[str, Any]:
    from .card_reference import vae_identity as process_vae_identity

    identity = {
        "module": type(vae).__module__,
        "class": type(vae).__name__,
        "process_identity": process_vae_identity(vae),
    }
    for name in (
        "downscale_ratio", "upscale_ratio", "latent_channels",
        "spacial_compression_encode", "temporal_compression_encode",
    ):
        value = getattr(vae, name, None)
        if value is None or isinstance(value, (str, int, float, bool)):
            identity[name] = value
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    identity["fingerprint"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return identity


def tensor_scene_color_stats(frames: Any) -> dict[str, Any]:
    if not torch.is_tensor(frames) or frames.ndim != 4 or int(frames.shape[-1]) < 3:
        raise ValueError("Color-Stable Drift AV expected IMAGE [frames,H,W,C]")
    count, height, width = (int(frames.shape[index]) for index in range(3))
    if count < 1:
        raise ValueError("Color-Stable Drift AV received no decoded frames")
    frame_step = max(1, count // 24)
    y0, y1 = int(height * 0.12), max(int(height * 0.92), 1)
    x0, x1 = int(width * 0.20), max(int(width * 0.80), 1)
    sample = frames[::frame_step, y0:y1:4, x0:x1:4, :3]
    if not int(sample.numel()):
        sample = frames[::frame_step, ::4, ::4, :3]
    rgb = torch.clamp(sample.detach().float(), 0.0, 1.0).mul_(255.0)
    luma = rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722
    maximum, minimum = torch.amax(rgb, dim=-1), torch.amin(rgb, dim=-1)
    saturation = torch.where(
        maximum > 0.0,
        (maximum - minimum) / torch.clamp(maximum, min=1.0) * 255.0,
        torch.zeros_like(maximum),
    )
    valid = (luma >= 20.0) & (luma <= 235.0)
    if int(torch.count_nonzero(valid).item()) >= 64:
        luma, saturation = luma[valid], saturation[valid]
    luma_values = torch.quantile(
        luma.reshape(-1), torch.tensor((0.10, 0.50, 0.90), device=luma.device),
    )
    saturation_values = torch.quantile(
        saturation.reshape(-1),
        torch.tensor((0.25, 0.50, 0.75), device=saturation.device),
    )
    return {
        "version": _STATS_VERSION,
        "luma_percentiles": [float(item) for item in luma_values.detach().cpu()],
        "saturation_percentiles": [
            float(item) for item in saturation_values.detach().cpu()
        ],
        "sampled_frames": len(range(0, count, frame_step)),
    }


def _validated_stats(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != _STATS_VERSION:
        raise ValueError(f"{label} has no compatible color statistics")
    luma = tuple(float(item) for item in value.get("luma_percentiles", ()))
    saturation = tuple(float(item) for item in value.get("saturation_percentiles", ()))
    if len(luma) != 3 or len(saturation) != 3 or not all(
        math.isfinite(item) for item in (*luma, *saturation)
    ):
        raise ValueError(f"{label} color statistics are malformed")
    return value


def _coherent_delta(values: tuple[float, ...], minimum: float) -> float:
    positive = tuple(item for item in values if item > 0.0)
    negative = tuple(item for item in values if item < 0.0)
    selected = positive if len(positive) >= 2 else negative if len(negative) >= 2 else ()
    if not selected:
        return 0.0
    value = float(statistics.median(selected))
    return value if abs(value) >= float(minimum) else 0.0


def scene_color_transform(anchor: Any, current: Any) -> tuple[float, float]:
    target = _validated_stats(anchor, "color anchor")
    source = _validated_stats(current, "continuation source")
    luma_shift = _coherent_delta(
        tuple(
            float(target_value) - float(source_value)
            for target_value, source_value in zip(
                target["luma_percentiles"], source["luma_percentiles"]
            )
        ),
        float(COLOR_STABLE_RECIPE["minimum_luma_shift_code_values"]),
    )
    strength = float(COLOR_STABLE_RECIPE["strength"])
    maximum_luma = float(COLOR_STABLE_RECIPE["max_luma_shift_code_values"])
    luma_shift = max(-maximum_luma, min(maximum_luma, luma_shift * strength))
    saturation_delta = _coherent_delta(
        tuple(
            float(target_value) / max(float(source_value), 1.0) - 1.0
            for target_value, source_value in zip(
                target["saturation_percentiles"], source["saturation_percentiles"]
            )
        ),
        float(COLOR_STABLE_RECIPE["minimum_saturation_ratio_change"]),
    ) * strength
    maximum_saturation = float(COLOR_STABLE_RECIPE["max_saturation_change"])
    saturation_delta = max(
        -maximum_saturation, min(maximum_saturation, saturation_delta)
    )
    return luma_shift / 255.0, 1.0 + saturation_delta


def color_transform_is_neutral(brightness: float, saturation: float) -> bool:
    return (
        abs(float(brightness))
        < float(COLOR_STABLE_RECIPE["neutral_brightness_threshold"])
        and abs(float(saturation) - 1.0)
        < float(COLOR_STABLE_RECIPE["neutral_saturation_threshold"])
    )


def apply_rgb_color_transform(frames, brightness: float, saturation: float):
    result = frames.detach().contiguous().clone()
    rgb = torch.clamp(result[..., :3].float(), 0.0, 1.0)
    luma = rgb[..., 0:1] * 0.2126 + rgb[..., 1:2] * 0.7152 + rgb[..., 2:3] * 0.0722
    corrected = luma + (rgb - luma) * float(saturation)
    result[..., :3] = torch.clamp(corrected + float(brightness), 0.0, 1.0).to(
        device=result.device, dtype=result.dtype,
    )
    return result


def temporal_delta_weights(steps: int) -> torch.Tensor:
    count = int(steps)
    if count < 2:
        raise ValueError("Color-Stable Drift AV requires at least two video steps")
    position = torch.linspace(0.0, 1.0, count, dtype=torch.float32)
    return position.square().mul_(3.0 - 2.0 * position)


def _spatial_lowpass(delta: torch.Tensor, kernel_size: int) -> torch.Tensor:
    kernel = int(kernel_size)
    if kernel <= 1:
        return delta
    if kernel % 2 != 1:
        raise ValueError("Color-Stable spatial kernel must be odd")
    radius = kernel // 2
    padded = functional.pad(
        delta.float(), (radius, radius, radius, radius, 0, 0), mode="replicate",
    )
    return functional.avg_pool3d(
        padded, kernel_size=(1, kernel, kernel), stride=1,
    ).to(device=delta.device, dtype=delta.dtype)


def decode_prefix_and_stats(video_prefix: Any, vae: Any) -> tuple[torch.Tensor, dict[str, Any]]:
    if not torch.is_tensor(video_prefix) or video_prefix.ndim != 5:
        raise ValueError("Color-Stable Drift AV requires video prefix [B,C,T,H,W]")
    decoded = vae.decode(video_prefix)
    if not torch.is_tensor(decoded):
        raise ValueError("Color-Stable video VAE did not return IMAGE")
    if decoded.ndim == 5:
        decoded = decoded.reshape(-1, *decoded.shape[-3:])
    if decoded.ndim != 4 or int(decoded.shape[0]) != int(COLOR_STABLE_RECIPE["context_frames"]):
        raise ValueError("Color-Stable VAE decode did not produce the required 39 frames")
    return decoded, tensor_scene_color_stats(decoded)


def cached_prefix_stats(
    video_prefix: Any, vae: Any, *, artifact_sha256: str, width: int, height: int,
) -> tuple[dict[str, Any], torch.Tensor | None, bool]:
    identity = vae_identity(vae)
    key = (
        str(artifact_sha256), int(width), int(height), identity["fingerprint"],
        str(COLOR_STABLE_RECIPE["version"]),
    )
    cached = _STATS_CACHE.get(key)
    if cached is not None:
        return dict(cached), None, True
    decoded, stats = decode_prefix_and_stats(video_prefix, vae)
    if len(_STATS_CACHE) >= _STATS_CACHE_LIMIT:
        _STATS_CACHE.pop(next(iter(_STATS_CACHE)))
    _STATS_CACHE[key] = dict(stats)
    return stats, decoded, False


def apply_color_delta(
    video_prefix: Any, vae: Any, *, anchor_stats: Any, current_stats: Any,
    decoded_current: torch.Tensor | None = None,
) -> tuple[torch.Tensor, dict[str, Any]]:
    if not torch.is_tensor(video_prefix) or video_prefix.ndim != 5:
        raise ValueError("Color-Stable Drift AV requires video prefix [B,C,T,H,W]")
    if int(video_prefix.shape[2]) != int(COLOR_STABLE_RECIPE["video_steps"]):
        raise ValueError("Color-Stable Drift AV requires a 12-step video prefix")
    brightness, saturation = scene_color_transform(anchor_stats, current_stats)
    summary = {
        "version": COLOR_STABLE_RECIPE["version"],
        "applied": False,
        "neutral": color_transform_is_neutral(brightness, saturation),
        "brightness": float(brightness),
        "saturation": float(saturation),
        "video_steps": int(video_prefix.shape[2]),
        "audio": "unchanged",
    }
    if summary["neutral"]:
        return video_prefix.detach().contiguous().clone(), summary
    decoded = decoded_current
    if decoded is None:
        decoded, _stats = decode_prefix_and_stats(video_prefix, vae)
    baseline = vae.encode(decoded)
    corrected = vae.encode(apply_rgb_color_transform(decoded, brightness, saturation))
    if tuple(baseline.shape) != tuple(video_prefix.shape) or tuple(corrected.shape) != tuple(video_prefix.shape):
        raise ValueError("Color-Stable VAE round-trip shape differs from the prefix")
    device, dtype = video_prefix.device, video_prefix.dtype
    delta = corrected.to(device=device, dtype=dtype) - baseline.to(device=device, dtype=dtype)
    delta = _spatial_lowpass(delta, int(COLOR_STABLE_RECIPE["spatial_lowpass_kernel"]))
    weights = temporal_delta_weights(int(video_prefix.shape[2])).to(device=device, dtype=dtype)
    result = video_prefix.detach().contiguous().clone()
    result.add_(delta * weights.view(1, 1, -1, 1, 1))
    summary.update({
        "applied": True,
        "neutral": False,
        "delta_rms": float(delta.detach().float().square().mean().sqrt().cpu().item()),
    })
    return result, summary


__all__ = [
    "COLOR_STABLE_RECIPE", "apply_color_delta", "apply_rgb_color_transform",
    "cached_prefix_stats", "color_transform_is_neutral", "decode_prefix_and_stats",
    "scene_color_transform", "temporal_delta_weights", "tensor_scene_color_stats",
    "vae_identity",
]
