"""Card-owned picture inputs and a bounded, process-local clean VAE cache."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import math
import threading
import uuid
import weakref

from .fingerprint import generation_fingerprint

CACHE_VERSION = "card-ref-native-v1"
_CACHE = OrderedDict()
_LOCK = threading.RLock()
_VAES = weakref.WeakKeyDictionary()


def validate_reference(reference, card_id=None):
    if reference is None:
        return
    if not isinstance(reference, dict):
        raise ValueError("card_reference must be an object or null")
    uuid.UUID(reference["id"])
    if not isinstance(reference.get("enabled"), bool):
        raise ValueError("card reference enabled must be boolean")
    if reference.get("latent_mode") not in {"normal", "soften"}:
        raise ValueError("card reference latent_mode must be normal or soften")
    strength = reference.get("latent_strength")
    if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError("card reference latent_strength must be between 0 and 1")
    digest = reference.get("asset_sha256", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("card reference requires a SHA-256")
    if card_id and reference.get("asset_path") != f"card_references/{card_id}/{reference['id']}.png":
        raise ValueError("card reference asset must belong to its card")


def vae_identity(vae):
    """No cross-process/model reuse; also detect in-place torch weight changes."""
    with _LOCK:
        token = _VAES.get(vae)
        if token is None:
            token = str(uuid.uuid4())
            _VAES[vae] = token
    model = getattr(vae, "first_stage_model", None)
    versions = []
    if model is not None and hasattr(model, "parameters"):
        for parameter in model.parameters():
            try:
                versions.append((id(parameter), parameter._version))
            except RuntimeError:
                # Unversioned inference tensors cannot establish safe reuse.
                versions.append(str(uuid.uuid4()))
    return generation_fingerprint({
        "instance": token, "model": id(model), "versions": versions,
        "patches": str(getattr(getattr(vae, "patcher", None), "patches_uuid", "")),
    })


def prepare_reference(store, reference, *, references, width, height, ref_image_size, video_vae):
    """Freeze resolved metadata before starting a generation transaction."""
    if reference is None:
        return None, None
    from .project import ProjectError, sha256_file
    validate_reference(reference)
    result = deepcopy(reference)
    if not result["enabled"]:
        return result, None
    from PIL import Image
    import numpy as np
    import torch
    from comfy_extras import nodes_minimax_h3 as native

    path = store.absolute_path(result["asset_path"])
    if not path.is_file() or sha256_file(path) != result["asset_sha256"]:
        raise ProjectError("card reference image is missing or its hash changed")
    with Image.open(path) as source:
        rgb = np.array(source.convert("RGB"), dtype=np.float32) / 255.0
    image = torch.from_numpy(rgb).unsqueeze(0)
    h, w = image.shape[1:3]
    scale = min(1.0, math.sqrt(width * height / (w * h))) if ref_image_size == "match" else min(1.0, native.REF_IMAGE_SHORT_EDGE / min(w, h))
    multiple = native.CANVAS_MULTIPLE
    dimensions = [max(multiple, round(size * scale / multiple) * multiple) for size in (w, h)]
    result.update({
        "resolved_picture_index": 1 + sum(item.get("kind") == "image" for item in (references or {}).get("resources", [])),
        "resized_width": dimensions[0], "resized_height": dimensions[1],
        "ref_image_size": ref_image_size, "canvas": [int(width), int(height)],
        "vae_identity": vae_identity(video_vae), "cache_version": CACHE_VERSION,
    })
    result["clean_cache_key"] = clean_cache_key(result)
    return result, image


def clean_cache_key(reference):
    return generation_fingerprint({name: reference.get(name) for name in (
        "asset_sha256", "resized_width", "resized_height", "ref_image_size",
        "canvas", "vae_identity", "cache_version",
    )})


class CachedCardVAE:
    """Delegate native encoding, caching only the mapped card picture call.

    Native Ref2VA resizes and presents RGB itself. This per-call proxy never
    monkeypatches the connected VAE or changes the native resize/tokenizer path.
    """
    def __init__(self, vae, reference, picture_index):
        self.vae = vae
        self.reference = reference
        self.picture_index = picture_index
        self.calls = 0
        self.clean = None

    def __getattr__(self, name):
        return getattr(self.vae, name)

    def encode(self, image, *args, **kwargs):
        self.calls += 1
        if self.calls != self.picture_index:
            return self.vae.encode(image, *args, **kwargs)
        expected = (1, self.reference["resized_height"], self.reference["resized_width"], 3)
        if tuple(image.shape) != expected or args or kwargs:
            raise ValueError("native card-reference encoding contract changed; refusing cached substitution")
        key = clean_cache_key(self.reference)
        with _LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                _CACHE.move_to_end(key)
                tensor, device = cached
                self.clean = tensor.to(device=device, copy=True)
                return self.clean
        self.clean = self.vae.encode(image)
        with _LOCK:
            _CACHE[key] = (self.clean.detach().cpu().clone(), self.clean.device)
            _CACHE.move_to_end(key)
            while len(_CACHE) > 8 or sum(t.numel() * t.element_size() for t, _ in _CACHE.values()) > 256 * 1024 * 1024:
                _CACHE.popitem(last=False)
        return self.clean


def substitute_card_latent(positive, *, picture_index, clean, mode, strength):
    """Copy the conditioning containers; replace exactly one image block."""
    if clean is None:
        raise ValueError("native reference node did not encode the card picture")
    effective = clean if mode == "normal" or strength == 1 else clean * strength
    result = []
    for embedding, metadata in positive:
        refs = metadata.get("minimax_refs", [])
        image_blocks = [i for i, block in enumerate(refs) if block.get("kind") == "image"]
        if len(image_blocks) < picture_index:
            raise ValueError("native reference node did not produce the expected card image block")
        index = image_blocks[picture_index - 1]
        if refs[index].get("latent") is not clean:
            raise ValueError("native card-reference mapping changed; refusing to modify another picture")
        cloned_refs = list(refs)
        cloned_refs[index] = {**refs[index], "latent": effective}
        result.append([embedding, {**metadata, "minimax_refs": cloned_refs}])
    return result


def prepare_refine_reference(store, recipe, *, references, width, height, ref_image_size, video_vae):
    """Resolve ONLY the accepted recipe; callers cannot supply child-card inputs."""
    frozen = recipe.get("card_reference")
    resolved, image = prepare_reference(
        store, frozen, references=references, width=width, height=height,
        ref_image_size=ref_image_size, video_vae=video_vae,
    )
    if frozen and frozen.get("enabled"):
        from .project import ProjectError
        for field in ("resolved_picture_index", "resized_width", "resized_height", "ref_image_size"):
            if resolved[field] != frozen[field]:
                raise ProjectError("refine card reference no longer matches the accepted recipe")
    return resolved, image


def prepare_hires_reference(store, recipe, *, references, width, height, ref_image_size, video_vae):
    """Keep the accepted picture/mode/strength while rebuilding target-size latents."""
    frozen = recipe.get("card_reference")
    resolved, image = prepare_reference(
        store, frozen, references=references, width=width, height=height,
        ref_image_size=ref_image_size, video_vae=video_vae,
    )
    if frozen and frozen.get("enabled"):
        from .project import ProjectError
        if resolved["resolved_picture_index"] != frozen["resolved_picture_index"]:
            raise ProjectError("hires card reference no longer matches the accepted picture index")
    return resolved, image
