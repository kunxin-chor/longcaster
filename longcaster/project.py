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
from .refine import CONTINUATION_SOURCE_TYPES, REFINE_CADENCES, validate_refine_cadence


SCHEMA_VERSION = 13
VALID_STATES = {"EMPTY", "DRAFT", "ACCEPTED", "FAILED", "INVALIDATED"}
IDENTITY_SCOPES = {"face_only", "face_clothing", "face_body", "everything", "custom"}
GENERATION_MODES = {"ref2va", "t2va"}
CONTINUATION_STRATEGIES = {"independent", "direct_mmh3"}
REF_IMAGE_SIZES = {"match", "max"}
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


def validate_lora_activation_words(value: str) -> str:
    if not isinstance(value, str):
        raise ProjectError("lora_activation_words must be a string")
    if "\x00" in value:
        raise ProjectError("lora_activation_words cannot contain NUL characters")
    words = value.strip()
    if len(words) > 2000:
        raise ProjectError("lora_activation_words cannot exceed 2000 characters")
    return words


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
    ref_image_size: str = "match",
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
    continuation_source = (
        {
            "source_card_id": parent_id,
            "type": "accepted_master",
            "derivative_id": None,
        }
        if parent_id
        else None
    )
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
        "ref_image_size": ref_image_size,
        "continuation_strategy": "direct_mmh3" if parent_id else "independent",
        "continuation_source": continuation_source,
        "continuation_source_preference": "accepted_master",
        "refine_enabled": False,
        "derivatives": [],
        "reference_set": None,
        "accepted_publication_id": None,
        "draft_inputs_dirty": False,
        "draft_takes": [],
        "selected_draft_take_id": None,
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


def _take_from_card(card: dict[str, Any], *, artifact_path: str | None = None) -> dict[str, Any]:
    """Freeze the generation inputs and outputs represented by the card fields."""
    path = artifact_path or card.get("draft_path") or card.get("master_path")
    if not path or not card.get("artifact_sha256"):
        raise ProjectError("cannot create a draft take without a completed artifact")
    return {
        "id": str(uuid.uuid4()),
        "attempt": int(card.get("attempt", 0)),
        "artifact_path": path,
        "artifact_sha256": card["artifact_sha256"],
        "prompt": card.get("prompt", ""),
        "prompt_format": card.get("prompt_format", "legacy_flat"),
        "prompt_sections": deepcopy(card.get("prompt_sections")),
        "assembled_prompt": card.get("assembled_prompt", card.get("prompt", "")),
        "prompt_hash": card.get("prompt_hash"),
        "requested_duration_seconds": card.get("requested_duration_seconds"),
        "seed": card.get("seed"),
        "ref_image_size": card.get("ref_image_size", "match"),
        "continuation_strategy": card.get("continuation_strategy"),
        "continuation_source": deepcopy(card.get("continuation_source")),
        "recipe": deepcopy(card.get("recipe")),
        "generation_fingerprint": card.get("generation_fingerprint"),
        "reference_set": deepcopy(card.get("reference_set")),
        "context_frame_count": card.get("context_frame_count", 0),
        "generated_frame_count": card.get("generated_frame_count"),
        "actual_new_frame_count": card.get("actual_new_frame_count"),
        "actual_duration_seconds": card.get("actual_duration_seconds"),
        "preview": deepcopy(card.get("preview")),
        "created_at": utc_now(),
    }


