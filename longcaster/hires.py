"""Hires-only contracts. Native continuation never consults these derivatives."""
from __future__ import annotations

import math
from typing import Any

from .refine import RefineError, _streams, _rebuild_like, _expanded_mask


HIRES_CADENCES = {"off", "manual"}
AUDIO_HANDLING = "locked_native_audio"


def resolve_target(width: int, height: int, *, scale: float = 2.0,
                   target_width: int = 0, target_height: int = 0) -> dict[str, Any]:
    if not math.isfinite(scale) or not 1.0 < scale <= 4.0:
        raise ValueError("hires scale must be greater than 1 and at most 4")
    if bool(target_width) != bool(target_height):
        raise ValueError("specify both hires target dimensions, or leave both at zero")
    tw = target_width or round(width * scale / 32) * 32
    th = target_height or round(height * scale / 32) * 32
    if any(isinstance(x, bool) or not isinstance(x, int) or x % 32 or x > 8192 for x in (tw, th)):
        raise ValueError("hires target dimensions must be multiples of 32, at most 8192")
    if tw <= width or th <= height or tw > width * 4 or th > height * 4:
        raise ValueError("hires target must enlarge both dimensions by at most 4x")
    return {"source_width": width, "source_height": height,
            "target_width": tw, "target_height": th,
            "scale": (tw / width + th / height) / 2, "alignment": 32}


def joint_streams(latent: dict[str, Any]) -> tuple[Any, Any]:
    import torch
    streams = _streams(latent.get("samples"), label="hires joint AV")
    if len(streams) != 2 or not all(torch.is_tensor(x) for x in streams):
        raise RefineError("hires requires exactly two joint video/audio tensors")
    video, audio = streams
    if video.ndim != 5 or video.shape[1] != 24:
        raise RefineError("hires requires H3 24-channel BCTHW video latents")
    if audio.ndim < 3 or audio.shape[1] != 32:
        raise RefineError("hires requires H3 32-channel native audio latents")
    return video, audio


def prepare_hires_latent(accepted: dict[str, Any], lifted_video: Any,
                         continuation_lock: dict[str, Any] | None = None,
                         *, parent_video: Any = None, dc_clamp: float = 0.05) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy the actual HR handover and lock *all* accepted audio.

    The MMH3 handover supplies the authoritative protected region, including
    temporal packing. No pixel-frame-to-latent-token conversion is guessed here.
    DC adjustment touches only the disposable prediction, then is overwritten.
    """
    import torch
    native_video, audio = joint_streams(accepted)
    if (lifted_video.ndim != 5 or lifted_video.shape[:3] != native_video.shape[:3]
            or not torch.isfinite(lifted_video).all()):
        raise RefineError("learned upscaler changed the video layout or produced non-finite values")
    video = lifted_video.detach().cpu().clone()
    audio = audio.detach().cpu().clone()
    mask = torch.ones((video.shape[0], 1, video.shape[2], 1, 1), dtype=video.dtype)
    report = {"method": "locked_native_audio", "video_protected_values": 0,
              "audio_protected_values": audio.numel(), "dc_correction": {
                  "channels_corrected": 0, "maximum_absolute_correction": 0.0,
                  "compared_latent_tokens": 0, "clamp": dc_clamp}}
    if not math.isfinite(dc_clamp) or not 0 <= dc_clamp <= 0.1:
        raise RefineError("hires DC clamp must be between 0 and 0.1")
    if continuation_lock is not None:
        source, _ = joint_streams(continuation_lock)
        masks = _streams(continuation_lock.get("noise_mask"), label="hires handover mask")
        if len(masks) != 2 or source.shape != video.shape or source.dtype != video.dtype:
            raise RefineError("hires parent handover layout or dtype does not match the target")
        source = source.detach().cpu()
        protected = _expanded_mask(masks[0].cpu(), video) <= 1e-8
        # Only a contiguous, spatially complete video opening is supported.
        steps = protected.all(dim=(0, 1, 3, 4))
        count = int(steps.sum().item())
        expected = torch.zeros_like(protected)
        expected[:, :, :count] = True
        if count < 1 or not torch.equal(protected, expected):
            raise RefineError("hires handover must protect a complete contiguous video prefix")
        if parent_video is not None:
            tail = parent_video.detach().cpu()[:, :, -count:]
            if tail.shape != source[:, :, :count].shape or tail.dtype != source.dtype or not torch.equal(
                tail.contiguous().view(torch.uint8), source[:, :, :count].contiguous().view(torch.uint8)
            ):
                raise RefineError("hires handover prefix is not the bit-exact parent tail")
        correction = (source[:, :, :count].float() - video[:, :, :count].float())
        correction = correction.permute(1, 0, 2, 3, 4).flatten(1).median(dim=1).values
        correction = correction.clamp(-dc_clamp, dc_clamp)
        video[:, :, :count] += correction.view(1, -1, 1, 1, 1).to(video.dtype)
        video[:, :, :count].copy_(source[:, :, :count])
        mask[:, :, :count] = 0
        report.update(method="inherited_hires_tail_and_locked_native_audio",
                      video_protected_values=int(protected.sum().item()),
                      protected_video_steps=count,
                      dc_correction={"channels_corrected": int((correction != 0).sum().item()),
                                     "maximum_absolute_correction": float(correction.abs().max().item()),
                                     "compared_latent_tokens": int(protected[:, :1].sum().item()),
                                     "clamp": dc_clamp})
    result = {k: v for k, v in accepted.items() if k not in {"samples", "noise_mask"}}
    result["samples"] = _rebuild_like(accepted["samples"], [video, audio])
    result["noise_mask"] = _rebuild_like(accepted["samples"], [mask, torch.zeros_like(audio)])
    return result, report


def protection_snapshot(latent: dict[str, Any]) -> dict[str, Any]:
    """Separate storage so an in-place sampler cannot defeat the assertion."""
    return {"samples": _rebuild_like(latent["samples"],
                                    [x.detach().cpu().clone() for x in joint_streams(latent)]),
            "noise_mask": _rebuild_like(latent["noise_mask"],
                                       [x.detach().cpu().clone() for x in _streams(latent["noise_mask"], label="mask")])}


def assert_hires_unchanged(before: dict[str, Any], after: dict[str, Any]) -> None:
    import torch
    for name, source, result, mask in zip(
        ("video", "audio"), joint_streams(before), joint_streams(after),
        _streams(before["noise_mask"], label="hires mask"),
    ):
        if source.shape != result.shape or source.dtype != result.dtype:
            raise RefineError(f"hires sampler changed the {name} layout or dtype")
        protected = _expanded_mask(mask.cpu(), source.cpu()) <= 1e-8
        # Compare bytes as well as dtype: no tolerance, conversion, or signed-zero drift.
        a = source.cpu()[protected].contiguous().view(torch.uint8)
        b = result.detach().cpu()[protected].contiguous().view(torch.uint8)
        if not torch.equal(a, b):
            raise RefineError(f"hires sampler changed protected {name} values")
