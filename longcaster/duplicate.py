from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import shutil
import uuid
from typing import Callable

from .project import ProjectError, ProjectStore, _atomic_json, sha256_file, utc_now


ArchiveCloner = Callable[[Path, Path, str, str | None], str]


def clone_card_archive(
    source: Path,
    destination: Path,
    project_name: str,
    source_artifact_sha256: str | None = None,
) -> str:
    """Copy an MMH3 card while rebinding its LongCaster metadata."""
    from .mmh3_adapter import load_packet, mmh3_api, primary_latent

    final: Path | None = None
    try:
        packet = load_packet(source, verify="full")
        card_metadata = deepcopy(
            packet.manifest.get("extensions", {}).get("longcaster", {}).get("card")
        )
        if not isinstance(card_metadata, dict):
            raise ProjectError(f"MMH3 archive has no LongCaster card metadata: {source.name}")
        card_metadata["project_name"] = project_name
        packet = packet.set_extension_value("longcaster", "card", card_metadata)

        derivative_metadata = deepcopy(
            packet.manifest.get("extensions", {}).get("longcaster", {}).get("derivative")
        )
        if isinstance(derivative_metadata, dict) and source_artifact_sha256 is not None:
            derivative_metadata["source_artifact_sha256"] = source_artifact_sha256
            packet = packet.set_extension_value("longcaster", "derivative", derivative_metadata)

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{uuid.uuid4().hex}.{destination.name}")
        _saved, final_path = mmh3_api().save_archive(packet, str(temporary.resolve()))
        final = Path(final_path).resolve()
        verified = load_packet(final, verify="full")
        primary_latent(verified)
        if destination.exists():
            raise ProjectError(f"duplicate archive destination already exists: {destination.name}")
        os.replace(final, destination)
        return sha256_file(destination)
    except ProjectError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectError(f"could not duplicate MMH3 archive {source.name}: {exc}") from exc
    finally:
        if "temporary" in locals():
            temporary.unlink(missing_ok=True)
        if final is not None and final != destination.resolve():
            final.unlink(missing_ok=True)


