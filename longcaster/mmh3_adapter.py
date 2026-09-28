from __future__ import annotations

from dataclasses import dataclass
import importlib
import logging
import os
from pathlib import Path
import sys
from typing import Any, Mapping


class MMH3DependencyError(RuntimeError):
    pass


_MMH3_API: Any = None
LOGGER = logging.getLogger("longcaster.mmh3_adapter")


def _looks_like_api(module: Any) -> bool:
    return all(
        hasattr(module, name)
        for name in (
            "MMH3Media",
            "build_h3_continuation_handover",
            "get_resource_payload",
            "load_archive",
            "resolve_reference_set",
            "save_archive",
        )
    )


def _complete_api(module: Any) -> Any:
    if not hasattr(module, "pack_h3_result"):
        public_api = importlib.import_module(f"{module.__name__}.public_api")
        module.pack_h3_result = public_api.pack_h3_result
    return module


def _candidate_roots() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("LONGCASTER_MMH3_ROOT")
    if configured:
        candidates.append(Path(configured).expanduser())
    custom_nodes = Path(__file__).resolve().parents[2]
    if custom_nodes.is_dir():
        for child in custom_nodes.iterdir():
            if child.is_dir() and (child / "mmh3_media" / "__init__.py").is_file():
                candidates.append(child)
    return candidates


def mmh3_api() -> Any:
    global _MMH3_API
    if _MMH3_API is not None:
        return _MMH3_API

    for module in tuple(sys.modules.values()):
        if module is not None and _looks_like_api(module):
            name = getattr(module, "__name__", "")
            if name == "mmh3_media" or name.endswith(".mmh3_media"):
                _MMH3_API = _complete_api(module)
                return _MMH3_API

    try:
        module = importlib.import_module("mmh3_media")
        if _looks_like_api(module):
            _MMH3_API = _complete_api(module)
            return _MMH3_API
    except (ImportError, OSError):
        pass

    for root in _candidate_roots():
        root_string = str(root.resolve())
        if root_string not in sys.path:
            sys.path.insert(0, root_string)
        try:
            module = importlib.import_module("mmh3_media")
        except (ImportError, OSError):
            continue
        if _looks_like_api(module):
            _MMH3_API = _complete_api(module)
            return _MMH3_API

    raise MMH3DependencyError(
        "ComfyUI_mmh3_media is required. Install it beside this custom node and restart ComfyUI."
    )


def _h3_reference_module(api: Any) -> Any:
    return importlib.import_module(f"{api.__name__}.h3_references")


def load_packet(path: str | Path, *, verify: str = "on_access") -> Any:
    return mmh3_api().load_archive(str(Path(path).resolve()), verify=verify)


def primary_latent(packet: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    api = mmh3_api()
    descriptor = packet.get_primary("latent")
    if descriptor is None:
        raise ValueError("MMH3 card archive has no primary latent")
    contract = api.h3_latent_contract_from_resource(descriptor)
    latent = api.get_resource_payload(packet, descriptor)
    return latent, contract


def prepare_continuation(
    packet: Any,
    *,
    target_frames: int,
    width: int,
    height: int,
    context_frames: int = 39,
) -> tuple[dict[str, Any], dict[str, Any]]:
    api = mmh3_api()
    source, contract = primary_latent(packet)
    api.validate_h3_continuation_compatibility(contract)
    result = api.build_h3_continuation_handover(
        source,
        source_origin=contract["origin"],
        target_frames=int(target_frames),
        video_handover_frames=int(context_frames),
        audio_handover_frames=int(context_frames),
        target_width=int(width),
        target_height=int(height),
    )
    return result.latent, result.plan.to_dict()


@dataclass(frozen=True)
class _CompatDiagnostic:
    severity: str
    code: str
    message: str
    resource_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "resource_ids": list(self.resource_ids),
        }


@dataclass(frozen=True)
class _CompatResource:
    resource_id: str
    kind: str
    order: int | None
    descriptor: dict[str, Any]
    enabled: bool
    purposes: tuple[str, ...]
    paired_video_resource_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.resource_id,
            "kind": self.kind,
            "role": "reference",
            "order": self.order,
            "path": str(self.descriptor.get("path") or ""),
            "content": dict(self.descriptor.get("content") or {}),
            "facts": {
                "reference_control": {
                    "enabled": self.enabled,
                    "purposes": list(self.purposes),
                }
            },
            "usage": "reference",
        }


