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


SCHEMA_VERSION = 1
VALID_STATES = {"EMPTY", "DRAFT", "ACCEPTED"}
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
    prompt: str,
    duration_seconds: float,
    seed: int,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "id": str(uuid.uuid4()),
        "timeline_index": timeline_index,
        "artifact_number": artifact_number,
        "generation_parent_id": parent_id,
        "status": "EMPTY",
        "prompt": prompt,
        "requested_duration_seconds": float(duration_seconds),
        "seed": int(seed),
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
        with self.locked():
            if self.manifest_path.exists():
                return self._load_unlocked()
            self.path.mkdir(parents=True, exist_ok=True)
            for directory in ("clips", "drafts", "transactions", "previews"):
                (self.path / directory).mkdir(exist_ok=True)
            first = _new_card(
                timeline_index=0,
                artifact_number=1,
                parent_id=None,
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
                "width": int(width),
                "height": int(height),
                "active_card_id": first["id"],
                "cards": [first],
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
            expected = "EMPTY" if action == "generate" else "DRAFT"
            if card["status"] != expected:
                raise ProjectError(f"{action} requires an {expected} card; current state is {card['status']}")
            operation_id = str(uuid.uuid4())
            operation = {
                "id": operation_id,
                "kind": action,
                "status": "running",
                "card_id": card["id"],
                "started_at": utc_now(),
                "owner_runtime_id": _RUNTIME_ID,
                "owner_pid": os.getpid(),
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
            card.update({
                "prompt": candidate.get("prompt", card["prompt"]),
                "requested_duration_seconds": candidate.get(
                    "requested_duration_seconds", card["requested_duration_seconds"]
                ),
                "seed": candidate.get("seed", card["seed"]),
                "recipe": deepcopy(candidate.get("recipe", card.get("recipe"))),
                "generation_fingerprint": candidate.get(
                    "generation_fingerprint", card.get("generation_fingerprint")
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
                "updated_at": utc_now(),
                "last_error": None,
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

    def accept(self) -> dict[str, Any]:
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot accept while another operation is pending")
            card = self._active_card(manifest)
            if card["status"] != "DRAFT":
                raise ProjectError(f"accept requires a DRAFT card; current state is {card['status']}")
            source = self.absolute_path(card["draft_path"])
            if not source.is_file() or sha256_file(source) != card["artifact_sha256"]:
                raise ProjectError("draft archive is missing or does not match its recorded hash")
            relative_destination = f"clips/card_{card['artifact_number']:04d}.mmh3"
            destination = self.absolute_path(relative_destination)
            if destination.exists():
                raise ProjectError(f"accepted master already exists and will not be overwritten: {relative_destination}")

            transaction = {
                "schema_version": 1,
                "kind": "accept",
                "card_id": card["id"],
                "source": card["draft_path"],
                "destination": relative_destination,
                "sha256": card["artifact_sha256"],
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
                artifact_number=max(item["artifact_number"] for item in manifest["cards"]) + 1,
                parent_id=current["id"],
                prompt=prompt,
                duration_seconds=duration_seconds,
                seed=seed,
            )
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
        return errors

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.manifest_path.is_file():
            raise ProjectError(f"project does not exist: {self.project_name}")
        try:
            with self.manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"cannot read project manifest: {exc}") from exc
        self._validate(manifest)
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
        cards = manifest.get("cards")
        if not isinstance(cards, list) or not cards:
            raise ProjectError("project must contain at least one card")
        ids: set[str] = set()
        accepted_numbers: set[int] = set()
        for index, card in enumerate(cards):
            if card.get("id") in ids:
                raise ProjectError("duplicate card id in project manifest")
            ids.add(card.get("id"))
            if card.get("timeline_index") != index:
                raise ProjectError("card timeline indexes are not contiguous")
            if card.get("status") not in VALID_STATES:
                raise ProjectError(f"invalid card state: {card.get('status')}")
            if card.get("status") == "ACCEPTED":
                number = card.get("artifact_number")
                if number in accepted_numbers:
                    raise ProjectError("duplicate accepted artifact number")
                accepted_numbers.add(number)
        if manifest.get("active_card_id") not in ids:
            raise ProjectError("active card does not exist")

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
        card.update({
            "status": "ACCEPTED",
            "master_path": transaction["destination"],
            "draft_path": None,
            "artifact_sha256": transaction["sha256"],
            "accepted_at": utc_now(),
            "updated_at": utc_now(),
            "last_error": None,
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
                    self._finish_accept_unlocked(manifest, transaction)
                    journal.unlink(missing_ok=True)
                    changed = True
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                raise ProjectError(f"cannot recover transaction {journal.name}: {exc}") from exc
        return changed
