from __future__ import annotations

import math
from typing import Any


def sigma_values(sigmas: Any) -> list[float]:
    try:
        values = sigmas.detach().cpu().double().flatten().tolist()
    except AttributeError as exc:
        raise ValueError("SIGMAS must be a torch tensor") from exc
    if len(values) < 2:
        raise ValueError("SIGMAS must contain at least one sampling step and a terminal zero")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("SIGMAS contains a non-finite value")
    if values[0] <= 0:
        raise ValueError("SIGMAS must begin above zero")
    if abs(values[-1]) > 1e-8:
        raise ValueError("SIGMAS must end at zero")
    if any(right > left + 1e-8 for left, right in zip(values, values[1:])):
        raise ValueError("SIGMAS must be monotonically non-increasing")
    return values


def generated_sigmas(model: Any, scheduler: str, steps: int) -> Any:
    import comfy.samplers

    return comfy.samplers.calculate_sigmas(
        model.get_model_object("model_sampling"), scheduler, int(steps)
    ).cpu()


def build_conditioning(
    *,
    clip: Any,
    video_vae: Any,
    audio_vae: Any,
    prompt: str,
    width: int,
    height: int,
    frames: int,
    reference_packet: Any | None,
    ref_image_size: str,
) -> tuple[Any, dict[str, Any], dict[str, Any] | None]:
    from comfy_extras.nodes_minimax_h3 import (
        MiniMaxH3ImageToVideo,
        MiniMaxH3ReferenceToVideo,
    )

    if reference_packet is None:
        output = MiniMaxH3ImageToVideo.execute(
            clip, video_vae, prompt, int(width), int(height), int(frames)
        )
        return output[0], output[1], None

    from .mmh3_adapter import materialize_native_references

    references = materialize_native_references(
        reference_packet,
        width=width,
        height=height,
        target_frames=frames,
        ref_image_size=ref_image_size,
    )
    output = MiniMaxH3ReferenceToVideo.execute(
        clip,
        video_vae,
        audio_vae,
        prompt,
        int(width),
        int(height),
        int(frames),
        ref_image_size,
        references.images,
        references.videos,
        references.video_audios,
        references.audios,
    )
    return output[0], output[1], references.report


def sample_h3(
    *,
    model: Any,
    positive: Any,
    latent: dict[str, Any],
    sigmas: Any,
    seed: int,
    sampler_name: str,
) -> dict[str, Any]:
    """Use Comfy's standard advanced sampler without cloning or replacing MODEL/SIGMAS."""
    from comfy_extras.nodes_custom_sampler import (
        BasicGuider,
        KSamplerSelect,
        RandomNoise,
        SamplerCustomAdvanced,
    )

    noise = RandomNoise.execute(int(seed))[0]
    guider = BasicGuider.execute(model, positive)[0]
    sampler = KSamplerSelect.execute(sampler_name)[0]
    # The exact SIGMAS object received here is forwarded to Comfy's sampler.
    return SamplerCustomAdvanced.execute(noise, guider, sampler, sigmas, latent)[0]


def runtime_capabilities() -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        import comfy.model_base

        cls = comfy.model_base.MiniMaxH3
        result["h3_joint_mask_conditioning"] = (
            "audio_denoise_mask" in cls.extra_conds.__code__.co_names
            or "audio_denoise_mask" in cls.extra_conds.__code__.co_consts
        )
        result["h3_scale_latent_inpaint"] = "scale_latent_inpaint" in cls.__dict__
    except Exception as exc:
        result["h3_runtime_probe_error"] = str(exc)
    return result