@dataclass(frozen=True)
class _CompatPresentation:
    resource: _CompatResource
    prompt_tag: str
    paired_video_resource_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {"resource": self.resource.to_dict(), "prompt_tag": self.prompt_tag}
        if self.paired_video_resource_id:
            value["paired_video_resource_id"] = self.paired_video_resource_id
        return value


@dataclass(frozen=True)
class _CompatResolvedReferenceSet:
    resources: tuple[_CompatResource, ...]
    presentation: tuple[_CompatPresentation, ...]
    disabled_resources: tuple[_CompatResource, ...]
    diagnostics: tuple[_CompatDiagnostic, ...]

    @property
    def ready(self) -> bool:
        return not any(item.severity == "error" for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": "all",
            "ready": self.ready,
            "resources": [item.to_dict() for item in self.resources],
            "presentation": [item.to_dict() for item in self.presentation],
            "disabled_resources": [item.to_dict() for item in self.disabled_resources],
            "filtered_resources": [],
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "cost": None,
        }


def _h3_reference_contract(descriptor: Mapping[str, Any]) -> Mapping[str, Any] | None:
    extensions = descriptor.get("extensions")
    h3 = extensions.get("minimax_h3") if isinstance(extensions, Mapping) else None
    value = h3.get("reference") if isinstance(h3, Mapping) else None
    return value if isinstance(value, Mapping) else None


def _compat_reference_settings(
    descriptor: Mapping[str, Any], contract: Mapping[str, Any] | None
) -> tuple[bool, tuple[str, ...], str | None]:
    if contract is None:
        enabled = descriptor.get("enabled", True)
        purposes = descriptor.get("purposes", ["unknown"])
        video_id = descriptor.get("video_resource_id")
    else:
        if contract.get("contract_version") != 1:
            raise ValueError(
                f"H3 reference {descriptor.get('id')!r} has unsupported reference contract version"
            )
        enabled = contract.get("enabled", True)
        purposes = contract.get("purposes")
        binding = contract.get("binding")
        video_id = binding.get("video_resource_id") if isinstance(binding, Mapping) else None
    if not isinstance(enabled, bool):
        raise ValueError(f"H3 reference {descriptor.get('id')!r} enabled must be boolean")
    if (
        isinstance(purposes, (str, bytes)) or not isinstance(purposes, (list, tuple))
        or not purposes or not all(isinstance(item, str) and item for item in purposes)
    ):
        raise ValueError(
            f"H3 reference {descriptor.get('id')!r} purposes must be a non-empty string array"
        )
    if video_id is not None and (descriptor.get("kind") != "audio" or not str(video_id).strip()):
        raise ValueError(f"H3 reference {descriptor.get('id')!r} has an invalid video binding")
    return enabled, tuple(dict.fromkeys(purposes)), str(video_id) if video_id is not None else None


