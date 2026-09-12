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
    current_state = next(
        (item for item in reversed(anchors) if item.get("role") == "current_state"), None
    )
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
        "current_state_anchor_id": current_state.get("anchor_id") if current_state else None,
        "current_state_anchor": current_state,
        "identity_anchor_ids": [
            item.get("anchor_id") for item in anchors if item.get("role") == "identity"
        ],
        "created_at": card.get("created_at"),
        "updated_at": card.get("updated_at"),
        "accepted_at": card.get("accepted_at"),
        "last_error": card.get("last_error"),
    }


def _identity_anchors(store: ProjectStore, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = manifest.get("active_identity_anchors", {})
    active_by_anchor: dict[str, list[str]] = {}
    for subject_id, anchor_id in bindings.items():
        active_by_anchor.setdefault(anchor_id, []).append(subject_id)
    identities = []
    for card in manifest["cards"]:
        for anchor in card.get("anchors", []):
            if anchor.get("role") != "identity":
                continue
            identities.append({
                "anchor_id": anchor["anchor_id"],
                "subject_id": anchor.get("subject_id"),
                "label": anchor.get("label") or "Identity checkpoint",
                "enabled": anchor.get("enabled", True),
                "identity_scope": anchor.get("identity_scope", "face_only"),
                "custom_identity_instruction": anchor.get("custom_identity_instruction"),
                "source_card_id": card["id"],
                "source_card_number": card["timeline_index"] + 1,
                "source_artifact_number": card["artifact_number"],
                "source_preview_frame_index": anchor.get("source_preview_frame_index"),
                "source_timestamp_seconds": anchor.get("source_preview_timestamp_seconds"),
                "active_for": active_by_anchor.get(anchor["anchor_id"], []),
                "asset_available": bool(
                    anchor.get("asset_path")
                    and store.absolute_path(anchor["asset_path"]).is_file()
                ),
            })
    return identities


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
        "active_identity_anchors": manifest.get("active_identity_anchors", {}),
        "identity_anchors": _identity_anchors(store, manifest),
        "generation_mode_editable": (
            len(manifest["cards"]) == 1
            and manifest["cards"][0]["status"] in {"EMPTY", "FAILED"}
            and not manifest["cards"][0].get("artifact_sha256")
            and not manifest.get("pending_operation")
        ),
        "project_folder": f"output/longcaster_projects/{manifest['project_name']}",
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
                    "folder": f"output/longcaster_projects/{manifest['project_name']}",
                })
            except (OSError, ValueError, ProjectError):
                continue
    return web.json_response({"projects": projects})


async def create_cards_project(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        store = ProjectStore(_projects_root(), str(payload.get("project", "")))
        if store.manifest_path.exists():
            raise ProjectError("a project with this name already exists")
        manifest = store.create(
            prompt="",
            duration_seconds=float(payload.get("duration_seconds", 5.0)),
            seed=int(payload.get("seed", 0)),
            width=int(payload.get("width", 544)),
            height=int(payload.get("height", 960)),
            generation_mode=str(payload.get("generation_mode", "ref2va")),
        )
        return web.json_response(_cards_state(store, manifest, "Project created."), status=201)
    except (OSError, TypeError, ValueError, ProjectError) as exc:
        return _error_response(exc)


async def update_cards_project(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        store = ProjectStore(_projects_root(), str(payload.get("project", "")))
        manifest = store.update_project_settings(
            expected_revision=int(payload.get("revision", -1)),
            generation_mode=str(payload.get("generation_mode", "")),
        )
        return web.json_response(_cards_state(store, manifest, "Project settings saved."))
    except (OSError, TypeError, ValueError, ProjectError) as exc:
        return _error_response(exc)


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
            continuation_strategy=payload.get("continuation_strategy"),
            convert_legacy=bool(payload.get("convert_legacy", False)),
            import_prompt=payload.get("import_prompt"),
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


async def get_anchor_asset(request: web.Request) -> web.StreamResponse:
    try:
        store = ProjectStore(_projects_root(), request.query.get("project", ""))
        manifest = store.load()
        anchor_id = request.query.get("anchor", "")
        anchor = next(
            (
                item
                for card in manifest["cards"]
                for item in card.get("anchors", [])
                if item.get("anchor_id") == anchor_id
            ),
            None,
        )
        if anchor is None:
            raise ProjectError("anchor does not exist")
        path = store.absolute_path(anchor["asset_path"])
        if not path.is_file():
            raise ProjectError("anchor image is missing")
        return web.FileResponse(path)
    except (OSError, ValueError, ProjectError) as exc:
        return _error_response(exc, not_found=True)


async def manage_identity_anchor(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
        store = ProjectStore(_projects_root(), str(payload.get("project", "")))
        subject_id = str(payload.get("subject_id", "<Subject 1>")).strip()
        action = str(payload.get("action", ""))
        if action == "bind":
            manifest = store.bind_identity_anchor(
                subject_id=subject_id,
                anchor_id=str(payload.get("anchor_id", "")),
                expected_revision=int(payload.get("revision", -1)),
            )
            message = f"Identity checkpoint selected for {subject_id}."
        elif action == "enable":
            manifest = store.set_identity_anchor_enabled(subject_id, True)
            message = f"Identity checkpoint enabled for {subject_id}."
        elif action == "disable":
            manifest = store.set_identity_anchor_enabled(subject_id, False)
            message = f"Identity checkpoint disabled for {subject_id}."
        elif action == "clear":
            manifest = store.clear_identity_anchor(subject_id)
            message = f"Active identity checkpoint cleared for {subject_id}."
        else:
            raise ProjectError(f"unsupported identity action: {action!r}")
        return web.json_response(_cards_state(store, manifest, message))
    except (OSError, TypeError, ValueError, ProjectError) as exc:
        return _error_response(exc)


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
    instance.routes.post("/longcaster/cards/projects")(create_cards_project)
    instance.routes.get("/longcaster/cards/state")(get_cards_state)
    instance.routes.patch("/longcaster/cards/project")(update_cards_project)
    instance.routes.patch("/longcaster/cards/card")(update_cards_card)
    instance.routes.post("/longcaster/cards/copy")(copy_cards_sections)
    instance.routes.get("/longcaster/cards/preview")(get_cards_preview)
    instance.routes.get("/longcaster/cards/anchor")(get_anchor_asset)
    instance.routes.post("/longcaster/cards/identity")(manage_identity_anchor)
    _REGISTERED = True
    return True
