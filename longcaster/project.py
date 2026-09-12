from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import uuid
from typing import Any, Iterator

from .prompt_sections import (
    assemble_prompt,
    copy_sections,
    edit_sections,
    empty_prompt_sections,
    hash_prompt,
    imported_prompt_sections,
    inherited_prompt_sections,
    prompt_fields,
    section_record,
    validate_prompt_sections,
)


SCHEMA_VERSION = 7
VALID_STATES = {"EMPTY", "DRAFT", "ACCEPTED", "FAILED"}
IDENTITY_SCOPES = {"face_only", "face_clothing", "face_body", "everything", "custom"}
GENERATION_MODES = {"ref2va", "t2va"}
CONTINUATION_STRATEGIES = {"independent", "direct_mmh3"}
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_RUNTIME_ID = str(uuid.uuid4())


class ProjectError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_project_name(name: str) -> str:
    if not isinstance(name, str) or not _PROJECT_RE.fullmatch(name):
        raise ProjectError(
            "project_name must contain 1-64 letters, digits, dots, underscores, or hyphens"
        )
    if name in {".", ".."} or name.upper().split(".", 1)[0] in _WINDOWS_RESERVED:
        raise ProjectError(f"project_name is reserved: {name!r}")
    return name


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _os_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve()).casefold()
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(key, threading.RLock())
    with local_lock:
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _new_card(
    *,
    timeline_index: int,
    artifact_number: int,
    parent_id: str | None,
    timeline_predecessor_id: str | None,
    prompt: str,
    duration_seconds: float,
    seed: int,
) -> dict[str, Any]:
    now = utc_now()
    structured = prompt_fields(prompt)
    if prompt == "":
        sections = empty_prompt_sections()
        assembled = assemble_prompt(sections)
        structured = {
            "prompt_sections": sections,
            "prompt_format": "structured_v1",
            "assembled_prompt": assembled,
            "prompt_hash": hash_prompt(assembled),
        }
    return {
        "id": str(uuid.uuid4()),
        "timeline_index": timeline_index,
        "artifact_number": artifact_number,
        "generation_parent_id": parent_id,
        "timeline_predecessor_id": timeline_predecessor_id,
        "status": "EMPTY",
        "prompt": structured["assembled_prompt"],
        **structured,
        "requested_duration_seconds": float(duration_seconds),
        "seed": int(seed),
        "continuation_strategy": "direct_mmh3" if parent_id else "independent",
        "reference_set": None,
        "accepted_publication_id": None,
        "draft_inputs_dirty": False,
        "attempt": 0,
        "draft_path": None,
        "master_path": None,
        "artifact_sha256": None,
        "generation_fingerprint": None,
        "recipe": None,
        "context_frame_count": 0,
        "generated_frame_count": None,
        "actual_new_frame_count": None,
        "actual_duration_seconds": None,
        "created_at": now,
        "updated_at": now,
        "accepted_at": None,
        "last_error": None,
        "anchors": [],
        "publication_history": [],
    }


