"""Memory-bounded adapter for the LBH learned H3 3D resizer (MIT)."""
from __future__ import annotations

import gc
import logging
import math
from pathlib import Path
import time
from typing import Any

LOGGER = logging.getLogger("longcaster")
UPSCALER_VERSION = "lbh_3d_temporal32_v1"


def model_names() -> list[str]:
    import folder_paths
    category = "latent_upscale_models"
    if category not in folder_paths.folder_names_and_paths:
        folder_paths.add_model_folder_path(category, str(Path(folder_paths.models_dir) / category))
    return [x for x in folder_paths.get_filename_list(category)
            if Path(x).suffix.lower() in {".safetensors", ".pth", ".pt"}] or ["(select an H3 3D upscaler model)"]


def model_path(name: str) -> Path:
    import folder_paths
    model_names()
    if not name or name.startswith("("):
        raise ValueError("select an H3 learned 3D checkpoint in models/latent_upscale_models")
    return Path(folder_paths.get_full_path_or_raise("latent_upscale_models", name))


def upscale_video(video: Any, *, model_name: str, target_width: int,
                  target_height: int, temporal_chunking: bool = True,
                  precision: str = "bf16", force_unload: bool = True) -> tuple[Any, dict[str, Any]]:
    import torch
    import comfy.model_management as mm
    import comfy.utils
    from .project import sha256_file
    from .vendor.h3_resizer import LatentResizer3D, _detect_arch, _extract_upscaler_sd, _make_norm_tensors

    if video.ndim != 5 or video.shape[1] != 24:
        raise ValueError("H3 learned 3D upscaler requires a 24-channel BCTHW video tensor")
    if precision not in {"bf16", "fp16"}:
        raise ValueError("hires precision must be bf16 or fp16")
    if target_width % 32 or target_height % 32:
        raise ValueError("hires target dimensions must align to 32 pixels")
    if not force_unload:
        raise ValueError("hires requires force_unload before H3 refinement")
    checkpoint = model_path(model_name)
    checksum = sha256_file(checkpoint)
    device = mm.get_torch_device()
    if precision == "bf16" and device.type == "cuda" and not torch.cuda.is_bf16_supported():
        precision = "fp16"
    dtype = {"bf16": torch.bfloat16, "fp16": torch.float16}[precision]
    # Return memory held by Comfy-managed models before allocating the upscaler.
    mm.unload_all_models()
    mm.soft_empty_cache()
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
    started = time.perf_counter()
    model = None
    try:
        raw = comfy.utils.load_torch_file(str(checkpoint), safe_load=True)
        sd = _extract_upscaler_sd(raw.get("model", raw))
        cfg = _detect_arch(sd)
        if cfg["in_channels"] != 24 or sd["conv_in.weight"].ndim != 5:
            raise ValueError("checkpoint is not an H3 learned 24-channel 3D upscaler")
        # Strict loading rejects unsupported architectures rather than interpolating.
        with torch.device("meta"):
            model = LatentResizer3D(**cfg)
        model.load_state_dict(sd, strict=True, assign=True)
        del raw, sd
        model = model.to(device=device, dtype=dtype).eval().requires_grad_(False)
        scale = (target_width / (video.shape[-1] * 16) + target_height / (video.shape[-2] * 16)) / 2
        with torch.inference_mode():
            mean, std = _make_norm_tensors(device, dtype)
            normalized = (video.to(device=device, dtype=dtype) - mean) / std
            output = model(normalized, scale=scale,
                           target_size=(video.shape[2], target_height // 16, target_width // 16),
                           enable_chunking=temporal_chunking,
                           interrupt=mm.throw_exception_if_processing_interrupted)
            output = (output * std + mean).to(device="cpu", dtype=video.dtype)
            del normalized, mean, std
        if tuple(output.shape) != (*video.shape[:3], target_height // 16, target_width // 16):
            raise ValueError("learned upscaler returned an unexpected target latent shape")
        if not torch.isfinite(output).all():
            raise ValueError("learned upscaler produced non-finite values; try bf16")
        report = {"upscaler_version": UPSCALER_VERSION, "upscaler_sha256": checksum,
                  "upscaler_config": cfg, "precision": precision,
                  "upscale_seconds": time.perf_counter() - started,
                  "target_latent_shape": list(output.shape),
                  "temporal_chunk_count": math.ceil(video.shape[2] / 32) if temporal_chunking else 1}
        if device.type == "cuda":
            report.update(peak_vram_bytes=torch.cuda.max_memory_allocated(device),
                          current_vram_bytes=torch.cuda.memory_allocated(device))
        LOGGER.info("Hires learned upscale: %s", report)
        return output, report
    finally:
        if model is not None:
            # A failed strict load may leave a meta model; it owns no GPU storage.
            if not any(p.is_meta for p in model.parameters()):
                model.to("cpu")
            del model
        gc.collect()
        mm.soft_empty_cache()
