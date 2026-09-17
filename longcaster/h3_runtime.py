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
    additional_reference_images: dict[str, Any] | None = None,
    card_reference: dict[str, Any] | None = None,
    card_reference_image: Any | None = None,
) -> tuple[Any, dict[str, Any], dict[str, Any] | None]:
    from comfy_extras.nodes_minimax_h3 import (
        MiniMaxH3ImageToVideo,
        MiniMaxH3ReferenceToVideo,
    )

    card_enabled = bool(card_reference and card_reference.get("enabled"))
    if card_enabled and reference_packet is None:
        raise ValueError("card references require REF2VA and a reference packet")
    if reference_packet is None and not additional_reference_images:
        output = MiniMaxH3ImageToVideo.execute(
            clip, video_vae, prompt, int(width), int(height), int(frames)
        )
        return output[0], output[1], None

    from .mmh3_adapter import materialize_native_references

    if reference_packet is None:
        raise ValueError("additional native identity references require a REF2VA reference packet")
    references = materialize_native_references(
        reference_packet, width=width, height=height, target_frames=frames,
        ref_image_size=ref_image_size,
    )
    images = dict(references.images)
    card_vae = None
    if card_enabled:
        from .card_reference import CachedCardVAE
        picture_index = 1 + sum(image is not None for image in images.values())
        if card_reference_image is None or picture_index != card_reference["resolved_picture_index"]:
            raise ValueError("card reference image or resolved native picture index is inconsistent")
        images[f"ref_image_{len(images)}_card"] = card_reference_image
        card_vae = CachedCardVAE(video_vae, card_reference, picture_index)
    for name, image in (additional_reference_images or {}).items():
        images[f"ref_image_{len(images)}_{name}"] = image
    if len(images) > 9:
        raise ValueError("MiniMax H3 supports at most 9 image references including card references and the identity anchor")
    output = MiniMaxH3ReferenceToVideo.execute(
        clip=clip, vae=card_vae if card_vae is not None else video_vae,
        audio_vae=audio_vae, prompt=prompt, width=int(width), height=int(height),
        length=int(frames), ref_image_size=ref_image_size, ref_images=images,
        ref_videos=references.videos, ref_video_audios=references.video_audios,
        ref_audios=references.audios,
    )
    positive = output[0]
    if card_vae is not None:
        from .card_reference import substitute_card_latent
        positive = substitute_card_latent(
            positive, picture_index=card_reference["resolved_picture_index"],
            clean=card_vae.clean, mode=card_reference["latent_mode"],
            strength=card_reference["latent_strength"],
        )
    report = dict(references.report)
    report["longcaster_additional_reference_images"] = len(additional_reference_images or {})
    if card_reference is not None:
        report["card_reference"] = dict(card_reference)
    return positive, output[1], report


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