class ProjectStore:
    def __init__(self, projects_root: str | Path, project_name: str):
        self.projects_root = Path(projects_root).resolve()
        self.project_name = _validate_project_name(project_name)
        self.path = (self.projects_root / self.project_name).resolve()
        try:
            self.path.relative_to(self.projects_root)
        except ValueError as exc:
            raise ProjectError("project path escapes the projects root") from exc
        self.manifest_path = self.path / "project.json"
        self.lock_path = self.path / ".project.lock"

    @contextmanager
    def locked(self) -> Iterator[None]:
        with _os_file_lock(self.lock_path):
            yield

    def create(
        self,
        *,
        prompt: str,
        duration_seconds: float,
        seed: int,
        width: int,
        height: int,
        generation_mode: str,
    ) -> dict[str, Any]:
        if generation_mode not in GENERATION_MODES:
            raise ProjectError("generation_mode must be ref2va or t2va")
        width = int(width)
        height = int(height)
        if not 32 <= width <= 8192 or not 32 <= height <= 8192:
            raise ProjectError("project width and height must be between 32 and 8192")
        if width % 32 or height % 32:
            raise ProjectError("project width and height must be multiples of 32")
        with self.locked():
            if self.manifest_path.exists():
                return self._load_unlocked()
            self.path.mkdir(parents=True, exist_ok=True)
            for directory in ("clips", "drafts", "transactions", "previews", "anchors"):
                (self.path / directory).mkdir(exist_ok=True)
            first = _new_card(
                timeline_index=0,
                artifact_number=1,
                parent_id=None,
                timeline_predecessor_id=None,
                prompt=prompt,
                duration_seconds=duration_seconds,
                seed=seed,
            )
            now = utc_now()
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "project_name": self.project_name,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "generation_mode": generation_mode,
                "width": width,
                "height": height,
                "active_card_id": first["id"],
                "cards": [first],
                "active_identity_anchors": {},
                "pending_operation": None,
                "last_operation": None,
            }
            _atomic_json(self.manifest_path, manifest)
            return deepcopy(manifest)

    def load(self, *, reconcile: bool = True) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            changed = self._recover_accept_transactions_unlocked(manifest)
            pending = manifest.get("pending_operation")
            if (
                reconcile
                and pending
                and pending.get("status") == "running"
                and pending.get("owner_runtime_id") != _RUNTIME_ID
            ):
                card = self._card(manifest, pending.get("card_id"))
                card["last_error"] = "Generation was interrupted before its draft was committed."
                if pending.get("previous_card_status") in {"EMPTY", "FAILED"}:
                    card["status"] = "FAILED"
                card["updated_at"] = utc_now()
                operation_record = {key: value for key, value in pending.items() if key != "candidate"}
                manifest["last_operation"] = {**operation_record, "status": "interrupted", "ended_at": utc_now()}
                manifest["pending_operation"] = None
                changed = True
            if changed:
                self._commit_unlocked(manifest)
            self._validate(manifest)
            return deepcopy(manifest)

    def begin_generation(
        self,
        *,
        action: str,
        prompt: str,
        duration_seconds: float,
        seed: int,
        recipe: dict[str, Any],
        fingerprint: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if action not in {"generate", "retry"}:
            raise ProjectError(f"unsupported generation action: {action}")
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError(
                    "another project operation is already pending; use Stop Render / Unlock"
                )
            card = self._active_card(manifest)
            if card.get("prompt_format") == "structured_v1":
                prompt = card["assembled_prompt"]
            expected = {"EMPTY", "FAILED"} if action == "generate" else {"DRAFT"}
            if card["status"] not in expected:
                label = "EMPTY or FAILED" if action == "generate" else "DRAFT"
                raise ProjectError(f"{action} requires a {label} card; current state is {card['status']}")
            operation_id = str(uuid.uuid4())
            operation = {
                "id": operation_id,
                "kind": action,
                "status": "running",
                "card_id": card["id"],
                "started_at": utc_now(),
                "owner_runtime_id": _RUNTIME_ID,
                "owner_pid": os.getpid(),
                "previous_card_status": card["status"],
                "candidate": {
                    "prompt": prompt,
                    "requested_duration_seconds": float(duration_seconds),
                    "seed": int(seed),
                    "recipe": deepcopy(recipe),
                    "generation_fingerprint": fingerprint,
                },
            }
            manifest["pending_operation"] = operation
            self._commit_unlocked(manifest)
            return deepcopy(manifest), deepcopy(card)

    def finish_generation(
        self,
        *,
        operation_id: str,
        draft_path: str | Path,
        artifact_sha256: str,
        context_frame_count: int,
        generated_frame_count: int,
        actual_new_frame_count: int,
        actual_duration_seconds: float,
    ) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            pending = manifest.get("pending_operation")
            if not pending or pending.get("id") != operation_id:
                raise ProjectError("generation result is stale or no longer pending")
            card = self._card(manifest, pending["card_id"])
            relative = self.relative_path(draft_path)
            absolute = self.absolute_path(relative)
            if not absolute.is_file():
                raise ProjectError(f"draft archive was not written: {relative}")
            observed_hash = sha256_file(absolute)
            if observed_hash != artifact_sha256:
                raise ProjectError("draft archive hash changed before commit")
            old_draft = card.get("draft_path")
            candidate = pending.get("candidate", {})
            candidate_prompt = candidate.get("prompt", card["prompt"])
            if card.get("prompt_format") == "legacy_flat":
                card["assembled_prompt"] = candidate_prompt
                card["prompt_hash"] = hash_prompt(candidate_prompt)
            card.update({
                "prompt": candidate_prompt,
                "requested_duration_seconds": candidate.get(
                    "requested_duration_seconds", card["requested_duration_seconds"]
                ),
                "seed": candidate.get("seed", card["seed"]),
                "recipe": deepcopy(candidate.get("recipe", card.get("recipe"))),
                "generation_fingerprint": candidate.get(
                    "generation_fingerprint", card.get("generation_fingerprint")
                ),
                "reference_set": deepcopy(
                    (candidate.get("recipe") or {}).get("references", card.get("reference_set"))
                ),
                "status": "DRAFT",
                "attempt": int(card.get("attempt", 0)) + 1,
                "draft_path": relative,
                "master_path": None,
                "artifact_sha256": artifact_sha256,
                "context_frame_count": int(context_frame_count),
                "generated_frame_count": int(generated_frame_count),
                "actual_new_frame_count": int(actual_new_frame_count),
                "actual_duration_seconds": float(actual_duration_seconds),
                "preview": None,
                "updated_at": utc_now(),
                "last_error": None,
                "draft_inputs_dirty": False,
            })
            operation_record = {key: value for key, value in pending.items() if key != "candidate"}
            manifest["last_operation"] = {**operation_record, "status": "complete", "ended_at": utc_now()}
            manifest["pending_operation"] = None
            self._commit_unlocked(manifest)
            if old_draft and old_draft != relative:
                try:
                    self.absolute_path(old_draft).unlink(missing_ok=True)
                except OSError:
                    pass
            return deepcopy(manifest)

    def fail_generation(self, operation_id: str, error: str) -> None:
        with self.locked():
            manifest = self._load_unlocked()
            pending = manifest.get("pending_operation")
            if not pending or pending.get("id") != operation_id:
                return
            card = self._card(manifest, pending["card_id"])
            card["last_error"] = str(error)[:4000]
            if pending.get("previous_card_status") in {"EMPTY", "FAILED"}:
                card["status"] = "FAILED"
            card["updated_at"] = utc_now()
            operation_record = {key: value for key, value in pending.items() if key != "candidate"}
            manifest["last_operation"] = {
                **operation_record, "status": "failed", "error": str(error)[:4000], "ended_at": utc_now()
            }
            manifest["pending_operation"] = None
            self._commit_unlocked(manifest)

    def cancel_pending(self, reason: str = "Generation cancelled by user.") -> tuple[dict[str, Any], bool]:
        """Clear the in-flight marker without changing the active card artifact.

        The sampler may still unwind after an interrupt. Its eventual completion is
        rejected by finish_generation because the operation is no longer pending.
        """
        with self.locked():
            manifest = self._load_unlocked()
            pending = manifest.get("pending_operation")
            if not pending:
                return deepcopy(manifest), False
            card = self._card(manifest, pending["card_id"])
            card["last_error"] = str(reason)[:4000]
            card["updated_at"] = utc_now()
            operation_record = {key: value for key, value in pending.items() if key != "candidate"}
            manifest["last_operation"] = {
                **operation_record,
                "status": "cancelled",
                "reason": str(reason)[:4000],
                "ended_at": utc_now(),
            }
            manifest["pending_operation"] = None
            self._commit_unlocked(manifest)
            return deepcopy(manifest), True

    def accept(self, *, anchor: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot accept while another operation is pending")
            card = self._active_card(manifest)
            if card["status"] != "DRAFT":
                raise ProjectError(f"accept requires a DRAFT card; current state is {card['status']}")
            if card.get("draft_inputs_dirty"):
                raise ProjectError("card inputs changed after generation; Retry Draft before accepting")
            source = self.absolute_path(card["draft_path"])
            if not source.is_file() or sha256_file(source) != card["artifact_sha256"]:
                raise ProjectError("draft archive is missing or does not match its recorded hash")
            relative_destination = f"clips/card_{card['artifact_number']:04d}.mmh3"
            destination = self.absolute_path(relative_destination)
            if destination.exists():
                raise ProjectError(f"accepted master already exists and will not be overwritten: {relative_destination}")
            if anchor is not None:
                self._validate_anchor_asset(card, anchor)

            transaction = {
                "schema_version": 1,
                "kind": "accept",
                "card_id": card["id"],
                "source": card["draft_path"],
                "destination": relative_destination,
                "sha256": card["artifact_sha256"],
                "publication_id": str(uuid.uuid4()),
                "anchor": deepcopy(anchor),
                "created_at": utc_now(),
            }
            journal = self.path / "transactions" / f"accept_{card['id']}.json"
            _atomic_json(journal, transaction)
            temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
                if sha256_file(temporary) != card["artifact_sha256"]:
                    raise ProjectError("accepted master copy failed its hash verification")
                if destination.exists():
                    raise ProjectError("accepted master appeared during publication; refusing overwrite")
                os.replace(temporary, destination)
                self._finish_accept_unlocked(manifest, transaction)
                self._commit_unlocked(manifest)
                journal.unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)
            return deepcopy(manifest)

    def unpublish_tail(self) -> dict[str, Any]:
        """Reopen the latest accepted card while retaining its immutable master.

        The published archive and its derived assets become a version-history
        record. A verified copy is installed as the card's retryable draft, and
        a fresh artifact number is reserved for the next acceptance.
        """
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot unpublish while another operation is pending")
            card = self._active_card(manifest)
            if card["status"] != "ACCEPTED":
                raise ProjectError(
                    f"unpublish requires an ACCEPTED card; current state is {card['status']}"
                )
            if card is not manifest["cards"][-1] or any(
                item.get("generation_parent_id") == card["id"] for item in manifest["cards"]
            ):
                raise ProjectError("only the latest accepted card with no descendants can be unpublished")

            master_relative = card.get("master_path")
            master = self.absolute_path(master_relative)
            artifact_hash = card.get("artifact_sha256")
            if not master.is_file() or sha256_file(master) != artifact_hash:
                raise ProjectError("accepted master is missing or does not match its recorded hash")

            draft = self.draft_destination(card)
            draft.parent.mkdir(parents=True, exist_ok=True)
            temporary = draft.with_name(f".{draft.name}.{uuid.uuid4().hex}.tmp")
            try:
                with master.open("rb") as source_handle, temporary.open("xb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                    target_handle.flush()
                    os.fsync(target_handle.fileno())
                if sha256_file(temporary) != artifact_hash:
                    raise ProjectError("unpublished draft copy failed its hash verification")
                os.replace(temporary, draft)
            finally:
                temporary.unlink(missing_ok=True)

            old_artifact_number = int(card["artifact_number"])
            old_anchors = deepcopy(card.get("anchors", []))
            history = card.setdefault("publication_history", [])
            history.append({
                "publication_id": card.get("accepted_publication_id") or str(uuid.uuid4()),
                "artifact_number": old_artifact_number,
                "master_path": master_relative,
                "artifact_sha256": artifact_hash,
                "accepted_at": card.get("accepted_at"),
                "invalidated_at": utc_now(),
                "anchors": old_anchors,
                "preview": deepcopy(card.get("preview")),
                "generation_parent_id": card.get("generation_parent_id"),
                "timeline_predecessor_id": card.get("timeline_predecessor_id"),
                "prompt": card.get("prompt"),
                "prompt_format": card.get("prompt_format"),
                "prompt_sections": deepcopy(card.get("prompt_sections")),
                "assembled_prompt": card.get("assembled_prompt"),
                "prompt_hash": card.get("prompt_hash"),
                "seed": card.get("seed"),
                "requested_duration_seconds": card.get("requested_duration_seconds"),
                "actual_duration_seconds": card.get("actual_duration_seconds"),
                "continuation_strategy": card.get("continuation_strategy"),
                "reference_set": deepcopy(card.get("reference_set")),
                "generation_fingerprint": card.get("generation_fingerprint"),
                "recipe": deepcopy(card.get("recipe")),
            })
            archived_anchor_ids = {
                item.get("anchor_id") for item in old_anchors if item.get("anchor_id")
            }
            bindings = manifest.setdefault("active_identity_anchors", {})
            for subject_id, anchor_id in list(bindings.items()):
                if anchor_id in archived_anchor_ids:
                    del bindings[subject_id]

            all_numbers = [int(item["artifact_number"]) for item in manifest["cards"]]
            for item in manifest["cards"]:
                all_numbers.extend(
                    int(publication["artifact_number"])
                    for publication in item.get("publication_history", [])
                )
            card.update({
                "artifact_number": max(all_numbers) + 1,
                "status": "DRAFT",
                "draft_path": self.relative_path(draft),
                "master_path": None,
                "accepted_at": None,
                "accepted_publication_id": None,
                "anchors": [],
                "preview": None,
                "updated_at": utc_now(),
                "last_error": None,
                "draft_inputs_dirty": False,
            })
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "unpublish_tail",
                "status": "complete",
                "card_id": card["id"],
                "invalidated_artifact_number": old_artifact_number,
                "replacement_artifact_number": card["artifact_number"],
                "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def remove_draft_tail(self) -> dict[str, Any]:
        """Discard the active unaccepted tail and return to its accepted predecessor."""
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot remove a draft card while another operation is pending")
            card = self._active_card(manifest)
            if card is not manifest["cards"][-1]:
                raise ProjectError("only the active last card can be removed")
            if card["status"] not in {"EMPTY", "DRAFT", "FAILED"}:
                raise ProjectError(
                    f"remove draft requires an unaccepted card; current state is {card['status']}"
                )
            if card.get("publication_history"):
                raise ProjectError("a previously published card cannot be removed; retry or accept it instead")
            if len(manifest["cards"]) < 2:
                raise ProjectError("the first card cannot be removed because there is no previous accepted card")
            predecessor = manifest["cards"][-2]
            if (
                card.get("timeline_predecessor_id") != predecessor["id"]
                or predecessor["status"] != "ACCEPTED"
            ):
                raise ProjectError("the removable draft must follow an accepted card")

            removed_card_id = card["id"]
            disposable_files: list[Path] = []
            for relative in (
                card.get("draft_path"),
                (card.get("preview") or {}).get("asset_path"),
            ):
                if relative:
                    disposable_files.append(self.absolute_path(relative))

            manifest["cards"].pop()
            manifest["active_card_id"] = predecessor["id"]
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "remove_draft_tail",
                "status": "complete",
                "card_id": removed_card_id,
                "restored_card_id": predecessor["id"],
                "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)

            for path in disposable_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            for relative_directory in (
                f"previews/{removed_card_id}",
                f"anchors/{removed_card_id}",
                f"anchor_candidates/{removed_card_id}",
            ):
                try:
                    shutil.rmtree(self.absolute_path(relative_directory), ignore_errors=True)
                except OSError:
                    pass
            return deepcopy(manifest)

    def add_anchor(self, card_id: str, anchor: dict[str, Any]) -> dict[str, Any]:
        """Attach a newly materialized anchor to an existing accepted card."""
        with self.locked():
            manifest = self._load_unlocked()
            card = self._card(manifest, card_id)
            if card["status"] != "ACCEPTED":
                raise ProjectError("state anchors can only be attached to accepted cards")
            self._validate_anchor_asset(card, anchor)
            anchors = card.setdefault("anchors", [])
            if any(item.get("anchor_id") == anchor.get("anchor_id") for item in anchors):
                return deepcopy(manifest)
            anchors.append(deepcopy(anchor))
            card["updated_at"] = utc_now()
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "add_anchor",
                "status": "complete",
                "card_id": card_id,
                "anchor_id": anchor["anchor_id"],
                "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def accepted_card(self, manifest: dict[str, Any], selector: str | int) -> dict[str, Any]:
        """Resolve an accepted card by stable UUID or user-facing artifact number."""
        value = str(selector).strip()
        matches = [
            card for card in manifest["cards"]
            if card["status"] == "ACCEPTED"
            and (card["id"] == value or str(card["artifact_number"]) == value)
        ]
        if not matches:
            raise ProjectError(f"accepted source card does not exist: {selector!r}")
        return deepcopy(matches[0])

    def add_identity_anchor(self, card_id: str, anchor: dict[str, Any]) -> dict[str, Any]:
        """Persist an identity checkpoint and make it active for its subject."""
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot set an identity anchor while generation is pending")
            card = self._card(manifest, card_id)
            if card["status"] != "ACCEPTED":
                raise ProjectError("identity anchors require an accepted source card")
            if anchor.get("role") != "identity":
                raise ProjectError("manual identity anchors must use role=identity")
            stored_anchor = deepcopy(anchor)
            stored_anchor.setdefault("identity_scope", "face_only")
            stored_anchor.setdefault("custom_identity_instruction", None)
            self._validate_anchor_asset(card, stored_anchor)
            anchors = card.setdefault("anchors", [])
            if any(item.get("anchor_id") == stored_anchor.get("anchor_id") for item in anchors):
                raise ProjectError("identity anchor is already present")
            anchors.append(stored_anchor)
            subject_id = stored_anchor["subject_id"]
            manifest.setdefault("active_identity_anchors", {})[subject_id] = stored_anchor["anchor_id"]
            card["updated_at"] = utc_now()
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()), "kind": "set_identity_anchor", "status": "complete",
                "card_id": card_id, "anchor_id": stored_anchor["anchor_id"],
                "subject_id": subject_id, "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def set_identity_anchor_enabled(self, subject_id: str, enabled: bool) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot change an identity anchor while generation is pending")
            anchor = self._bound_identity_anchor(manifest, subject_id)
            anchor["enabled"] = bool(enabled)
            source = self._card(manifest, anchor["source_card_id"])
            source["updated_at"] = utc_now()
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "enable_identity_anchor" if enabled else "disable_identity_anchor",
                "status": "complete", "anchor_id": anchor["anchor_id"],
                "subject_id": subject_id, "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def bind_identity_anchor(
        self, *, subject_id: str, anchor_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Make an existing historical identity anchor active for its subject."""
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if manifest.get("pending_operation"):
                raise ProjectError("cannot change an identity anchor while generation is pending")
            subject = str(subject_id).strip()
            if not subject:
                raise ProjectError("subject_id cannot be empty")
            anchor = next(
                (
                    item
                    for card in manifest["cards"]
                    for item in card.get("anchors", [])
                    if item.get("anchor_id") == anchor_id and item.get("role") == "identity"
                ),
                None,
            )
            if anchor is None:
                raise ProjectError("identity anchor does not exist")
            if anchor.get("subject_id") != subject:
                raise ProjectError("identity anchor belongs to a different subject")
            anchor["enabled"] = True
            manifest.setdefault("active_identity_anchors", {})[subject] = anchor_id
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "bind_identity_anchor",
                "status": "complete",
                "anchor_id": anchor_id,
                "subject_id": subject,
                "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def clear_identity_anchor(self, subject_id: str) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot clear an identity anchor while generation is pending")
            bindings = manifest.setdefault("active_identity_anchors", {})
            previous = bindings.pop(subject_id, None)
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()), "kind": "clear_identity_anchor", "status": "complete",
                "anchor_id": previous, "subject_id": subject_id, "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def active_identity_anchor(
        self, manifest: dict[str, Any], subject_id: str = "<Subject 1>", *, include_disabled: bool = False
    ) -> dict[str, Any] | None:
        try:
            anchor = deepcopy(self._bound_identity_anchor(manifest, subject_id))
        except ProjectError:
            return None
        if not include_disabled and not anchor.get("enabled", True):
            return None
        return anchor

    def record_preview(
        self, *, card_id: str, preview_path: str | Path, preview_sha256: str,
        source_artifact_sha256: str,
    ) -> dict[str, Any]:
        """Attach a disposable, rebuildable video preview to a card."""
        with self.locked():
            manifest = self._load_unlocked()
            card = self._card(manifest, card_id)
            if card.get("artifact_sha256") != source_artifact_sha256:
                raise ProjectError("preview source no longer matches the card's current artifact")
            relative = self.relative_path(preview_path)
            path = self.absolute_path(relative)
            if not path.is_file() or sha256_file(path) != preview_sha256:
                raise ProjectError("preview file is missing or its hash does not match")
            card["preview"] = {
                "asset_path": relative,
                "asset_sha256": preview_sha256,
                "source_artifact_sha256": source_artifact_sha256,
                "created_at": utc_now(),
                "media_type": "video/mp4",
                "fps": 24,
            }
            card["updated_at"] = utc_now()
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def card(self, manifest: dict[str, Any], card_id: str) -> dict[str, Any]:
        return deepcopy(self._card(manifest, card_id))

    def update_card_editor(
        self,
        *,
        card_id: str,
        expected_revision: int,
        section_changes: dict[str, str] | None = None,
        clear_sections: list[str] | None = None,
        duration_seconds: float | None = None,
        seed: int | None = None,
        continuation_strategy: str | None = None,
        convert_legacy: bool = False,
        import_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Autosave editable card inputs with optimistic revision protection."""
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if manifest.get("pending_operation"):
                raise ProjectError("cannot edit a card while generation is pending")
            card = self._card(manifest, card_id)
            if card_id != manifest.get("active_card_id"):
                raise ProjectError("only the active generation card is editable")
            if card["status"] not in {"EMPTY", "DRAFT", "FAILED"}:
                raise ProjectError("accepted cards are read-only")

            previous_inputs = (
                card.get("prompt"), card.get("requested_duration_seconds"), card.get("seed"),
                card.get("continuation_strategy"),
            )

            changes = section_changes or {}
            clears = clear_sections or []
            try:
                if convert_legacy and import_prompt is not None:
                    raise ProjectError("convert_legacy and import_prompt cannot be used together")
                if import_prompt is not None:
                    sections = imported_prompt_sections(import_prompt)
                    card["prompt_format"] = "structured_v1"
                elif convert_legacy:
                    if card.get("prompt_format") != "legacy_flat":
                        raise ProjectError("card prompt is already structured")
                    sections = imported_prompt_sections(card.get("assembled_prompt", ""))
                    if changes:
                        sections = edit_sections(sections, changes)
                    card["prompt_format"] = "structured_v1"
                elif changes or clears:
                    if card.get("prompt_format") != "structured_v1":
                        raise ProjectError("convert the legacy flat prompt before editing sections")
                    sections = edit_sections(card["prompt_sections"], changes)
                else:
                    sections = card.get("prompt_sections")
                invalid_clears = set(clears) - set(sections)
                if invalid_clears:
                    raise ProjectError(
                        f"unsupported prompt sections: {', '.join(sorted(invalid_clears))}"
                    )
                for name in clears:
                    sections[name] = section_record()
                if card.get("prompt_format") == "structured_v1":
                    assembled = assemble_prompt(sections)
                    card.update({
                        "prompt_sections": sections,
                        "assembled_prompt": assembled,
                        "prompt": assembled,
                        "prompt_hash": hash_prompt(assembled),
                    })
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc

            if duration_seconds is not None:
                duration = float(duration_seconds)
                if not 0.1 <= duration <= 120.0:
                    raise ProjectError("duration_seconds must be between 0.1 and 120")
                card["requested_duration_seconds"] = duration
            if seed is not None:
                if isinstance(seed, bool):
                    raise ProjectError("seed must be an integer")
                if isinstance(seed, float) and not seed.is_integer():
                    raise ProjectError("seed must be an integer")
                value = int(seed)
                if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
                    raise ProjectError("seed is outside the supported range")
                card["seed"] = value
            if continuation_strategy is not None:
                strategy = str(continuation_strategy)
                if strategy not in CONTINUATION_STRATEGIES:
                    raise ProjectError("continuation_strategy must be independent or direct_mmh3")
                if strategy == "direct_mmh3":
                    predecessor_id = card.get("timeline_predecessor_id")
                    if predecessor_id is None:
                        raise ProjectError("the first card must use independent generation")
                    predecessor = self._card(manifest, predecessor_id)
                    if predecessor.get("status") != "ACCEPTED":
                        raise ProjectError("direct MMH3 continuation requires an accepted predecessor")
                    card["generation_parent_id"] = predecessor_id
                else:
                    card["generation_parent_id"] = None
                card["continuation_strategy"] = strategy
            if card["status"] == "DRAFT" and previous_inputs != (
                card.get("prompt"), card.get("requested_duration_seconds"), card.get("seed"),
                card.get("continuation_strategy"),
            ):
                card["draft_inputs_dirty"] = True
            card["updated_at"] = utc_now()
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def update_project_settings(
        self, *, expected_revision: int, generation_mode: str
    ) -> dict[str, Any]:
        """Change fixed project settings only before the first generation starts."""
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if generation_mode not in GENERATION_MODES:
                raise ProjectError("generation_mode must be ref2va or t2va")
            pristine = (
                len(manifest["cards"]) == 1
                and manifest["cards"][0]["status"] in {"EMPTY", "FAILED"}
                and not manifest["cards"][0].get("artifact_sha256")
                and not manifest.get("pending_operation")
            )
            if not pristine and generation_mode != manifest["generation_mode"]:
                raise ProjectError("generation mode is locked after the first generation")
            if generation_mode != manifest["generation_mode"]:
                manifest["generation_mode"] = generation_mode
                manifest["last_operation"] = {
                    "id": str(uuid.uuid4()),
                    "kind": "update_project_settings",
                    "status": "complete",
                    "ended_at": utc_now(),
                }
                self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def copy_card_sections(
        self,
        *,
        target_card_id: str,
        source_card_id: str,
        names: list[str],
        expected_revision: int,
    ) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if manifest.get("pending_operation"):
                raise ProjectError("cannot edit a card while generation is pending")
            target = self._card(manifest, target_card_id)
            source = self._card(manifest, source_card_id)
            if target_card_id != manifest.get("active_card_id"):
                raise ProjectError("only the active generation card is editable")
            if target["status"] not in {"EMPTY", "DRAFT", "FAILED"}:
                raise ProjectError("accepted cards are read-only")
            if target.get("prompt_format") != "structured_v1":
                raise ProjectError("convert the legacy flat prompt before copying sections")
            if source.get("prompt_format") != "structured_v1":
                raise ProjectError("the source card has a legacy flat prompt")
            try:
                previous_prompt = target.get("prompt")
                sections = copy_sections(
                    target["prompt_sections"],
                    source["prompt_sections"],
                    source_card_id=source_card_id,
                    names=names,
                    previous=target.get("timeline_predecessor_id") == source_card_id,
                )
                assembled = assemble_prompt(sections)
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc
            target.update({
                "prompt_sections": sections,
                "assembled_prompt": assembled,
                "prompt": assembled,
                "prompt_hash": hash_prompt(assembled),
                "updated_at": utc_now(),
            })
            if target["status"] == "DRAFT" and target["prompt"] != previous_prompt:
                target["draft_inputs_dirty"] = True
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    @staticmethod
    def _require_revision(manifest: dict[str, Any], expected_revision: int) -> None:
        if isinstance(expected_revision, bool) or int(expected_revision) != int(manifest.get("revision", -1)):
            raise ProjectError(
                f"stale project revision: expected {expected_revision}, current {manifest.get('revision')}"
            )

    def append(self, *, prompt: str, duration_seconds: float, seed: int) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot append while another operation is pending")
            current = self._active_card(manifest)
            if current["status"] != "ACCEPTED":
                raise ProjectError(f"append requires an ACCEPTED card; current state is {current['status']}")
            card = _new_card(
                timeline_index=len(manifest["cards"]),
                artifact_number=max(
                    [int(item["artifact_number"]) for item in manifest["cards"]]
                    + [
                        int(publication["artifact_number"])
                        for item in manifest["cards"]
                        for publication in item.get("publication_history", [])
                    ]
                ) + 1,
                parent_id=current["id"],
                timeline_predecessor_id=current["id"],
                prompt=prompt,
                duration_seconds=duration_seconds,
                seed=seed,
            )
            if not prompt:
                source_sections = (
                    current["prompt_sections"]
                    if current.get("prompt_format") == "structured_v1"
                    else empty_prompt_sections()
                )
                sections = inherited_prompt_sections(source_sections, current["id"])
                assembled = assemble_prompt(sections)
                card.update({
                    "prompt_sections": sections,
                    "prompt_format": "structured_v1",
                    "assembled_prompt": assembled,
                    "prompt": assembled,
                    "prompt_hash": hash_prompt(assembled),
                })
            manifest["cards"].append(card)
            manifest["active_card_id"] = card["id"]
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()), "kind": "append", "status": "complete", "ended_at": utc_now()
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def active_card(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return deepcopy(self._active_card(manifest))

    def parent_card(self, manifest: dict[str, Any], card: dict[str, Any]) -> dict[str, Any] | None:
        parent_id = card.get("generation_parent_id")
        return deepcopy(self._card(manifest, parent_id)) if parent_id else None

    def relative_path(self, path: str | Path) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            pure = PurePosixPath(str(path).replace("\\", "/"))
            if pure.is_absolute() or ".." in pure.parts:
                raise ProjectError("artifact path escapes the project")
            candidate = self.path.joinpath(*pure.parts)
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self.path)
        except ValueError as exc:
            raise ProjectError("artifact path escapes the project") from exc
        return relative.as_posix()

    def absolute_path(self, relative: str | Path) -> Path:
        canonical = self.relative_path(relative)
        return (self.path / Path(*PurePosixPath(canonical).parts)).resolve()

    def draft_destination(self, card: dict[str, Any]) -> Path:
        filename = f"card_{card['artifact_number']:04d}_{uuid.uuid4().hex}_draft.mmh3"
        return self.path / "drafts" / filename

    def validate_artifacts(self, manifest: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for card in manifest["cards"]:
            relative = card.get("master_path") if card["status"] == "ACCEPTED" else card.get("draft_path")
            if not relative:
                continue
            try:
                path = self.absolute_path(relative)
                if not path.is_file():
                    errors.append(f"card {card['artifact_number']}: missing {relative}")
                elif sha256_file(path) != card.get("artifact_sha256"):
                    errors.append(f"card {card['artifact_number']}: hash mismatch for {relative}")
            except ProjectError as exc:
                errors.append(f"card {card['artifact_number']}: {exc}")
            for publication in card.get("publication_history", []):
                try:
                    published_path = self.absolute_path(publication["master_path"])
                    if not published_path.is_file():
                        errors.append(
                            f"card {card['timeline_index'] + 1}: missing historical master "
                            f"{publication['master_path']}"
                        )
                    elif sha256_file(published_path) != publication.get("artifact_sha256"):
                        errors.append(
                            f"card {card['timeline_index'] + 1}: historical master hash mismatch "
                            f"for {publication['master_path']}"
                        )
                except (KeyError, ProjectError) as exc:
                    errors.append(
                        f"card {card['timeline_index'] + 1}: invalid publication history: {exc}"
                    )
            for anchor in card.get("anchors", []):
                try:
                    anchor_path = self.absolute_path(anchor["asset_path"])
                    if not anchor_path.is_file():
                        errors.append(f"card {card['artifact_number']}: missing anchor {anchor['asset_path']}")
                    elif sha256_file(anchor_path) != anchor.get("asset_sha256"):
                        errors.append(f"card {card['artifact_number']}: anchor hash mismatch for {anchor['asset_path']}")
                except (KeyError, ProjectError) as exc:
                    errors.append(f"card {card['artifact_number']}: invalid anchor: {exc}")
        return errors

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            raise ProjectError(f"project does not exist: {self.project_name}")
        try:
            with self.manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"cannot read project manifest: {exc}") from exc
        migrated = self._migrate_manifest(manifest)
        self._validate(manifest)
        if migrated:
            manifest["revision"] = int(manifest.get("revision", 0)) + 1
            manifest["updated_at"] = utc_now()
            _atomic_json(self.manifest_path, manifest)
        return manifest

    def _commit_unlocked(self, manifest: dict[str, Any]) -> None:
        manifest["revision"] = int(manifest.get("revision", 0)) + 1
        manifest["updated_at"] = utc_now()
        self._validate(manifest)
        _atomic_json(self.manifest_path, manifest)

    def _validate(self, manifest: dict[str, Any]) -> None:
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise ProjectError(f"unsupported project schema: {manifest.get('schema_version')}")
        if manifest.get("project_name") != self.project_name:
            raise ProjectError("project manifest name does not match its directory")
        if manifest.get("generation_mode") not in GENERATION_MODES:
            raise ProjectError("project generation_mode must be ref2va or t2va")
        cards = manifest.get("cards")
        if not isinstance(cards, list) or not cards:
            raise ProjectError("project must contain at least one card")
        ids: set[str] = set()
        accepted_numbers: set[int] = set()
        anchor_ids: set[str] = set()
        publication_ids: set[str] = set()
        anchors_by_id: dict[str, dict[str, Any]] = {}
        for index, card in enumerate(cards):
            if card.get("id") in ids:
                raise ProjectError("duplicate card id in project manifest")
            ids.add(card.get("id"))
            if card.get("timeline_index") != index:
                raise ProjectError("card timeline indexes are not contiguous")
            predecessor_id = card.get("timeline_predecessor_id")
            if index == 0 and predecessor_id is not None:
                raise ProjectError("the first card cannot have a timeline predecessor")
            if predecessor_id is not None and predecessor_id not in ids:
                raise ProjectError("timeline predecessor must be an earlier card UUID")
            parent_id = card.get("generation_parent_id")
            if parent_id is not None and parent_id not in ids:
                raise ProjectError("generation parent must be an earlier card UUID")
            if card.get("status") not in VALID_STATES:
                raise ProjectError(f"invalid card state: {card.get('status')}")
            prompt_format = card.get("prompt_format")
            if prompt_format not in {"legacy_flat", "structured_v1"}:
                raise ProjectError("card prompt_format must be legacy_flat or structured_v1")
            try:
                validate_prompt_sections(card.get("prompt_sections"))
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc
            for record in card["prompt_sections"].values():
                source_card_id = record["provenance"].get("source_card_id")
                if source_card_id is not None and (
                    source_card_id == card.get("id") or source_card_id not in ids
                ):
                    raise ProjectError("prompt section source must be an earlier card UUID")
            prompt = card.get("prompt")
            assembled = card.get("assembled_prompt")
            if not isinstance(prompt, str) or not isinstance(assembled, str) or prompt != assembled:
                raise ProjectError("card prompt must match assembled_prompt")
            if prompt_format == "structured_v1" and assemble_prompt(card["prompt_sections"]) != assembled:
                raise ProjectError("structured card assembled_prompt is stale")
            if card.get("prompt_hash") != hash_prompt(assembled):
                raise ProjectError("card prompt_hash does not match assembled_prompt")
            if card.get("continuation_strategy") not in CONTINUATION_STRATEGIES:
                raise ProjectError("card has an invalid continuation_strategy")
            if card.get("continuation_strategy") == "independent" and parent_id is not None:
                raise ProjectError("independent cards cannot have a generation parent")
            if card.get("continuation_strategy") == "direct_mmh3" and parent_id is None:
                raise ProjectError("direct MMH3 continuation requires a generation parent")
            if card.get("reference_set") is not None and not isinstance(card.get("reference_set"), dict):
                raise ProjectError("card reference_set must be an object or null")
            if not isinstance(card.get("draft_inputs_dirty"), bool):
                raise ProjectError("card draft_inputs_dirty must be boolean")
            accepted_publication_id = card.get("accepted_publication_id")
            if card.get("status") == "ACCEPTED":
                try:
                    uuid.UUID(accepted_publication_id)
                except (ValueError, AttributeError) as exc:
                    raise ProjectError("accepted_publication_id must be a UUID for accepted cards") from exc
                if accepted_publication_id in publication_ids:
                    raise ProjectError("publication IDs must be unique")
                publication_ids.add(accepted_publication_id)
            elif accepted_publication_id is not None:
                raise ProjectError("only accepted cards may have accepted_publication_id")
            if card.get("status") == "ACCEPTED":
                number = card.get("artifact_number")
                if number in accepted_numbers:
                    raise ProjectError("duplicate accepted artifact number")
                accepted_numbers.add(number)
            anchors = card.get("anchors")
            if not isinstance(anchors, list):
                raise ProjectError("card anchors must be a list")
            for anchor in anchors:
                anchor_id = anchor.get("anchor_id")
                if not isinstance(anchor_id, str) or not anchor_id or anchor_id in anchor_ids:
                    raise ProjectError("anchor IDs must be non-empty and unique")
                try:
                    uuid.UUID(anchor_id)
                except (ValueError, AttributeError) as exc:
                    raise ProjectError("anchor_id must be a UUID") from exc
                anchor_ids.add(anchor_id)
                if anchor.get("source_card_id") != card.get("id"):
                    raise ProjectError("anchor source_card_id does not match its card")
                if anchor.get("role") not in {"current_state", "identity"}:
                    raise ProjectError(f"unsupported anchor role: {anchor.get('role')}")
                if not isinstance(anchor.get("source_frame_index"), int) or anchor["source_frame_index"] < 0:
                    raise ProjectError("anchor source_frame_index must be a non-negative integer")
                timestamp = anchor.get("source_timestamp_seconds")
                if not isinstance(timestamp, (int, float)) or timestamp < 0:
                    raise ProjectError("anchor source_timestamp_seconds must be non-negative")
                if not isinstance(anchor.get("enabled"), bool):
                    raise ProjectError("anchor enabled must be boolean")
                if not isinstance(anchor.get("mode"), str) or not anchor["mode"]:
                    raise ProjectError("anchor mode is required")
                asset_path = anchor.get("asset_path")
                if not isinstance(asset_path, str) or not asset_path:
                    raise ProjectError("anchor asset_path is required")
                if not isinstance(anchor.get("asset_sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", anchor["asset_sha256"]
                ):
                    raise ProjectError("anchor asset_sha256 must be a lowercase SHA-256")
                self.relative_path(asset_path)
                if anchor.get("role") == "identity":
                    if card.get("status") != "ACCEPTED":
                        raise ProjectError("identity anchors must belong to accepted cards")
                    subject_id = anchor.get("subject_id")
                    if not isinstance(subject_id, str) or not subject_id.strip():
                        raise ProjectError("identity anchor subject_id is required")
                    preview_index = anchor.get("source_preview_frame_index")
                    if not isinstance(preview_index, int) or preview_index < 0:
                        raise ProjectError("identity anchor preview frame index must be non-negative")
                    scope = anchor.get("identity_scope")
                    if scope not in IDENTITY_SCOPES:
                        raise ProjectError("identity anchor has an invalid identity_scope")
                    custom = anchor.get("custom_identity_instruction")
                    if scope == "custom" and (not isinstance(custom, str) or not custom.strip()):
                        raise ProjectError("custom identity scope requires a custom identity instruction")
                    if scope != "custom" and custom is not None:
                        raise ProjectError("non-custom identity scope cannot store a custom identity instruction")
                anchors_by_id[anchor_id] = anchor
            history = card.get("publication_history")
            if not isinstance(history, list):
                raise ProjectError("card publication_history must be a list")
            for publication in history:
                try:
                    publication_id = publication.get("publication_id", "")
                    uuid.UUID(publication_id)
                except (ValueError, AttributeError) as exc:
                    raise ProjectError("publication_id must be a UUID") from exc
                if publication_id in publication_ids:
                    raise ProjectError("publication IDs must be unique")
                publication_ids.add(publication_id)
                number = publication.get("artifact_number")
                if not isinstance(number, int) or number < 1 or number in accepted_numbers:
                    raise ProjectError("published artifact numbers must be positive and unique")
                accepted_numbers.add(number)
                path = publication.get("master_path")
                if not isinstance(path, str) or not path:
                    raise ProjectError("publication history master_path is required")
                self.relative_path(path)
                if not isinstance(publication.get("artifact_sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", publication["artifact_sha256"]
                ):
                    raise ProjectError("publication history artifact_sha256 must be a lowercase SHA-256")
        if manifest.get("active_card_id") not in ids:
            raise ProjectError("active card does not exist")
        bindings = manifest.get("active_identity_anchors")
        if not isinstance(bindings, dict):
            raise ProjectError("active_identity_anchors must be an object")
        for subject_id, anchor_id in bindings.items():
            if not isinstance(subject_id, str) or not subject_id.strip():
                raise ProjectError("identity anchor binding subject ID is invalid")
            anchor = anchors_by_id.get(anchor_id)
            if anchor is None or anchor.get("role") != "identity":
                raise ProjectError("identity anchor binding points to a missing or non-identity anchor")
            if anchor.get("subject_id") != subject_id:
                raise ProjectError("identity anchor binding subject does not match its anchor")

    @staticmethod
    def _migrate_manifest(manifest: dict[str, Any]) -> bool:
        version = manifest.get("schema_version")
        if version == SCHEMA_VERSION:
            return False
        if version not in {1, 2, 3, 4, 5, 6}:
            raise ProjectError(f"unsupported project schema: {version}")
        previous_id = None
        for card in manifest.get("cards", []):
            card.setdefault("anchors", [])
            card.setdefault("publication_history", [])
            card.setdefault("timeline_predecessor_id", previous_id)
            fields = prompt_fields(card.get("prompt", ""))
            for key, value in fields.items():
                card.setdefault(key, value)
            if card.get("prompt_format") == "structured_v1":
                old_assembled = card.get("assembled_prompt", "")
                assembled = assemble_prompt(card["prompt_sections"])
                card["assembled_prompt"] = assembled
                card["prompt_hash"] = hash_prompt(assembled)
                if old_assembled != assembled and card.get("status") == "DRAFT":
                    card["draft_inputs_dirty"] = True
            card["prompt"] = card["assembled_prompt"]
            card.setdefault(
                "continuation_strategy",
                "direct_mmh3" if card.get("generation_parent_id") else "independent",
            )
            recipe = card.get("recipe") or {}
            references = recipe.get("references") if isinstance(recipe, dict) else None
            card.setdefault("reference_set", deepcopy(references) if isinstance(references, dict) else None)
            card.setdefault(
                "accepted_publication_id",
                str(uuid.uuid4()) if card.get("status") == "ACCEPTED" else None,
            )
            card.setdefault("draft_inputs_dirty", False)
            for anchor in card["anchors"]:
                if anchor.get("role") == "identity":
                    anchor.setdefault("identity_scope", "face_only")
                    anchor.setdefault("custom_identity_instruction", None)
            previous_id = card.get("id")
        manifest.setdefault("active_identity_anchors", {})
        manifest["schema_version"] = SCHEMA_VERSION
        return True

    def _validate_anchor_asset(self, card: dict[str, Any], anchor: dict[str, Any]) -> None:
        if anchor.get("source_card_id") != card.get("id"):
            raise ProjectError("anchor source_card_id does not match the card being accepted")
        if anchor.get("role") not in {"current_state", "identity"}:
            raise ProjectError("anchor role must be current_state or identity")
        try:
            uuid.UUID(anchor.get("anchor_id", ""))
        except (ValueError, AttributeError) as exc:
            raise ProjectError("anchor_id must be a UUID") from exc
        if not isinstance(anchor.get("source_frame_index"), int) or anchor["source_frame_index"] < 0:
            raise ProjectError("anchor source_frame_index must be a non-negative integer")
        timestamp = anchor.get("source_timestamp_seconds")
        if not isinstance(timestamp, (int, float)) or timestamp < 0:
            raise ProjectError("anchor source_timestamp_seconds must be non-negative")
        if not isinstance(anchor.get("enabled"), bool):
            raise ProjectError("anchor enabled must be boolean")
        if not isinstance(anchor.get("mode"), str) or not anchor["mode"]:
            raise ProjectError("anchor mode is required")
        if anchor.get("role") == "identity":
            scope = anchor.get("identity_scope")
            if scope not in IDENTITY_SCOPES:
                raise ProjectError("identity anchor has an invalid identity_scope")
            custom = anchor.get("custom_identity_instruction")
            if scope == "custom" and (not isinstance(custom, str) or not custom.strip()):
                raise ProjectError("custom identity scope requires a custom identity instruction")
            if scope != "custom" and custom is not None:
                raise ProjectError("non-custom identity scope cannot store a custom identity instruction")
        if not isinstance(anchor.get("asset_sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", anchor["asset_sha256"]
        ):
            raise ProjectError("anchor asset_sha256 must be a lowercase SHA-256")
        asset_path = anchor.get("asset_path")
        if not isinstance(asset_path, str) or not asset_path:
            raise ProjectError("anchor asset_path is required")
        path = self.absolute_path(asset_path)
        if not path.is_file():
            raise ProjectError(f"state anchor was not written: {anchor.get('asset_path')}")
        if sha256_file(path) != anchor.get("asset_sha256"):
            raise ProjectError("state anchor hash changed before manifest commit")

    @staticmethod
    def _bound_identity_anchor(manifest: dict[str, Any], subject_id: str) -> dict[str, Any]:
        anchor_id = manifest.get("active_identity_anchors", {}).get(subject_id)
        if not anchor_id:
            raise ProjectError(f"no identity anchor is selected for {subject_id}")
        for card in manifest["cards"]:
            for anchor in card.get("anchors", []):
                if anchor.get("anchor_id") == anchor_id and anchor.get("role") == "identity":
                    return anchor
        raise ProjectError(f"active identity anchor is missing: {anchor_id}")

    @staticmethod
    def _card(manifest: dict[str, Any], card_id: str | None) -> dict[str, Any]:
        for card in manifest["cards"]:
            if card["id"] == card_id:
                return card
        raise ProjectError(f"card does not exist: {card_id}")

    def _active_card(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return self._card(manifest, manifest["active_card_id"])

    @staticmethod
    def _finish_accept_unlocked(manifest: dict[str, Any], transaction: dict[str, Any]) -> None:
        card = ProjectStore._card(manifest, transaction["card_id"])
        anchor = transaction.get("anchor")
        anchors = card.setdefault("anchors", [])
        if anchor is not None and not any(
            item.get("anchor_id") == anchor.get("anchor_id") for item in anchors
        ):
            anchors.append(deepcopy(anchor))
        card.update({
            "status": "ACCEPTED",
            "master_path": transaction["destination"],
            "draft_path": None,
            "artifact_sha256": transaction["sha256"],
            "accepted_publication_id": transaction.get("publication_id") or str(uuid.uuid4()),
            "accepted_at": utc_now(),
            "updated_at": utc_now(),
            "last_error": None,
            "draft_inputs_dirty": False,
        })
        manifest["last_operation"] = {
            "id": str(uuid.uuid4()), "kind": "accept", "status": "complete", "ended_at": utc_now()
        }

    def _recover_accept_transactions_unlocked(self, manifest: dict[str, Any]) -> bool:
        changed = False
        transaction_dir = self.path / "transactions"
        if not transaction_dir.exists():
            return False
        for journal in transaction_dir.glob("accept_*.json"):
            try:
                transaction = json.loads(journal.read_text(encoding="utf-8"))
                card = self._card(manifest, transaction["card_id"])
                destination = self.absolute_path(transaction["destination"])
                if card["status"] == "ACCEPTED":
                    if card.get("artifact_sha256") != transaction["sha256"]:
                        raise ProjectError("completed accept journal disagrees with the project manifest")
                    journal.unlink(missing_ok=True)
                    continue
                if destination.is_file() and sha256_file(destination) == transaction["sha256"]:
                    if transaction.get("anchor") is not None:
                        self._validate_anchor_asset(card, transaction["anchor"])
                    self._finish_accept_unlocked(manifest, transaction)
                    journal.unlink(missing_ok=True)
                    changed = True
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                raise ProjectError(f"cannot recover transaction {journal.name}: {exc}") from exc
        return changed
