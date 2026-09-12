from __future__ import annotations

from pathlib import Path
from typing import Any

import folder_paths
from aiohttp import web
from server import PromptServer

from .project import ProjectError, ProjectStore


def _projects_root() -> Path:
    return Path(folder_paths.get_output_directory()) / "longcaster_projects"


def _identity_state(
    store: ProjectStore,
    manifest: dict[str, Any],
    subject_id: str = "<Subject 1>",
    message: str = "Identity anchor state inspected.",
) -> dict[str, Any]:
    active = store.active_identity_anchor(manifest, subject_id, include_disabled=True)
    accepted = [
        {
            "card": card["artifact_number"],
            "card_id": card["id"],
            "preview_frames": card.get("actual_new_frame_count"),
            "max_frame_index": max(0, int(card.get("actual_new_frame_count") or 1) - 1),
            "duration_seconds": max(0, int(card.get("actual_new_frame_count") or 1) - 1) / 24.0,
            "preview_available": bool(card.get("preview", {}).get("asset_path")),
        }
        for card in manifest["cards"]
        if card["status"] == "ACCEPTED"
    ]
    return {
        "message": message,
        "project": manifest["project_name"],
        "subject_id": subject_id,
        "active_identity_anchor": active,
        "active_source": ({
            "card": active.get("source_card_artifact_number"),
            "card_id": active.get("source_card_id"),
            "preview_frame_index": active.get("source_preview_frame_index"),
            "preview_timestamp_seconds": active.get("source_preview_timestamp_seconds"),
            "label": active.get("label"),
            "enabled": active.get("enabled", True),
        } if active else None),
        "accepted_sources": accepted,
    }


def _card_summary(card: dict[str, Any]) -> dict[str, Any]:
    sections = card.get("prompt_sections") or {}
    summary = (sections.get("summary") or {}).get("text", "")
    title = next((line.strip() for line in summary.splitlines() if line.strip()), "")
    if not title and card.get("prompt_format") == "structured_v1":
        detail = (sections.get("detailed_description") or {}).get("text", "")
        title = next((line.strip() for line in detail.splitlines() if line.strip()), "")
    if not title and card.get("prompt_format") == "legacy_flat":
        title = next((line.strip() for line in card.get("prompt", "").splitlines() if line.strip()), "")
    anchors = card.get("anchors") or []
    return {
        "id": card["id"],
        "timeline_index": card["timeline_index"],
        "artifact_number": card["artifact_number"],
        "status": card["status"],
        "title": title[:100],
        "generation_parent_id": card.get("generation_parent_id"),
        "timeline_predecessor_id": card.get("timeline_predecessor_id"),
        "prompt_format": card.get("prompt_format"),
        "prompt_sections": card.get("prompt_sections"),
        "assembled_prompt": card.get("assembled_prompt", card.get("prompt", "")),
        "prompt_hash": card.get("prompt_hash"),
        "requested_duration_seconds": card.get("requested_duration_seconds"),
        "actual_duration_seconds": card.get("actual_duration_seconds"),
        "seed": card.get("seed"),
        "continuation_strategy": card.get("continuation_strategy"),
        "reference_set": card.get("reference_set"),
        "accepted_publication_id": card.get("accepted_publication_id"),
        "draft_inputs_dirty": card.get("draft_inputs_dirty", False),
        "preview_available": bool((card.get("preview") or {}).get("asset_path")),
        "current_state_anchor_id": next(
            (item.get("anchor_id") for item in reversed(anchors) if item.get("role") == "current_state"),
            None,
        ),
        "identity_anchor_ids": [
            item.get("anchor_id") for item in anchors if item.get("role") == "identity"
        ],
        "created_at": card.get("created_at"),
        "updated_at": card.get("updated_at"),
        "accepted_at": card.get("accepted_at"),
        "last_error": card.get("last_error"),
    }


def _cards_state(store: ProjectStore, manifest: dict[str, Any], message: str = "Project loaded.") -> dict[str, Any]:
    identity = store.active_identity_anchor(manifest, include_disabled=True)
    pending = manifest.get("pending_operation")
    return {
        "message": message,
        "project": manifest["project_name"],
        "revision": manifest["revision"],
        "generation_mode": manifest["generation_mode"],
        "width": manifest["width"],
        "height": manifest["height"],
        "active_card_id": manifest["active_card_id"],
        "pending_operation": ({
            key: pending.get(key) for key in ("id", "kind", "status", "card_id", "started_at")
        } if pending else None),
        "active_identity_anchor": identity,
        "cards": [_card_summary(card) for card in manifest["cards"]],
    }


def _error_response(exc: Exception, *, not_found: bool = False) -> web.Response:
    message = str(exc)
    status = 409 if message.startswith("stale project revision:") else 404 if not_found else 400
    return web.json_response({"error": message}, status=status)


async def list_cards_projects(_request: web.Request) -> web.Response:
    root = _projects_root()
    projects = []
    if root.is_dir():
        for manifest_path in sorted(root.glob("*/project.json"), key=lambda item: item.parent.name.lower()):
            try:
                store = ProjectStore(root, manifest_path.parent.name)
                manifest = store.load()
                projects.append({
                    "project": manifest["project_name"],
                    "revision": manifest["revision"],
                    "generation_mode": manifest["generation_mode"],
                    "card_count": len(manifest["cards"]),
                    "active_card_id": manifest["active_card_id"],
                })
            except (OSError, ValueError, ProjectError):
                continue
    return web.json_response({"projects": projects})


async def get_cards_state(request: web.Request) -> web.Response:
    try:
        store = ProjectStore(_projects_root(), request.query.get("project", ""))
        return web.json_response(_cards_state(store, store.load()))
    except (OSError, ValueError, ProjectError) as exc:
        return _error_response(exc, not_found=True)