def _apply_take_to_card(card: dict[str, Any], take: dict[str, Any]) -> None:
    """Mirror an immutable selected take into the legacy/current card fields."""
    card.update({
        "prompt": take["prompt"],
        "prompt_format": take["prompt_format"],
        "prompt_sections": deepcopy(take["prompt_sections"]),
        "assembled_prompt": take["assembled_prompt"],
        "prompt_hash": take["prompt_hash"],
        "requested_duration_seconds": take["requested_duration_seconds"],
        "seed": take["seed"],
        "ref_image_size": take.get("ref_image_size", "match"),
        "continuation_strategy": take["continuation_strategy"],
        "continuation_source": deepcopy(take.get("continuation_source")),
        "recipe": deepcopy(take["recipe"]),
        "generation_fingerprint": take["generation_fingerprint"],
        "reference_set": deepcopy(take.get("reference_set")),
        "attempt": int(take["attempt"]),
        "draft_path": take["artifact_path"],
        "artifact_sha256": take["artifact_sha256"],
        "context_frame_count": int(take.get("context_frame_count", 0)),
        "generated_frame_count": take.get("generated_frame_count"),
        "actual_new_frame_count": take.get("actual_new_frame_count"),
        "actual_duration_seconds": take.get("actual_duration_seconds"),
        "preview": deepcopy(take.get("preview")),
        "selected_draft_take_id": take["id"],
        "draft_inputs_dirty": False,
        "last_error": None,
        "updated_at": utc_now(),
    })


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
        refine_cadence: str = "off",
        lora_activation_words: str = "",
    ) -> dict[str, Any]:
        if generation_mode not in GENERATION_MODES:
            raise ProjectError("generation_mode must be ref2va or t2va")
        try:
            refine_cadence = validate_refine_cadence(refine_cadence)
        except RuntimeError as exc:
            raise ProjectError(str(exc)) from exc
        lora_activation_words = validate_lora_activation_words(lora_activation_words)
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
            for directory in (
                "clips", "drafts", "transactions", "previews", "anchors", "derivatives"
            ):
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
                "refine_cadence": refine_cadence,
                "lora_activation_words": lora_activation_words,
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
                if pending.get("kind") == "refine":
                    derivative = self._derivative(card, pending.get("derivative_id"))
                    derivative.update({
                        "status": "FAILED",
                        "completed_at": utc_now(),
                        "last_error": "Refine was interrupted before its derivative was committed.",
                    })
                    if (
                        card.get("continuation_source_preference") == "derivative"
                        and self._latest_ready_refine(card) is None
                    ):
                        card["continuation_source_preference"] = "accepted_master"
                else:
                    card["last_error"] = "Generation was interrupted before its draft was committed."
                    if pending.get("previous_card_status") in {"EMPTY", "FAILED", "INVALIDATED"}:
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
            expected = {"EMPTY", "FAILED", "INVALIDATED"} if action == "generate" else {"DRAFT"}
            if card["status"] not in expected:
                label = "EMPTY, FAILED, or INVALIDATED" if action == "generate" else "DRAFT"
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
            candidate = pending.get("candidate", {})
            candidate_prompt = candidate.get("prompt", card["prompt"])
            if card.get("prompt_format") == "legacy_flat":
                card["assembled_prompt"] = candidate_prompt
                card["prompt_hash"] = hash_prompt(candidate_prompt)
            next_attempt = max(
                [int(card.get("attempt", 0))]
                + [int(item.get("attempt", 0)) for item in card.get("draft_takes", [])]
            ) + 1
            card.update({
                "prompt": candidate_prompt,
                "requested_duration_seconds": candidate.get(
                    "requested_duration_seconds", card["requested_duration_seconds"]
                ),
                "seed": candidate.get("seed", card["seed"]),
                "ref_image_size": (candidate.get("recipe") or {}).get(
                    "ref_image_size", card.get("ref_image_size", "match")
                ),
                "recipe": deepcopy(candidate.get("recipe", card.get("recipe"))),
                "generation_fingerprint": candidate.get(
                    "generation_fingerprint", card.get("generation_fingerprint")
                ),
                "reference_set": deepcopy(
                    (candidate.get("recipe") or {}).get("references", card.get("reference_set"))
                ),
                "status": "DRAFT",
                "attempt": next_attempt,
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
            take = _take_from_card(card, artifact_path=relative)
            card.setdefault("draft_takes", []).append(take)
            card["selected_draft_take_id"] = take["id"]
            operation_record = {key: value for key, value in pending.items() if key != "candidate"}
            manifest["last_operation"] = {**operation_record, "status": "complete", "ended_at": utc_now()}
            manifest["pending_operation"] = None
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def fail_generation(self, operation_id: str, error: str) -> None:
        with self.locked():
            manifest = self._load_unlocked()
            pending = manifest.get("pending_operation")
            if not pending or pending.get("id") != operation_id:
                return
            card = self._card(manifest, pending["card_id"])
            card["last_error"] = str(error)[:4000]
            if pending.get("previous_card_status") in {"EMPTY", "FAILED", "INVALIDATED"}:
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
            if pending.get("kind") == "refine":
                derivative = self._derivative(card, pending.get("derivative_id"))
                derivative.update({
                    "status": "FAILED",
                    "completed_at": utc_now(),
                    "last_error": str(reason)[:4000],
                })
                if (
                    card.get("continuation_source_preference") == "derivative"
                    and self._latest_ready_refine(card) is None
                ):
                    card["continuation_source_preference"] = "accepted_master"
            else:
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

            derivative_paths: list[Path] = []
            derivative_records = list(card.get("derivatives", []))
            for publication in card.get("publication_history", []):
                derivative_records.extend(publication.get("derivatives", []))
            for derivative in derivative_records:
                relative = derivative.get("artifact_path")
                if relative:
                    derivative_paths.append(self.absolute_path(relative))

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
                "ref_image_size": card.get("ref_image_size", "match"),
                "requested_duration_seconds": card.get("requested_duration_seconds"),
                "actual_duration_seconds": card.get("actual_duration_seconds"),
                "continuation_strategy": card.get("continuation_strategy"),
                "reference_set": deepcopy(card.get("reference_set")),
                "generation_fingerprint": card.get("generation_fingerprint"),
                "recipe": deepcopy(card.get("recipe")),
                "derivatives": [],
                "continuation_source_preference": "accepted_master",
            })
            for publication in history:
                publication["derivatives"] = []
                publication["continuation_source_preference"] = "accepted_master"
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
                "derivatives": [],
                "continuation_source_preference": "accepted_master",
                "preview": None,
                "updated_at": utc_now(),
                "last_error": None,
                "draft_inputs_dirty": False,
            })
            selected_take = next(
                (
                    item for item in card.get("draft_takes", [])
                    if item.get("id") == card.get("selected_draft_take_id")
                ),
                None,
            )
            superseded_take_path = None
            if selected_take is None:
                selected_take = _take_from_card(card, artifact_path=self.relative_path(draft))
                card.setdefault("draft_takes", []).append(selected_take)
                card["selected_draft_take_id"] = selected_take["id"]
            else:
                superseded_take_path = selected_take.get("artifact_path")
                selected_take["artifact_path"] = self.relative_path(draft)
                selected_take["artifact_sha256"] = artifact_hash
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
            retained_take_paths = {
                item.get("artifact_path") for item in card.get("draft_takes", [])
                if item.get("artifact_path")
            }
            retained_take_paths.update(
                item.get("master_path") for item in card.get("publication_history", [])
                if item.get("master_path")
            )
            if superseded_take_path and superseded_take_path not in retained_take_paths:
                try:
                    self.absolute_path(superseded_take_path).unlink(missing_ok=True)
                except OSError:
                    pass
            for path in derivative_paths:
                path.unlink(missing_ok=True)
            derivative_directory = self.path / "derivatives" / card["id"]
            try:
                derivative_directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # Unknown files are not removed as part of derivative cleanup.
                pass
            return deepcopy(manifest)

    def set_refine_cadence(
        self, cadence: str, *, expected_revision: int | None = None
    ) -> dict[str, Any]:
        try:
            cadence = validate_refine_cadence(cadence)
        except RuntimeError as exc:
            raise ProjectError(str(exc)) from exc
        with self.locked():
            manifest = self._load_unlocked()
            if expected_revision is not None:
                self._require_revision(manifest, expected_revision)
            if manifest.get("pending_operation"):
                raise ProjectError("cannot change refine cadence while an operation is pending")
            if manifest.get("refine_cadence") != cadence:
                manifest["refine_cadence"] = cadence
                manifest["last_operation"] = {
                    "id": str(uuid.uuid4()),
                    "kind": "set_refine_cadence",
                    "status": "complete",
                    "ended_at": utc_now(),
                }
                self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def begin_refine(
        self,
        *,
        card_id: str,
        cadence: str,
        recipe: dict[str, Any],
        use_as_default: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            cadence = validate_refine_cadence(cadence)
        except RuntimeError as exc:
            raise ProjectError(str(exc)) from exc
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("another project operation is already pending")
            card = self._card(manifest, card_id)
            if card.get("status") != "ACCEPTED":
                raise ProjectError("refine requires an accepted card")
            source_path = card.get("master_path")
            source_hash = card.get("artifact_sha256")
            source = self.absolute_path(source_path)
            if not source.is_file() or sha256_file(source) != source_hash:
                raise ProjectError("accepted master is missing or corrupt")
            derivative_id = str(uuid.uuid4())
            now = utc_now()
            derivative = {
                "id": derivative_id,
                "type": "refine",
                "status": "PROCESSING",
                "source_card_id": card["id"],
                "source_master_path": source_path,
                "source_artifact_sha256": source_hash,
                "cadence": cadence,
                "recipe": deepcopy(recipe),
                "artifact_path": None,
                "artifact_sha256": None,
                "created_at": now,
                "started_at": now,
                "completed_at": None,
                "execution_seconds": None,
                "last_error": None,
                "use_as_default": bool(use_as_default),
            }
            card.setdefault("derivatives", []).append(derivative)
            operation = {
                "id": str(uuid.uuid4()),
                "kind": "refine",
                "status": "running",
                "card_id": card["id"],
                "derivative_id": derivative_id,
                "started_at": now,
                "owner_runtime_id": _RUNTIME_ID,
                "owner_pid": os.getpid(),
            }
            manifest["pending_operation"] = operation
            card["updated_at"] = now
            self._commit_unlocked(manifest)
            return deepcopy(manifest), deepcopy(derivative)

    def finish_refine(
        self,
        *,
        operation_id: str,
        derivative_path: str | Path,
        artifact_sha256: str,
        execution_seconds: float,
        prefix_protection: dict[str, Any],
        audio_handling: str,
    ) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            pending = manifest.get("pending_operation")
            if (
                not pending
                or pending.get("id") != operation_id
                or pending.get("kind") != "refine"
            ):
                raise ProjectError("refine result is stale or no longer pending")
            card = self._card(manifest, pending["card_id"])
            derivative = self._derivative(card, pending["derivative_id"])
            if (
                card.get("status") != "ACCEPTED"
                or card.get("master_path") != derivative.get("source_master_path")
                or card.get("artifact_sha256") != derivative.get("source_artifact_sha256")
            ):
                raise ProjectError("accepted source changed while refine was running")
            relative = self.relative_path(derivative_path)
            path = self.absolute_path(relative)
            if not path.is_file() or sha256_file(path) != artifact_sha256:
                raise ProjectError("refine derivative is missing or its hash does not match")
            derivative.update({
                "status": "READY",
                "artifact_path": relative,
                "artifact_sha256": artifact_sha256,
                "completed_at": utc_now(),
                "execution_seconds": max(0.0, float(execution_seconds)),
                "prefix_protection": deepcopy(prefix_protection),
                "audio_handling": str(audio_handling),
                "last_error": None,
            })
            if derivative.get("use_as_default"):
                card["continuation_source_preference"] = "derivative"
            card["updated_at"] = utc_now()
            manifest["last_operation"] = {
                **{key: value for key, value in pending.items() if key != "owner_runtime_id"},
                "status": "complete",
                "ended_at": utc_now(),
            }
            manifest["pending_operation"] = None
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def fail_refine(self, operation_id: str, error: str) -> dict[str, Any] | None:
        with self.locked():
            manifest = self._load_unlocked()
            pending = manifest.get("pending_operation")
            if (
                not pending
                or pending.get("id") != operation_id
                or pending.get("kind") != "refine"
            ):
                return None
            card = self._card(manifest, pending["card_id"])
            derivative = self._derivative(card, pending["derivative_id"])
            message = str(error)[:4000]
            derivative.update({
                "status": "FAILED",
                "completed_at": utc_now(),
                "last_error": message,
            })
            if (
                card.get("continuation_source_preference") == "derivative"
                and self._latest_ready_refine(card) is None
            ):
                card["continuation_source_preference"] = "accepted_master"
            card["updated_at"] = utc_now()
            manifest["last_operation"] = {
                **{key: value for key, value in pending.items() if key != "owner_runtime_id"},
                "status": "failed",
                "error": message,
                "ended_at": utc_now(),
            }
            manifest["pending_operation"] = None
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
            if len(manifest["cards"]) < 2:
                raise ProjectError("the first card cannot be removed because there is no previous accepted card")
            predecessor = manifest["cards"][-2]
            if (
                card.get("timeline_predecessor_id") != predecessor["id"]
                or predecessor["status"] != "ACCEPTED"
            ):
                raise ProjectError("the removable draft must follow an accepted card")

            removed_card_id = card["id"]
            publication_history = card.get("publication_history", [])
            disposable_relatives = [
                card.get("draft_path"),
                card.get("master_path"),
                (card.get("preview") or {}).get("asset_path"),
                *(item.get("artifact_path") for item in card.get("draft_takes", [])),
                *(
                    (item.get("preview") or {}).get("asset_path")
                    for item in card.get("draft_takes", [])
                ),
                *(item.get("asset_path") for item in card.get("anchors", [])),
                *(item.get("artifact_path") for item in card.get("derivatives", [])),
            ]
            for publication in publication_history:
                disposable_relatives.extend((
                    publication.get("master_path"),
                    (publication.get("preview") or {}).get("asset_path"),
                    *(item.get("asset_path") for item in publication.get("anchors", [])),
                    *(item.get("artifact_path") for item in publication.get("derivatives", [])),
                ))
            disposable_files: list[Path] = []
            for relative in disposable_relatives:
                if relative:
                    disposable_files.append(self.absolute_path(relative))

            removed_anchor_ids = {
                item.get("anchor_id")
                for item in card.get("anchors", [])
                if item.get("anchor_id")
            }
            for publication in publication_history:
                removed_anchor_ids.update(
                    item.get("anchor_id")
                    for item in publication.get("anchors", [])
                    if item.get("anchor_id")
                )
            bindings = manifest.setdefault("active_identity_anchors", {})
            for subject_id, anchor_id in list(bindings.items()):
                if anchor_id in removed_anchor_ids:
                    del bindings[subject_id]

            manifest["cards"].pop()
            manifest["active_card_id"] = predecessor["id"]
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "remove_draft_tail",
                "status": "complete",
                "card_id": removed_card_id,
                "restored_card_id": predecessor["id"],
                "removed_publication_count": len(publication_history),
                "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)

            for path in set(disposable_files):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            for relative_directory in (
                f"previews/{removed_card_id}",
                f"anchors/{removed_card_id}",
                f"anchor_candidates/{removed_card_id}",
                f"derivatives/{removed_card_id}",
            ):
                try:
                    shutil.rmtree(self.absolute_path(relative_directory), ignore_errors=True)
                except OSError:
                    pass
            return deepcopy(manifest)

    def invalidate_render(self) -> dict[str, Any]:
        """Delete the active tail's render lineage while preserving its card definition."""
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot invalidate a render while another operation is pending")
            card = self._active_card(manifest)
            if card is not manifest["cards"][-1] or any(
                item.get("timeline_predecessor_id") == card["id"]
                or item.get("generation_parent_id") == card["id"]
                for item in manifest["cards"]
            ):
                raise ProjectError("only the active timeline tail with no dependent cards can be invalidated")
            if card.get("status") not in {"DRAFT", "ACCEPTED"}:
                raise ProjectError("only a rendered DRAFT or ACCEPTED card can be invalidated")

            history = card.get("publication_history", [])
            disposable_relatives = [
                card.get("draft_path"),
                card.get("master_path"),
                (card.get("preview") or {}).get("asset_path"),
                *(item.get("artifact_path") for item in card.get("draft_takes", [])),
                *((item.get("preview") or {}).get("asset_path") for item in card.get("draft_takes", [])),
                *(item.get("asset_path") for item in card.get("anchors", [])),
                *(item.get("artifact_path") for item in card.get("derivatives", [])),
            ]
            for publication in history:
                disposable_relatives.extend((
                    publication.get("master_path"),
                    (publication.get("preview") or {}).get("asset_path"),
                    *(item.get("asset_path") for item in publication.get("anchors", [])),
                    *(item.get("artifact_path") for item in publication.get("derivatives", [])),
                ))

            operation_id = str(uuid.uuid4())
            quarantine = self.path / "transactions" / f".invalidate_{operation_id}"
            moved: list[tuple[Path, Path]] = []
            try:
                for relative in dict.fromkeys(item for item in disposable_relatives if item):
                    source = self.absolute_path(relative)
                    if not source.is_file():
                        continue
                    staged = quarantine.joinpath(*PurePosixPath(relative).parts)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, staged)
                    moved.append((source, staged))

                removed_anchor_ids = {
                    item.get("anchor_id") for item in card.get("anchors", [])
                    if item.get("anchor_id")
                }
                for publication in history:
                    removed_anchor_ids.update(
                        item.get("anchor_id") for item in publication.get("anchors", [])
                        if item.get("anchor_id")
                    )
                bindings = manifest.setdefault("active_identity_anchors", {})
                for subject_id, anchor_id in list(bindings.items()):
                    if anchor_id in removed_anchor_ids:
                        del bindings[subject_id]

                previous_status = card["status"]
                previous_artifact_number = card["artifact_number"]
                if previous_status == "ACCEPTED":
                    used_numbers = [int(item["artifact_number"]) for item in manifest["cards"]]
                    used_numbers.extend(
                        int(publication["artifact_number"])
                        for item in manifest["cards"]
                        for publication in item.get("publication_history", [])
                    )
                    card["artifact_number"] = max(used_numbers) + 1
                card.update({
                    "status": "INVALIDATED",
                    "attempt": 0,
                    "draft_path": None,
                    "master_path": None,
                    "artifact_sha256": None,
                    "generation_fingerprint": None,
                    "recipe": None,
                    "reference_set": None,
                    "context_frame_count": 0,
                    "generated_frame_count": None,
                    "actual_new_frame_count": None,
                    "actual_duration_seconds": None,
                    "accepted_at": None,
                    "accepted_publication_id": None,
                    "draft_inputs_dirty": False,
                    "draft_takes": [],
                    "selected_draft_take_id": None,
                    "preview": None,
                    "anchors": [],
                    "derivatives": [],
                    "publication_history": [],
                    "continuation_source_preference": "accepted_master",
                    "updated_at": utc_now(),
                    "last_error": None,
                })
                manifest["last_operation"] = {
                    "id": operation_id,
                    "kind": "invalidate_render",
                    "status": "complete",
                    "card_id": card["id"],
                    "previous_card_status": previous_status,
                    "invalidated_artifact_number": previous_artifact_number,
                    "removed_file_count": len(moved),
                    "ended_at": utc_now(),
                }
                self._commit_unlocked(manifest)
            except Exception:
                for source, staged in reversed(moved):
                    if staged.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(staged, source)
                shutil.rmtree(quarantine, ignore_errors=True)
                raise

            shutil.rmtree(quarantine, ignore_errors=True)
            for relative_directory in (
                f"previews/{card['id']}",
                f"anchors/{card['id']}",
                f"anchor_candidates/{card['id']}",
                f"derivatives/{card['id']}",
            ):
                try:
                    shutil.rmtree(self.absolute_path(relative_directory), ignore_errors=True)
                except OSError:
                    pass
            return deepcopy(manifest)

    def invalidate_all_renders(self) -> dict[str, Any]:
        """Reset every card's render lineage and restart regeneration at Card 1."""
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot invalidate renders while another operation is pending")
            if all(card.get("status") == "INVALIDATED" for card in manifest["cards"]):
                raise ProjectError("all cards are already invalidated")

            disposable_relatives: list[str | None] = []
            used_numbers: list[int] = []
            for card in manifest["cards"]:
                used_numbers.append(int(card["artifact_number"]))
                disposable_relatives.extend((
                    card.get("draft_path"),
                    card.get("master_path"),
                    (card.get("preview") or {}).get("asset_path"),
                    *(item.get("artifact_path") for item in card.get("draft_takes", [])),
                    *((item.get("preview") or {}).get("asset_path") for item in card.get("draft_takes", [])),
                    *(item.get("asset_path") for item in card.get("anchors", [])),
                    *(item.get("artifact_path") for item in card.get("derivatives", [])),
                ))
                for publication in card.get("publication_history", []):
                    used_numbers.append(int(publication["artifact_number"]))
                    disposable_relatives.extend((
                        publication.get("master_path"),
                        (publication.get("preview") or {}).get("asset_path"),
                        *(item.get("asset_path") for item in publication.get("anchors", [])),
                        *(item.get("artifact_path") for item in publication.get("derivatives", [])),
                    ))

            operation_id = str(uuid.uuid4())
            quarantine = self.path / "transactions" / f".invalidate_all_{operation_id}"
            moved: list[tuple[Path, Path]] = []
            try:
                for relative in dict.fromkeys(item for item in disposable_relatives if item):
                    source = self.absolute_path(relative)
                    if not source.is_file():
                        continue
                    staged = quarantine.joinpath(*PurePosixPath(relative).parts)
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, staged)
                    moved.append((source, staged))

                next_artifact_number = max(used_numbers, default=0) + 1
                for offset, card in enumerate(manifest["cards"]):
                    card.update({
                        "artifact_number": next_artifact_number + offset,
                        "status": "INVALIDATED",
                        "attempt": 0,
                        "draft_path": None,
                        "master_path": None,
                        "artifact_sha256": None,
                        "generation_fingerprint": None,
                        "recipe": None,
                        "reference_set": None,
                        "context_frame_count": 0,
                        "generated_frame_count": None,
                        "actual_new_frame_count": None,
                        "actual_duration_seconds": None,
                        "accepted_at": None,
                        "accepted_publication_id": None,
                        "draft_inputs_dirty": False,
                        "draft_takes": [],
                        "selected_draft_take_id": None,
                        "preview": None,
                        "anchors": [],
                        "derivatives": [],
                        "publication_history": [],
                        "continuation_source_preference": "accepted_master",
                        "updated_at": utc_now(),
                        "last_error": None,
                    })
                    if card.get("continuation_strategy") == "direct_mmh3":
                        card["continuation_source"] = {
                            "source_card_id": card.get("generation_parent_id"),
                            "type": "accepted_master",
                            "derivative_id": None,
                        }
                manifest["active_card_id"] = manifest["cards"][0]["id"]
                manifest["active_identity_anchors"] = {}
                manifest["last_operation"] = {
                    "id": operation_id,
                    "kind": "invalidate_all_renders",
                    "status": "complete",
                    "card_count": len(manifest["cards"]),
                    "removed_file_count": len(moved),
                    "ended_at": utc_now(),
                }
                self._commit_unlocked(manifest)
            except Exception:
                for source, staged in reversed(moved):
                    if staged.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(staged, source)
                shutil.rmtree(quarantine, ignore_errors=True)
                raise

            shutil.rmtree(quarantine, ignore_errors=True)
            for card in manifest["cards"]:
                for relative_directory in (
                    f"previews/{card['id']}",
                    f"anchors/{card['id']}",
                    f"anchor_candidates/{card['id']}",
                    f"derivatives/{card['id']}",
                ):
                    try:
                        shutil.rmtree(self.absolute_path(relative_directory), ignore_errors=True)
                    except OSError:
                        pass
            return deepcopy(manifest)

    def activate_next_invalidated(self, accepted_card_id: str) -> tuple[dict[str, Any], bool]:
        """Advance a bulk-invalidated project to its next preserved card definition."""
        with self.locked():
            manifest = self._load_unlocked()
            card = self._card(manifest, accepted_card_id)
            next_index = int(card["timeline_index"]) + 1
            if card.get("status") != "ACCEPTED" or next_index >= len(manifest["cards"]):
                return deepcopy(manifest), False
            next_card = manifest["cards"][next_index]
            if (
                next_card.get("status") != "INVALIDATED"
                or next_card.get("timeline_predecessor_id") != card["id"]
            ):
                return deepcopy(manifest), False
            if next_card.get("continuation_strategy") == "direct_mmh3":
                next_card["generation_parent_id"] = card["id"]
                next_card["continuation_source"] = self._preferred_source(card)
            manifest["active_card_id"] = next_card["id"]
            next_card["updated_at"] = utc_now()
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "activate_next_invalidated",
                "status": "complete",
                "accepted_card_id": card["id"],
                "active_card_id": next_card["id"],
                "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest), True

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

    def delete_identity_anchor(
        self, *, anchor_id: str, expected_revision: int
    ) -> dict[str, Any]:
        """Delete one saved identity checkpoint and unbind every subject using it."""
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if manifest.get("pending_operation"):
                raise ProjectError("cannot delete an identity anchor while generation is pending")
            owner = None
            anchor = None
            for card in manifest["cards"]:
                match = next(
                    (
                        item for item in card.get("anchors", [])
                        if item.get("anchor_id") == anchor_id
                        and item.get("role") == "identity"
                    ),
                    None,
                )
                if match is not None:
                    owner, anchor = card, match
                    break
            if owner is None or anchor is None:
                raise ProjectError("identity anchor does not exist")

            asset = self.absolute_path(anchor["asset_path"])
            bindings = manifest.setdefault("active_identity_anchors", {})
            unbound_subjects = [
                subject_id for subject_id, bound_id in bindings.items()
                if bound_id == anchor_id
            ]
            for subject_id in unbound_subjects:
                del bindings[subject_id]
            owner["anchors"] = [
                item for item in owner.get("anchors", [])
                if item.get("anchor_id") != anchor_id
            ]
            asset_relative = anchor["asset_path"]
            asset_still_referenced = any(
                item.get("asset_path") == asset_relative
                for card in manifest["cards"]
                for item in card.get("anchors", [])
            )
            owner["updated_at"] = utc_now()
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "delete_identity_anchor",
                "status": "complete",
                "anchor_id": anchor_id,
                "unbound_subjects": unbound_subjects,
                "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            if not asset_still_referenced:
                asset.unlink(missing_ok=True)
                try:
                    asset.parent.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    # Other checkpoint files in the card directory are preserved.
                    pass
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

    def select_draft_take(
        self, *, card_id: str, take_id: str, expected_revision: int
    ) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if manifest.get("pending_operation"):
                raise ProjectError("cannot select a draft take while an operation is pending")
            card = self._card(manifest, card_id)
            if card is not self._active_card(manifest) or card.get("status") != "DRAFT":
                raise ProjectError("draft takes can be selected only on the active DRAFT card")
            take = next(
                (item for item in card.get("draft_takes", []) if item.get("id") == take_id),
                None,
            )
            if take is None:
                raise ProjectError("draft take does not exist")
            path = self.absolute_path(take["artifact_path"])
            if not path.is_file() or sha256_file(path) != take["artifact_sha256"]:
                raise ProjectError("selected draft take archive is missing or corrupt")
            _apply_take_to_card(card, take)
            card["master_path"] = None
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def delete_draft_take(
        self, *, card_id: str, take_id: str | None, expected_revision: int,
        delete_unselected: bool = False,
    ) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if manifest.get("pending_operation"):
                raise ProjectError("cannot delete draft takes while an operation is pending")
            card = self._card(manifest, card_id)
            selected_id = card.get("selected_draft_take_id")
            takes = card.get("draft_takes", [])
            if delete_unselected:
                removed = [item for item in takes if item.get("id") != selected_id]
            else:
                if take_id == selected_id:
                    raise ProjectError("select another take before deleting the selected take")
                removed = [item for item in takes if item.get("id") == take_id]
                if not removed:
                    raise ProjectError("draft take does not exist")
            removed_ids = {item["id"] for item in removed}
            card["draft_takes"] = [item for item in takes if item.get("id") not in removed_ids]
            card["updated_at"] = utc_now()
            self._commit_unlocked(manifest)

            protected = {
                item for item in (
                    card.get("master_path"), card.get("draft_path"),
                    *((item.get("master_path")) for item in card.get("publication_history", [])),
                    *((item.get("artifact_path")) for item in card.get("draft_takes", [])),
                ) if item
            }
            protected_previews = {
                item for item in (
                    (card.get("preview") or {}).get("asset_path"),
                    *(
                        (item.get("preview") or {}).get("asset_path")
                        for item in card.get("draft_takes", [])
                    ),
                    *(
                        (item.get("preview") or {}).get("asset_path")
                        for item in card.get("publication_history", [])
                    ),
                ) if item
            }
            for take in removed:
                artifact = take.get("artifact_path")
                preview = (take.get("preview") or {}).get("asset_path")
                for relative, protected_set in (
                    (artifact, protected), (preview, protected_previews)
                ):
                    if relative and relative not in protected_set:
                        try:
                            self.absolute_path(relative).unlink(missing_ok=True)
                        except OSError:
                            pass
            return deepcopy(manifest)

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
            selected_id = card.get("selected_draft_take_id")
            selected_take = next(
                (item for item in card.get("draft_takes", []) if item.get("id") == selected_id),
                None,
            )
            if selected_take is not None:
                selected_take["preview"] = deepcopy(card["preview"])
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
        ref_image_size: str | None = None,
        continuation_strategy: str | None = None,
        refine_enabled: bool | None = None,
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
            if card["status"] not in {"EMPTY", "DRAFT", "FAILED", "INVALIDATED"}:
                raise ProjectError("accepted cards are read-only")

            previous_inputs = (
                card.get("prompt"), card.get("requested_duration_seconds"), card.get("seed"),
                card.get("ref_image_size"), card.get("continuation_strategy"),
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
            if ref_image_size is not None:
                value = str(ref_image_size)
                if value not in REF_IMAGE_SIZES:
                    raise ProjectError("ref_image_size must be match or max")
                card["ref_image_size"] = value
            if refine_enabled is not None:
                if not isinstance(refine_enabled, bool):
                    raise ProjectError("refine_enabled must be boolean")
                card["refine_enabled"] = refine_enabled
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
                    card["continuation_source"] = self._preferred_source(predecessor)
                else:
                    card["generation_parent_id"] = None
                    card["continuation_source"] = None
                card["continuation_strategy"] = strategy
            if card["status"] == "DRAFT" and previous_inputs != (
                card.get("prompt"), card.get("requested_duration_seconds"), card.get("seed"),
                card.get("ref_image_size"), card.get("continuation_strategy"),
            ):
                card["draft_inputs_dirty"] = True
            card["updated_at"] = utc_now()
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def update_project_settings(
        self,
        *,
        expected_revision: int,
        generation_mode: str,
        refine_cadence: str | None = None,
        lora_activation_words: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> dict[str, Any]:
        """Update project policy; generation mode alone locks after first render."""
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if generation_mode not in GENERATION_MODES:
                raise ProjectError("generation_mode must be ref2va or t2va")
            try:
                cadence = validate_refine_cadence(
                    manifest.get("refine_cadence", "off")
                    if refine_cadence is None
                    else refine_cadence
                )
            except RuntimeError as exc:
                raise ProjectError(str(exc)) from exc
            activation_words = validate_lora_activation_words(
                manifest.get("lora_activation_words", "")
                if lora_activation_words is None
                else lora_activation_words
            )
            pristine = (
                len(manifest["cards"]) == 1
                and manifest["cards"][0]["status"] in {"EMPTY", "FAILED"}
                and not manifest["cards"][0].get("artifact_sha256")
                and not manifest.get("pending_operation")
            )
            all_invalidated = (
                bool(manifest["cards"])
                and all(card.get("status") == "INVALIDATED" for card in manifest["cards"])
                and not manifest.get("pending_operation")
            )
            if not pristine and generation_mode != manifest["generation_mode"]:
                raise ProjectError("generation mode is locked after the first generation")
            if (width is None) != (height is None):
                raise ProjectError("project width and height must be changed together")
            requested_width = manifest["width"] if width is None else int(width)
            requested_height = manifest["height"] if height is None else int(height)
            if not 32 <= requested_width <= 8192 or not 32 <= requested_height <= 8192:
                raise ProjectError("project width and height must be between 32 and 8192")
            if requested_width % 32 or requested_height % 32:
                raise ProjectError("project width and height must be multiples of 32")
            resolution_changed = (
                requested_width != manifest["width"] or requested_height != manifest["height"]
            )
            if resolution_changed and not (pristine or all_invalidated):
                raise ProjectError("to change resolution, invalidate all cards first")
            changed = False
            if generation_mode != manifest["generation_mode"]:
                manifest["generation_mode"] = generation_mode
                changed = True
            if cadence != manifest.get("refine_cadence"):
                manifest["refine_cadence"] = cadence
                changed = True
            if resolution_changed:
                manifest["width"] = requested_width
                manifest["height"] = requested_height
                changed = True
            if activation_words != manifest.get("lora_activation_words", ""):
                if manifest.get("pending_operation"):
                    raise ProjectError(
                        "cannot change LoRA activation words while an operation is pending"
                    )
                manifest["lora_activation_words"] = activation_words
                active = self._active_card(manifest)
                if active.get("status") == "DRAFT":
                    active["draft_inputs_dirty"] = True
                    active["updated_at"] = utc_now()
                changed = True
            if changed:
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
            if target["status"] not in {"EMPTY", "DRAFT", "FAILED", "INVALIDATED"}:
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
                ref_image_size=current.get("ref_image_size", "match"),
            )
            card["continuation_source"] = self._preferred_source(current)
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

    def continuation_artifact(
        self, manifest: dict[str, Any], card: dict[str, Any]
    ) -> dict[str, Any] | None:
        if card.get("continuation_strategy") != "direct_mmh3":
            return None
        parent = self._card(manifest, card.get("generation_parent_id"))
        source = card.get("continuation_source") or self._preferred_source(parent)
        if source.get("type") == "derivative":
            try:
                derivative = self._derivative(parent, source.get("derivative_id"))
            except ProjectError:
                derivative = None
            if derivative and derivative.get("status") == "READY":
                try:
                    derivative_path = self.absolute_path(derivative["artifact_path"])
                    derivative_ready = (
                        derivative_path.is_file()
                        and sha256_file(derivative_path) == derivative["artifact_sha256"]
                    )
                except (KeyError, ProjectError, OSError):
                    derivative_ready = False
                if derivative_ready:
                    return {
                        "source_card_id": parent["id"],
                        "type": "derivative",
                        "derivative_id": derivative["id"],
                        "path": derivative["artifact_path"],
                        "artifact_sha256": derivative["artifact_sha256"],
                        "fallback": False,
                    }
        return {
            "source_card_id": parent["id"],
            "type": "accepted_master",
            "derivative_id": None,
            "path": parent["master_path"],
            "artifact_sha256": parent["artifact_sha256"],
            "fallback": source.get("type") == "derivative",
            "fallback_reason": (
                "selected refine derivative is unavailable or invalid"
                if source.get("type") == "derivative"
                else None
            ),
        }

    def set_continuation_source_preference(
        self,
        *,
        card_id: str,
        source_type: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        source_type = str(source_type)
        if source_type not in CONTINUATION_SOURCE_TYPES:
            raise ProjectError("continuation source must be accepted_master or derivative")
        with self.locked():
            manifest = self._load_unlocked()
            self._require_revision(manifest, expected_revision)
            if manifest.get("pending_operation"):
                raise ProjectError("cannot change continuation source while an operation is pending")
            card = self._card(manifest, card_id)
            if card.get("status") != "ACCEPTED":
                raise ProjectError("continuation source preference requires an accepted card")
            if source_type == "derivative" and self._latest_ready_refine(card) is None:
                raise ProjectError("this card has no ready refine derivative")
            card["continuation_source_preference"] = source_type
            active = self._active_card(manifest)
            if (
                active is not card
                and active.get("generation_parent_id") == card["id"]
                and active.get("status") in {"EMPTY", "FAILED", "INVALIDATED"}
            ):
                active["continuation_source"] = self._preferred_source(card)
                active["updated_at"] = utc_now()
            card["updated_at"] = utc_now()
            manifest["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "set_continuation_source",
                "status": "complete",
                "card_id": card["id"],
                "source_type": source_type,
                "ended_at": utc_now(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

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

    def derivative_destination(self, card: dict[str, Any], derivative_id: str) -> Path:
        try:
            uuid.UUID(str(derivative_id))
        except (ValueError, AttributeError) as exc:
            raise ProjectError("derivative_id must be a UUID") from exc
        return self.path / "derivatives" / card["id"] / f"refine_{derivative_id}.mmh3"

    def validate_artifacts(self, manifest: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for card in manifest["cards"]:
            validated_artifacts: set[tuple[str, str | None]] = set()
            relative = card.get("master_path") if card["status"] == "ACCEPTED" else card.get("draft_path")
            if relative:
                validated_artifacts.add((relative, card.get("artifact_sha256")))
                try:
                    path = self.absolute_path(relative)
                    if not path.is_file():
                        errors.append(f"card {card['artifact_number']}: missing {relative}")
                    elif sha256_file(path) != card.get("artifact_sha256"):
                        errors.append(f"card {card['artifact_number']}: hash mismatch for {relative}")
                except ProjectError as exc:
                    errors.append(f"card {card['artifact_number']}: {exc}")
            for take in card.get("draft_takes", []):
                try:
                    take_path = self.absolute_path(take["artifact_path"])
                    take_key = (take["artifact_path"], take.get("artifact_sha256"))
                    if take_key in validated_artifacts:
                        pass
                    elif not take_path.is_file():
                        errors.append(
                            f"card {card['artifact_number']}: missing draft take {take['artifact_path']}"
                        )
                    elif sha256_file(take_path) != take.get("artifact_sha256"):
                        errors.append(
                            f"card {card['artifact_number']}: draft take hash mismatch for "
                            f"{take['artifact_path']}"
                        )
                    validated_artifacts.add(take_key)
                    take_preview = take.get("preview")
                    if take_preview:
                        preview_path = self.absolute_path(take_preview["asset_path"])
                        if not preview_path.is_file():
                            errors.append(
                                f"card {card['artifact_number']}: missing draft take preview "
                                f"{take_preview['asset_path']}"
                            )
                        elif sha256_file(preview_path) != take_preview.get("asset_sha256"):
                            errors.append(
                                f"card {card['artifact_number']}: draft take preview hash mismatch "
                                f"for {take_preview['asset_path']}"
                            )
                except (KeyError, ProjectError) as exc:
                    errors.append(f"card {card['artifact_number']}: invalid draft take: {exc}")
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
            for derivative in card.get("derivatives", []):
                if derivative.get("status") != "READY":
                    continue
                try:
                    derivative_path = self.absolute_path(derivative["artifact_path"])
                    if not derivative_path.is_file():
                        errors.append(
                            f"card {card['artifact_number']}: missing refine derivative "
                            f"{derivative['artifact_path']}"
                        )
                    elif sha256_file(derivative_path) != derivative.get("artifact_sha256"):
                        errors.append(
                            f"card {card['artifact_number']}: refine derivative hash mismatch "
                            f"for {derivative['artifact_path']}"
                        )
                except (KeyError, ProjectError) as exc:
                    errors.append(
                        f"card {card['artifact_number']}: invalid refine derivative: {exc}"
                    )
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
        if manifest.get("refine_cadence") not in REFINE_CADENCES:
            raise ProjectError("project has an invalid refine_cadence")
        width = manifest.get("width")
        height = manifest.get("height")
        if (
            not isinstance(width, int) or isinstance(width, bool)
            or not isinstance(height, int) or isinstance(height, bool)
            or not 32 <= width <= 8192 or not 32 <= height <= 8192
            or width % 32 or height % 32
        ):
            raise ProjectError("project width and height must be multiples of 32 between 32 and 8192")
        validate_lora_activation_words(manifest.get("lora_activation_words"))
        cards = manifest.get("cards")
        if not isinstance(cards, list) or not cards:
            raise ProjectError("project must contain at least one card")
        ids: set[str] = set()
        accepted_numbers: set[int] = set()
        anchor_ids: set[str] = set()
        publication_ids: set[str] = set()
        derivative_ids: set[str] = set()
        take_ids: set[str] = set()
        ready_derivatives: dict[str, str] = {}
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
            if card.get("ref_image_size") not in REF_IMAGE_SIZES:
                raise ProjectError("card ref_image_size must be match or max")
            if card.get("continuation_strategy") == "independent" and parent_id is not None:
                raise ProjectError("independent cards cannot have a generation parent")
            if card.get("continuation_strategy") == "direct_mmh3" and parent_id is None:
                raise ProjectError("direct MMH3 continuation requires a generation parent")
            continuation_source = card.get("continuation_source")
            if card.get("continuation_strategy") == "independent":
                if continuation_source is not None:
                    raise ProjectError("independent cards cannot have a continuation source")
            else:
                if not isinstance(continuation_source, dict):
                    raise ProjectError("direct MMH3 continuation requires a continuation source")
                if continuation_source.get("source_card_id") != parent_id:
                    raise ProjectError("continuation source must match the generation parent")
                source_type = continuation_source.get("type")
                if source_type not in CONTINUATION_SOURCE_TYPES:
                    raise ProjectError("card has an invalid continuation source type")
                derivative_id = continuation_source.get("derivative_id")
                if source_type == "accepted_master" and derivative_id is not None:
                    raise ProjectError("accepted-master continuation cannot name a derivative")
                if source_type == "derivative" and ready_derivatives.get(derivative_id) != parent_id:
                    raise ProjectError("continuation source derivative is missing or not ready")
            preference = card.get("continuation_source_preference")
            if preference not in CONTINUATION_SOURCE_TYPES:
                raise ProjectError("card has an invalid continuation source preference")
            if card.get("reference_set") is not None and not isinstance(card.get("reference_set"), dict):
                raise ProjectError("card reference_set must be an object or null")
            if not isinstance(card.get("draft_inputs_dirty"), bool):
                raise ProjectError("card draft_inputs_dirty must be boolean")
            if not isinstance(card.get("refine_enabled"), bool):
                raise ProjectError("card refine_enabled must be boolean")
            takes = card.get("draft_takes")
            if not isinstance(takes, list):
                raise ProjectError("card draft_takes must be a list")
            selected_take_id = card.get("selected_draft_take_id")
            selected_take = None
            for take in takes:
                take_id = take.get("id")
                try:
                    uuid.UUID(take_id)
                except (ValueError, AttributeError) as exc:
                    raise ProjectError("draft take id must be a UUID") from exc
                if take_id in take_ids:
                    raise ProjectError("draft take IDs must be unique")
                take_ids.add(take_id)
                if take_id == selected_take_id:
                    selected_take = take
                if not isinstance(take.get("attempt"), int) or take["attempt"] < 0:
                    raise ProjectError("draft take attempt must be a non-negative integer")
                take_path = take.get("artifact_path")
                if not isinstance(take_path, str) or not take_path:
                    raise ProjectError("draft take artifact_path is required")
                self.relative_path(take_path)
                if not isinstance(take.get("artifact_sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", take["artifact_sha256"]
                ):
                    raise ProjectError("draft take artifact hash must be a lowercase SHA-256")
                if take.get("prompt_format") not in {"legacy_flat", "structured_v1"}:
                    raise ProjectError("draft take prompt_format is invalid")
                try:
                    validate_prompt_sections(take.get("prompt_sections"))
                except ValueError as exc:
                    raise ProjectError(f"invalid draft take: {exc}") from exc
                take_prompt = take.get("prompt")
                take_assembled = take.get("assembled_prompt")
                if not isinstance(take_prompt, str) or take_prompt != take_assembled:
                    raise ProjectError("draft take prompt must match assembled_prompt")
                if (
                    take.get("prompt_format") == "structured_v1"
                    and assemble_prompt(take["prompt_sections"]) != take_assembled
                ):
                    raise ProjectError("structured draft take assembled_prompt is stale")
                if take.get("prompt_hash") != hash_prompt(take_assembled):
                    raise ProjectError("draft take prompt_hash does not match assembled_prompt")
                if take.get("continuation_strategy") not in CONTINUATION_STRATEGIES:
                    raise ProjectError("draft take continuation_strategy is invalid")
                if take.get("ref_image_size") not in REF_IMAGE_SIZES:
                    raise ProjectError("draft take ref_image_size must be match or max")
                if take.get("recipe") is not None and not isinstance(take.get("recipe"), dict):
                    raise ProjectError("draft take recipe must be an object or null")
                take_preview = take.get("preview")
                if take_preview is not None:
                    if not isinstance(take_preview, dict):
                        raise ProjectError("draft take preview must be an object or null")
                    preview_path = take_preview.get("asset_path")
                    if not isinstance(preview_path, str) or not preview_path:
                        raise ProjectError("draft take preview asset_path is required")
                    self.relative_path(preview_path)
                    if take_preview.get("source_artifact_sha256") != take["artifact_sha256"]:
                        raise ProjectError("draft take preview source hash does not match its artifact")
                    if not isinstance(take_preview.get("asset_sha256"), str) or not re.fullmatch(
                        r"[0-9a-f]{64}", take_preview["asset_sha256"]
                    ):
                        raise ProjectError("draft take preview hash must be a lowercase SHA-256")
            if takes and selected_take is None:
                raise ProjectError("selected_draft_take_id must identify a draft take")
            if not takes and selected_take_id is not None:
                raise ProjectError("a card without draft takes cannot select one")
            if card.get("artifact_sha256"):
                if selected_take is None:
                    raise ProjectError("a completed card artifact must select a draft take")
                if selected_take.get("artifact_sha256") != card.get("artifact_sha256"):
                    raise ProjectError("selected draft take hash does not match the card artifact")
                if card.get("status") == "DRAFT" and selected_take.get("artifact_path") != card.get("draft_path"):
                    raise ProjectError("selected draft take path does not match the current draft")
                if not card.get("draft_inputs_dirty"):
                    for field in (
                        "prompt", "prompt_format", "prompt_sections", "assembled_prompt",
                        "prompt_hash", "requested_duration_seconds", "seed",
                        "continuation_strategy", "continuation_source", "recipe",
                        "generation_fingerprint", "reference_set", "context_frame_count",
                        "generated_frame_count", "actual_new_frame_count", "actual_duration_seconds",
                    ):
                        if card.get(field) != selected_take.get(field):
                            raise ProjectError(f"selected draft take does not match card field {field}")
            derivatives = card.get("derivatives")
            if not isinstance(derivatives, list):
                raise ProjectError("card derivatives must be a list")
            ready_on_card = False
            for derivative in derivatives:
                derivative_id = derivative.get("id")
                try:
                    uuid.UUID(derivative_id)
                except (ValueError, AttributeError) as exc:
                    raise ProjectError("derivative id must be a UUID") from exc
                if derivative_id in derivative_ids:
                    raise ProjectError("derivative IDs must be unique")
                derivative_ids.add(derivative_id)
                if derivative.get("type") != "refine":
                    raise ProjectError("unsupported derivative type")
                if derivative.get("status") not in {"PROCESSING", "READY", "FAILED"}:
                    raise ProjectError("invalid refine derivative status")
                if derivative.get("source_card_id") != card.get("id"):
                    raise ProjectError("refine derivative source card does not match")
                if derivative.get("cadence") not in REFINE_CADENCES:
                    raise ProjectError("refine derivative has invalid cadence")
                if not isinstance(derivative.get("recipe"), dict):
                    raise ProjectError("refine derivative recipe must be an object")
                source_path = derivative.get("source_master_path")
                if not isinstance(source_path, str) or not source_path:
                    raise ProjectError("refine derivative source master path is required")
                self.relative_path(source_path)
                if not isinstance(derivative.get("source_artifact_sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", derivative["source_artifact_sha256"]
                ):
                    raise ProjectError("refine derivative source hash must be a lowercase SHA-256")
                if derivative.get("status") == "READY":
                    path = derivative.get("artifact_path")
                    if not isinstance(path, str) or not path:
                        raise ProjectError("ready refine derivative artifact path is required")
                    self.relative_path(path)
                    if not isinstance(derivative.get("artifact_sha256"), str) or not re.fullmatch(
                        r"[0-9a-f]{64}", derivative["artifact_sha256"]
                    ):
                        raise ProjectError("ready refine derivative hash must be a lowercase SHA-256")
                    ready_derivatives[derivative_id] = card["id"]
                    ready_on_card = True
            if preference == "derivative" and not ready_on_card:
                raise ProjectError("refined continuation preference requires a ready derivative")
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
        if version not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}:
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
            card.setdefault("derivatives", [])
            card.setdefault("refine_enabled", False)
            card.setdefault("continuation_source_preference", "accepted_master")
            card.setdefault(
                "continuation_source",
                ({
                    "source_card_id": card.get("generation_parent_id"),
                    "type": "accepted_master",
                    "derivative_id": None,
                } if card.get("generation_parent_id") else None),
            )
            recipe = card.get("recipe") or {}
            recipe_ref_image_size = (
                recipe.get("ref_image_size") if isinstance(recipe, dict) else None
            )
            card.setdefault(
                "ref_image_size",
                recipe_ref_image_size if recipe_ref_image_size in REF_IMAGE_SIZES else "match",
            )
            references = recipe.get("references") if isinstance(recipe, dict) else None
            card.setdefault("reference_set", deepcopy(references) if isinstance(references, dict) else None)
            card.setdefault(
                "accepted_publication_id",
                str(uuid.uuid4()) if card.get("status") == "ACCEPTED" else None,
            )
            card.setdefault("draft_inputs_dirty", False)
            if version < 11:
                card.setdefault("draft_takes", [])
                card.setdefault("selected_draft_take_id", None)
                artifact_path = card.get("draft_path") or card.get("master_path")
                if not card["draft_takes"] and artifact_path and card.get("artifact_sha256"):
                    take = _take_from_card(card, artifact_path=artifact_path)
                    card["draft_takes"].append(take)
                    card["selected_draft_take_id"] = take["id"]
            for take in card.get("draft_takes", []):
                take_recipe = take.get("recipe") or {}
                take_ref_image_size = (
                    take_recipe.get("ref_image_size") if isinstance(take_recipe, dict) else None
                )
                take.setdefault(
                    "ref_image_size",
                    take_ref_image_size
                    if take_ref_image_size in REF_IMAGE_SIZES
                    else card["ref_image_size"],
                )
            for anchor in card["anchors"]:
                if anchor.get("role") == "identity":
                    anchor.setdefault("identity_scope", "face_only")
                    anchor.setdefault("custom_identity_instruction", None)
            previous_id = card.get("id")
        manifest.setdefault("active_identity_anchors", {})
        manifest.setdefault("refine_cadence", "off")
        manifest.setdefault("lora_activation_words", "")
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

    @staticmethod
    def _derivative(card: dict[str, Any], derivative_id: str | None) -> dict[str, Any]:
        for derivative in card.get("derivatives", []):
            if derivative.get("id") == derivative_id:
                return derivative
        raise ProjectError(f"derivative does not exist: {derivative_id}")

    @staticmethod
    def _latest_ready_refine(card: dict[str, Any]) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in reversed(card.get("derivatives", []))
                if item.get("type") == "refine" and item.get("status") == "READY"
            ),
            None,
        )

    @staticmethod
    def _preferred_source(card: dict[str, Any]) -> dict[str, Any]:
        derivative = ProjectStore._latest_ready_refine(card)
        if card.get("continuation_source_preference") == "derivative" and derivative:
            return {
                "source_card_id": card["id"],
                "type": "derivative",
                "derivative_id": derivative["id"],
            }
        return {
            "source_card_id": card["id"],
            "type": "accepted_master",
            "derivative_id": None,
        }

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