def _resolve_legacy_reference_set(packet: Any, refs: list[Any]) -> _CompatResolvedReferenceSet:
    kind_rank = {"image": 0, "video": 1, "audio": 2}
    resources: list[_CompatResource] = []
    disabled: list[_CompatResource] = []
    legacy_ids: list[str] = []
    for ref in refs:
        descriptor = dict(ref.descriptor)
        if not descriptor.get("id"):
            raise ValueError("H3 reference descriptor has no resource ID")
        if descriptor.get("kind") not in {"image", "video", "audio"}:
            raise ValueError(
                f"H3 reference {descriptor.get('id')!r} has unsupported kind "
                f"{descriptor.get('kind')!r}"
            )
        contract = _h3_reference_contract(descriptor)
        enabled, purposes, video_id = _compat_reference_settings(descriptor, contract)
        resource = _CompatResource(
            resource_id=str(descriptor.get("id") or ""),
            kind=str(descriptor.get("kind") or ""),
            order=descriptor.get("order") if isinstance(descriptor.get("order"), int) else None,
            descriptor=descriptor,
            enabled=enabled,
            purposes=purposes,
            paired_video_resource_id=video_id,
        )
        if contract is None:
            legacy_ids.append(resource.resource_id)
        (resources if enabled else disabled).append(resource)
    resources.sort(
        key=lambda item: (
            kind_rank.get(item.kind, 99), item.order is None,
            item.order or 0, item.resource_id,
        )
    )
    disabled.sort(
        key=lambda item: (
            kind_rank.get(item.kind, 99), item.order is None,
            item.order or 0, item.resource_id,
        )
    )
    diagnostics: list[_CompatDiagnostic] = [
        _CompatDiagnostic(
            "warning",
            "legacy_reference_contract_defaulted",
            "Legacy H3 reference descriptors had no reference contract; "
            "LongCaster treated them as enabled with purpose 'unknown'.",
            tuple(legacy_ids),
        )
    ]
    for label, kind, maximum in (
        ("image reference", "image", 9),
        ("video reference", "video", 3),
    ):
        selected = [item for item in resources if item.kind == kind]
        if len(selected) > maximum:
            diagnostics.append(_CompatDiagnostic(
                "error", "reference_limit_exceeded",
                f"Selected {len(selected)} {label} resources, but native H3 accepts at most {maximum}.",
                tuple(item.resource_id for item in selected),
            ))
    video_ids = {item.resource_id for item in resources if item.kind == "video"}
    paired_audio = {
        item.resource_id for item in resources
        if item.kind == "audio" and item.paired_video_resource_id in video_ids
    }
    standalone_audio = [
        item for item in resources if item.kind == "audio" and item.resource_id not in paired_audio
    ]
    if len(standalone_audio) > 3:
        diagnostics.append(_CompatDiagnostic(
            "error", "reference_limit_exceeded",
            f"Selected {len(standalone_audio)} standalone audio reference resources, "
            "but native H3 accepts at most 3.",
            tuple(item.resource_id for item in standalone_audio),
        ))
    presentation: list[_CompatPresentation] = []
    for index, item in enumerate((r for r in resources if r.kind == "image"), start=1):
        presentation.append(_CompatPresentation(item, f"<Picture {index}>"))
    audio_index = 0
    for index, video in enumerate((r for r in resources if r.kind == "video"), start=1):
        soundtracks = [
            audio for audio in resources
            if audio.kind == "audio" and audio.paired_video_resource_id == video.resource_id
        ]
        if len(soundtracks) > 1:
            diagnostics.append(_CompatDiagnostic(
                "error", "multiple_video_soundtracks",
                f"video reference order={video.order} has {len(soundtracks)} bound audio "
                "resources; native H3 accepts one soundtrack per reference video.",
                tuple(item.resource_id for item in soundtracks),
            ))
        for audio in soundtracks:
            audio_index += 1
            presentation.append(_CompatPresentation(audio, f"<Audio {audio_index}>", video.resource_id))
        presentation.append(_CompatPresentation(video, f"<Video {index}>"))
    for audio in standalone_audio:
        audio_index += 1
        presentation.append(_CompatPresentation(audio, f"<Audio {audio_index}>"))
    LOGGER.warning(
        "LongCaster applied legacy MMH3 reference compatibility resource_ids=%s",
        ",".join(legacy_ids),
    )
    return _CompatResolvedReferenceSet(
        tuple(resources), tuple(presentation), tuple(disabled), tuple(diagnostics)
    )


def _resolve_reference_set(packet: Any, **kwargs: Any) -> Any:
    refs = list(packet.filter_resource_refs(role="reference"))
    if any(_h3_reference_contract(ref.descriptor) is None for ref in refs):
        return _resolve_legacy_reference_set(packet, refs)
    return mmh3_api().resolve_reference_set(packet, **kwargs)


def reference_snapshot(packet: Any | None) -> dict[str, Any] | None:
    if packet is None:
        return None
    resolved = _resolve_reference_set(packet, preset="all")
    return {
        "packet_id": packet.manifest.get("id"),
        "packet_name": packet.manifest.get("name"),
        "ready": resolved.ready,
        "resources": [
            {
                "id": resource.resource_id,
                "kind": resource.kind,
                "content": resource.to_dict().get("content", {}),
            }
            for resource in resolved.resources
        ],
        "diagnostics": [item.to_dict() for item in resolved.diagnostics],
    }


@dataclass(frozen=True)
class NativeReferences:
    images: dict[str, Any]
    videos: dict[str, Any]
    video_audios: dict[str, Any]
    audios: dict[str, Any]
    report: dict[str, Any]


