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
    _REGISTERED = True
    return True
