from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import time
from typing import Any, Protocol

from .mmh3_adapter import prepare_continuation, primary_latent


CONTINUATION_TREATMENTS = {"off", "drift_control", "color_stable"}


def validate_continuation_treatment(value: str) -> str:
    mode = str(value)
    if mode not in CONTINUATION_TREATMENTS:
        raise ValueError("continuation treatment must be off, drift_control, or color_stable")
    return mode


def _streams(latent: dict[str, Any]) -> list[Any]:
    import torch
    samples = latent.get("samples") if isinstance(latent, dict) else None
    if hasattr(samples, "unbind"):
        values = list(samples.unbind())
    elif hasattr(samples, "tensors"):
        values = list(samples.tensors)
    else:
        values = list(samples or ())
    if len(values) != 2 or not all(torch.is_tensor(item) for item in values):
        raise ValueError("continuation treatment requires a two-stream H3 AV latent")
    return values


def _replace_video(latent: dict[str, Any], video: Any) -> dict[str, Any]:
    output = {key: value for key, value in latent.items()}
    samples = latent["samples"]
    _old_video, audio = _streams(latent)
    if isinstance(samples, tuple):
        output["samples"] = (video, audio)
    elif isinstance(samples, list):
        output["samples"] = [video, audio]
    else:
        try:
            output["samples"] = type(samples)((video, audio))
        except (TypeError, RuntimeError):
            from comfy.nested_tensor import NestedTensor
            output["samples"] = NestedTensor((video, audio))
    return output


def _video_tail(packet: Any, steps: int = 12) -> Any:
    latent, _index = primary_latent(packet)
    video, _audio = _streams(latent)
    if video.ndim != 5 or int(video.shape[2]) < int(steps):
        raise ValueError("color baseline artifact has no 12-step H3 video tail")
    return video[:, :, -int(steps):].detach().contiguous().clone()


@dataclass
class ContinuationPolicyState:
    """Prepared disposable latent plus a sampling-stage binding hook.

    SelfLift can later create one instance per stage and call bind_sampling_stage
    with that stage's exact latent and sigma schedule.
    """

    latent: dict[str, Any]
    plan: dict[str, Any]
    mode: str
    provenance: dict[str, Any]
    preparation_seconds: float = 0.0

    def bind_sampling_stage(self, model: Any, sigmas: Any) -> Any:
        if self.mode == "off":
            return model
        from .drift_control import install_drift_control_model
        return install_drift_control_model(model, self.latent, sigmas)


def prepare_continuation_policy(
    latent: dict[str, Any],
    plan: dict[str, Any],
    *,
    mode: str,
    video_vae: Any | None = None,
    source_packet: Any | None = None,
    source_sha256: str | None = None,
    anchor_packet: Any | None = None,
    anchor_sha256: str | None = None,
    anchor_card_id: str | None = None,
    source_card_id: str | None = None,
    width: int = 0,
    height: int = 0,
) -> ContinuationPolicyState:
    """Prepare a disposable handover policy; archived source packets stay read-only."""
    treatment = validate_continuation_treatment(mode)
    started = time.perf_counter()
    if treatment == "off":
        return ContinuationPolicyState(
            latent, deepcopy(plan), treatment,
            {
                "mode": "off", "drift_recipe": None, "color_recipe": None,
                "source_card_id": source_card_id,
                "source_artifact_sha256": source_sha256,
                "color_anchor_source_card_id": anchor_card_id,
                "color_anchor_artifact_sha256": anchor_sha256,
                "color": {"applied": False, "neutral": True, "reason": "disabled"},
            },
            time.perf_counter() - started,
        )
    from .color_stable import (
        COLOR_STABLE_RECIPE, apply_color_delta, cached_prefix_stats, vae_identity,
    )
    from .drift_control import DRIFT_CONTROL_RECIPE
    output = latent
    report: dict[str, Any] = {
        "mode": treatment,
        "drift_recipe": deepcopy(DRIFT_CONTROL_RECIPE) if treatment != "off" else None,
        "color_recipe": deepcopy(COLOR_STABLE_RECIPE) if treatment == "color_stable" else None,
        "source_card_id": source_card_id,
        "source_artifact_sha256": source_sha256,
        "color_anchor_source_card_id": anchor_card_id,
        "color_anchor_artifact_sha256": anchor_sha256,
        "color": {"applied": False, "neutral": True, "reason": "disabled"},
    }
    if treatment == "color_stable":
        video, _audio = _streams(latent)
        output = _replace_video(latent, video.detach().contiguous().clone())
        if video_vae is None:
            raise ValueError("Color-Stable Drift AV requires the connected video VAE")
        report["video_vae"] = vae_identity(video_vae)
        if source_card_id == anchor_card_id:
            report["color"] = {
                "applied": False, "neutral": True,
                "reason": "first_continuation_from_chain_baseline",
            }
        else:
            if source_packet is None or anchor_packet is None or not source_sha256 or not anchor_sha256:
                raise ValueError("Color-Stable Drift AV requires verified source and baseline artifacts")
            source_video = _video_tail(source_packet)
            anchor_video = _video_tail(anchor_packet)
            anchor_stats, _anchor_decoded, anchor_cached = cached_prefix_stats(
                anchor_video, video_vae, artifact_sha256=anchor_sha256,
                width=width, height=height,
            )
            source_stats, source_decoded, source_cached = cached_prefix_stats(
                source_video, video_vae, artifact_sha256=source_sha256,
                width=width, height=height,
            )
            prepared_video, summary = apply_color_delta(
                _streams(output)[0][:, :, :12], video_vae,
                anchor_stats=anchor_stats, current_stats=source_stats,
                decoded_current=source_decoded,
            )
            target_video, _target_audio = _streams(output)
            corrected = target_video.clone()
            corrected[:, :, :12] = prepared_video
            output = _replace_video(output, corrected)
            report.update({
                "anchor_stats": anchor_stats,
                "source_stats": source_stats,
                "stats_cache": {"anchor_hit": anchor_cached, "source_hit": source_cached},
                "color": summary,
            })
    elapsed = time.perf_counter() - started
    return ContinuationPolicyState(output, deepcopy(plan), treatment, report, elapsed)


class ContinuationStrategy(Protocol):
    name: str

    def prepare(
        self,
        source_packet: Any,
        *,
        target_frames: int,
        width: int,
        height: int,
        context_frames: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        ...


@dataclass(frozen=True)
class DirectLatentContinuation:
    """MMH3 joint AV continuation without a VAE round trip."""

    name: str = "direct_latent"

    def prepare(
        self,
        source_packet: Any,
        *,
        target_frames: int,
        width: int,
        height: int,
        context_frames: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return prepare_continuation(
            source_packet,
            target_frames=target_frames,
            width=width,
            height=height,
            context_frames=context_frames,
        )


DIRECT_LATENT_CONTINUATION = DirectLatentContinuation()


__all__ = [
    "CONTINUATION_TREATMENTS", "ContinuationPolicyState",
    "DIRECT_LATENT_CONTINUATION", "DirectLatentContinuation",
    "prepare_continuation_policy", "validate_continuation_treatment",
]