def duplicate_project(
    projects_root: str | Path,
    source_name: str,
    destination_name: str,
    *,
    archive_cloner: ArchiveCloner = clone_card_archive,
    invalidate_renders: bool = False,
) -> dict:
    """Create an independent, validated copy of a LongCaster project."""
    source = ProjectStore(projects_root, source_name)
    destination = ProjectStore(projects_root, destination_name)
    if source.project_name == destination.project_name:
        raise ProjectError("duplicate project name must differ from the source")
    if destination.path.exists():
        raise ProjectError("a project with this name already exists")

    staging = destination.projects_root / f".{destination.project_name}.{uuid.uuid4().hex}.duplicate"
    with source.locked():
        manifest = source._load_unlocked()
        if manifest.get("pending_operation"):
            raise ProjectError("cannot duplicate a project while an operation is pending")
        if not invalidate_renders:
            errors = source.validate_artifacts(manifest)
            if errors:
                raise ProjectError("cannot duplicate a project with invalid artifacts: " + "; ".join(errors))

        def ignore(_directory: str, names: list[str]) -> set[str]:
            ignored = {
                name for name in names
                if name in {"project.json", ".project.lock", "transactions"}
                or name.lower().endswith(".mmh3")
            }
            if invalidate_renders and Path(_directory).resolve() == source.path:
                ignored.update(
                    name for name in names
                    if name in {
                        "clips", "drafts", "previews", "anchors",
                        "anchor_candidates", "derivatives",
                    }
                )
            return ignored

        try:
            shutil.copytree(source.path, staging, ignore=ignore)
            (staging / "transactions").mkdir(exist_ok=True)
            duplicated = deepcopy(manifest)
            duplicated["project_name"] = destination.project_name
            duplicated["revision"] = 1
            duplicated["created_at"] = utc_now()
            duplicated["updated_at"] = duplicated["created_at"]
            duplicated["pending_operation"] = None
            duplicated["last_operation"] = {
                "id": str(uuid.uuid4()),
                "kind": "duplicate_project",
                "status": "complete",
                "source_project": source.project_name,
                "source_revision": manifest["revision"],
                "ended_at": duplicated["created_at"],
            }

            if invalidate_renders:
                for index, card in enumerate(duplicated["cards"]):
                    card.update({
                        "artifact_number": index + 1,
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
                        "updated_at": duplicated["created_at"],
                        "last_error": None,
                    })
                    if card.get("continuation_strategy") == "direct_mmh3":
                        card["continuation_source"] = {
                            "source_card_id": card.get("generation_parent_id"),
                            "type": "accepted_master",
                            "derivative_id": None,
                        }
                duplicated["active_card_id"] = duplicated["cards"][0]["id"]
                duplicated["active_identity_anchors"] = {}
                duplicated["last_operation"]["kind"] = "duplicate_and_invalidate_project"
                for directory_name in (
                    "clips", "drafts", "previews", "anchors", "anchor_candidates", "derivatives"
                ):
                    directory = (staging / directory_name).resolve()
                    try:
                        directory.relative_to(staging.resolve())
                    except ValueError as exc:
                        raise ProjectError("duplicate asset directory escapes staging") from exc
                    shutil.rmtree(directory, ignore_errors=True)
                    directory.mkdir(parents=True, exist_ok=True)

                staging_store = ProjectStore(destination.projects_root, destination.project_name)
                staging_store.path = staging.resolve()
                staging_store.manifest_path = staging_store.path / "project.json"
                staging_store.lock_path = staging_store.path / ".project.lock"
                staging_store._validate(duplicated)
                _atomic_json(staging_store.manifest_path, duplicated)
                os.replace(staging, destination.path)
                return destination.load()

            primary_records: list[tuple[str, str]] = []
            for card in duplicated["cards"]:
                current_path = card.get("master_path") or card.get("draft_path")
                if current_path and card.get("artifact_sha256"):
                    primary_records.append((current_path, card["artifact_sha256"]))
                for publication in card.get("publication_history", []):
                    primary_records.append(
                        (publication["master_path"], publication["artifact_sha256"])
                    )
                for take in card.get("draft_takes", []):
                    primary_records.append(
                        (take["artifact_path"], take["artifact_sha256"])
                    )

            new_hashes: dict[str, str] = {}
            expected_hashes: dict[str, str] = {}
            for relative, expected_hash in primary_records:
                previous = expected_hashes.setdefault(relative, expected_hash)
                if previous != expected_hash:
                    raise ProjectError(f"project records conflicting hashes for {relative}")
            cloned_by_source_hash: dict[str, Path] = {}
            for relative, expected_hash in expected_hashes.items():
                source_path = source.absolute_path(relative)
                if sha256_file(source_path) != expected_hash:
                    raise ProjectError(f"source artifact changed while duplicating: {relative}")
                target_path = (staging / Path(relative)).resolve()
                try:
                    target_path.relative_to(staging.resolve())
                except ValueError as exc:
                    raise ProjectError("duplicate artifact path escapes its project") from exc
                equivalent = cloned_by_source_hash.get(expected_hash)
                if equivalent is not None:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(equivalent, target_path)
                    new_hashes[relative] = sha256_file(target_path)
                else:
                    new_hashes[relative] = archive_cloner(
                        source_path, target_path, destination.project_name, None
                    )
                    cloned_by_source_hash[expected_hash] = target_path

            for card in duplicated["cards"]:
                current_path = card.get("master_path") or card.get("draft_path")
                if current_path in new_hashes:
                    card["artifact_sha256"] = new_hashes[current_path]
                    if card.get("preview"):
                        card["preview"]["source_artifact_sha256"] = new_hashes[current_path]
                for publication in card.get("publication_history", []):
                    path = publication["master_path"]
                    publication["artifact_sha256"] = new_hashes[path]
                    if publication.get("preview"):
                        publication["preview"]["source_artifact_sha256"] = new_hashes[path]
                for take in card.get("draft_takes", []):
                    path = take["artifact_path"]
                    take["artifact_sha256"] = new_hashes[path]
                    if take.get("preview"):
                        take["preview"]["source_artifact_sha256"] = new_hashes[path]

                for derivative in card.get("derivatives", []):
                    source_path = derivative.get("source_master_path")
                    if source_path in new_hashes:
                        derivative["source_artifact_sha256"] = new_hashes[source_path]
                    artifact_path = derivative.get("artifact_path")
                    if derivative.get("status") != "READY" or not artifact_path:
                        continue
                    derivative_source = source.absolute_path(artifact_path)
                    if sha256_file(derivative_source) != derivative.get("artifact_sha256"):
                        raise ProjectError(
                            f"source derivative changed while duplicating: {artifact_path}"
                        )
                    derivative_target = (staging / Path(artifact_path)).resolve()
                    derivative["artifact_sha256"] = archive_cloner(
                        derivative_source,
                        derivative_target,
                        destination.project_name,
                        derivative.get("source_artifact_sha256"),
                    )

            staging_store = ProjectStore(destination.projects_root, destination.project_name)
            staging_store.path = staging.resolve()
            staging_store.manifest_path = staging_store.path / "project.json"
            staging_store.lock_path = staging_store.path / ".project.lock"
            staging_store._validate(duplicated)
            _atomic_json(staging_store.manifest_path, duplicated)
            os.replace(staging, destination.path)
            return destination.load()
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