async def update_cards_card(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        sections = payload.get("sections") or {}
        if not isinstance(sections, dict):
            raise ProjectError("sections must be an object")
        clears = payload.get("clear_sections") or []
        if not isinstance(clears, list):
            raise ProjectError("clear_sections must be a list")
        store = ProjectStore(_projects_root(), str(payload.get("project", "")))
        manifest = store.update_card_editor(
            card_id=str(payload.get("card_id", "")),
            expected_revision=int(payload.get("revision", -1)),
            section_changes=sections,
            clear_sections=[str(item) for item in clears],
            duration_seconds=payload.get("duration_seconds"),
            seed=payload.get("seed"),
            convert_legacy=bool(payload.get("convert_legacy", False)),
        )
        return web.json_response(_cards_state(store, manifest, "Card saved."))
    except (OSError, TypeError, ValueError, ProjectError) as exc:
        return _error_response(exc)


async def copy_cards_sections(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        names = payload.get("sections")
        if not isinstance(names, list) or not names:
            raise ProjectError("sections must be a non-empty list")
        store = ProjectStore(_projects_root(), str(payload.get("project", "")))
        manifest = store.copy_card_sections(
            target_card_id=str(payload.get("target_card_id", "")),
            source_card_id=str(payload.get("source_card_id", "")),
            names=[str(item) for item in names],
            expected_revision=int(payload.get("revision", -1)),
        )
        return web.json_response(_cards_state(store, manifest, "Prompt section copied."))
    except (OSError, TypeError, ValueError, ProjectError) as exc:
        return _error_response(exc)


async def get_cards_preview(request: web.Request) -> web.StreamResponse:
    try:
        store = ProjectStore(_projects_root(), request.query.get("project", ""))
        manifest = store.load()
        card = store.card(manifest, request.query.get("card", ""))
        preview = card.get("preview") or {}
        asset_path = preview.get("asset_path")
        if not asset_path:
            raise ProjectError("this card has no registered project preview")
        path = store.absolute_path(asset_path)
        if not path.is_file():
            raise ProjectError("the registered project preview is missing")
        return web.FileResponse(path)
    except (OSError, ValueError, ProjectError) as exc:
        return _error_response(exc, not_found=True)


async def list_identity_projects(_request: web.Request) -> web.Response:
    root = _projects_root()
    projects = []
    if root.is_dir():
        for manifest_path in sorted(root.glob("*/project.json"), key=lambda item: item.parent.name.lower()):
            try:
                store = ProjectStore(root, manifest_path.parent.name)
                manifest = store.load()
                projects.append(_identity_state(store, manifest))
            except (OSError, ValueError, ProjectError):
                continue
    return web.json_response({"projects": projects})


async def get_identity_state(request: web.Request) -> web.Response:
    try:
        project_name = request.query.get("project", "")
        subject_id = request.query.get("subject_id", "<Subject 1>")
        store = ProjectStore(_projects_root(), project_name)
        return web.json_response(_identity_state(store, store.load(), subject_id))
    except (OSError, ValueError, ProjectError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def get_identity_preview(request: web.Request) -> web.StreamResponse:
    try:
        project_name = request.query.get("project", "")
        card_selector = request.query.get("card", "")
        store = ProjectStore(_projects_root(), project_name)
        manifest = store.load()
        card = store.accepted_card(manifest, card_selector)
        preview = card.get("preview") or {}
        asset_path = preview.get("asset_path")
        if not asset_path:
            raise ProjectError("this accepted card has no registered project preview")
        path = store.absolute_path(asset_path)
        if not path.is_file():
            raise ProjectError("the registered project preview is missing")
        return web.FileResponse(path)
    except (OSError, ValueError, ProjectError) as exc:
        return web.json_response({"error": str(exc)}, status=404)


async def control_identity_anchor(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        project_name = str(payload.get("project", ""))
        subject_id = str(payload.get("subject_id", "<Subject 1>")).strip()
        action = str(payload.get("action", ""))
        store = ProjectStore(_projects_root(), project_name)
        if action == "enable":
            manifest = store.set_identity_anchor_enabled(subject_id, True)
            message = f"Identity anchor enabled for {subject_id}."
        elif action == "disable":
            manifest = store.set_identity_anchor_enabled(subject_id, False)
            message = f"Identity anchor disabled for {subject_id}."
        elif action == "clear":
            manifest = store.clear_identity_anchor(subject_id)
            message = f"Active identity anchor cleared for {subject_id}; its historical asset was retained."
        else:
            raise ProjectError(f"unsupported identity control action: {action!r}")
        return web.json_response(_identity_state(store, manifest, subject_id, message))
    except (OSError, ValueError, ProjectError) as exc:
        return web.json_response({"error": str(exc)}, status=400)


_REGISTERED = False


def register_routes() -> bool:
    """Register routes once PromptServer has created its singleton instance."""
    global _REGISTERED
    if _REGISTERED:
        return True
    instance = getattr(PromptServer, "instance", None)
    if instance is None:
        return False
    instance.routes.get("/longcaster/identity/projects")(list_identity_projects)
    instance.routes.get("/longcaster/identity/state")(get_identity_state)
    instance.routes.get("/longcaster/identity/preview")(get_identity_preview)
    instance.routes.post("/longcaster/identity/control")(control_identity_anchor)
    instance.routes.get("/longcaster/cards/projects")(list_cards_projects)
    instance.routes.get("/longcaster/cards/state")(get_cards_state)
    instance.routes.patch("/longcaster/cards/card")(update_cards_card)
    instance.routes.post("/longcaster/cards/copy")(copy_cards_sections)
    instance.routes.get("/longcaster/cards/preview")(get_cards_preview)
    _REGISTERED = True
    return True