def materialize_native_references(
    packet: Any,
    *,
    width: int,
    height: int,
    target_frames: int,
    ref_image_size: str = "match",
) -> NativeReferences:
    api = mmh3_api()
    resolved = _resolve_reference_set(
        packet,
        preset="all",
        target_width=int(width),
        target_height=int(height),
        target_frames=int(target_frames),
        ref_image_size=ref_image_size,
    )
    if not resolved.ready:
        errors = [item.to_dict().get("message", str(item)) for item in resolved.diagnostics
                  if item.to_dict().get("severity") == "error"]
        raise ValueError("MMH3 reference set is not ready: " + "; ".join(errors))
    if not resolved.resources:
        raise ValueError("REF2VA mode requires at least one enabled MMH3 reference resource")

    paired_audio: dict[str, str] = {}
    paired_ids: set[str] = set()
    for item in resolved.presentation:
        if item.resource.kind == "audio" and item.paired_video_resource_id:
            paired_audio[item.paired_video_resource_id] = item.resource.resource_id
            paired_ids.add(item.resource.resource_id)

    images: dict[str, Any] = {}
    videos: dict[str, Any] = {}
    video_audios: dict[str, Any] = {}
    audios: dict[str, Any] = {}
    h3_references = None
    for resource in resolved.resources:
        raw = packet.get_by_id(resource.resource_id)
        if resource.kind == "image":
            images[f"ref_image_{len(images)}"] = api.get_resource_payload(packet, raw)
        elif resource.kind == "video":
            index = len(videos)
            if h3_references is None:
                h3_references = _h3_reference_module(api)
            materialized = h3_references.materialize_video_reference(
                packet,
                resource.resource_id,
                paired_audio_resource_id=paired_audio.get(resource.resource_id, ""),
                target_fps=24.0,
            )
            videos[f"ref_video_{index}"] = materialized.frames
            if materialized.audio is not None:
                video_audios[f"ref_video_audio_{index}"] = materialized.audio
        elif resource.kind == "audio" and resource.resource_id not in paired_ids:
            audios[f"ref_audio_{len(audios)}"] = api.get_resource_payload(packet, raw)

    return NativeReferences(images, videos, video_audios, audios, resolved.to_dict())


def pack_and_save_card(
    *,
    path: str | Path,
    latent: dict[str, Any],
    card_metadata: dict[str, Any],
    process_info: dict[str, Any],
    mode: str,
    name: str,
) -> tuple[Any, str]:
    api = mmh3_api()
    clean_latent = {key: value for key, value in latent.items() if key != "noise_mask"}
    packet = api.MMH3Media.create(
        name=name,
        generation={
            "task": mode,
            "prompt": card_metadata.get("prompt", ""),
            "seed": card_metadata.get("seed"),
            "frames": card_metadata.get("generated_frame_count"),
        },
    )
    packet = packet.set_extension_value("longcaster", "card", card_metadata)
    packed = api.pack_h3_result(
        packet,
        latent=clean_latent,
        operation="longcaster_card",
        mode=mode,
        status="draft",
        process_info=process_info,
        latent_origin="sampler_output",
    )
    saved, final_path = api.save_archive(packed.packet, str(Path(path).resolve()))
    # Reopen with full verification so a draft cannot enter the manifest unless
    # every embedded resource has passed its declared hash and size checks.
    verified = api.load_archive(final_path, verify="full")
    primary_latent(verified)
    return saved, final_path


def pack_and_save_refine_derivative(
    *,
    path: str | Path,
    latent: dict[str, Any],
    card_metadata: dict[str, Any],
    derivative_metadata: dict[str, Any],
    process_info: dict[str, Any],
    mode: str,
    name: str,
) -> tuple[Any, str]:
    """Persist a rebuildable refine derivative without touching its source master."""
    api = mmh3_api()
    clean_latent = {key: value for key, value in latent.items() if key != "noise_mask"}
    packet = api.MMH3Media.create(
        name=name,
        generation={
            "task": mode,
            "prompt": card_metadata.get("prompt", ""),
            "seed": derivative_metadata.get("seed"),
            "frames": card_metadata.get("generated_frame_count"),
        },
    )
    packet = packet.set_extension_value("longcaster", "card", card_metadata)
    packet = packet.set_extension_value("longcaster", "derivative", derivative_metadata)
    packed = api.pack_h3_result(
        packet,
        latent=clean_latent,
        operation=("longcaster_hires_derivative" if derivative_metadata.get("type") == "hires_refine"
                   else "longcaster_refine_derivative"),
        mode=mode,
        status="draft",
        process_info=process_info,
        latent_origin="sampler_output",
    )
    saved, final_path = api.save_archive(packed.packet, str(Path(path).resolve()))
    verified = api.load_archive(final_path, verify="full")
    primary_latent(verified)
    return saved, final_path
