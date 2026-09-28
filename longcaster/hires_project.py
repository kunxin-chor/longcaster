"""Persistent hires bookkeeping, separate from Stage 3A source selection."""
from __future__ import annotations

from copy import deepcopy
import os
import uuid

from .hires import AUDIO_HANDLING, HIRES_CADENCES, resolve_target


class HiresProjectMixin:
    def set_hires_cadence(self, cadence, *, expected_revision=None):
        from .project import ProjectError
        if cadence not in HIRES_CADENCES:
            raise ProjectError("hires cadence must be off or manual; auto awaits seam validation")
        with self.locked():
            manifest = self._load_unlocked()
            if expected_revision is not None and manifest["revision"] != expected_revision:
                raise ProjectError("project changed; refresh before saving hires cadence")
            if manifest.get("pending_operation"):
                raise ProjectError("cannot change hires cadence during a pending operation")
            if manifest.get("hires_cadence") != cadence:
                manifest["hires_cadence"] = cadence
                self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def latest_ready_hires(self, manifest, card, *, verify=True):
        """Validate native source and the entire HR ancestry, failing closed."""
        from .project import ProjectError, sha256_file
        current = self._card(manifest, card["id"])
        derivative = next((d for d in reversed(current.get("derivatives", []))
                           if d.get("type") == "hires_refine" and d.get("status") == "READY"), None)
        if derivative is None:
            return None
        if (current.get("status") != "ACCEPTED"
                or derivative.get("source_master_path") != current.get("master_path")
                or derivative.get("source_artifact_sha256") != current.get("artifact_sha256")):
            raise ProjectError("hires derivative has a stale native source")
        if verify:
            for path_key, hash_key, record in (
                ("master_path", "artifact_sha256", current),
                ("artifact_path", "artifact_sha256", derivative),
            ):
                path = self.absolute_path(record[path_key])
                if not path.is_file() or sha256_file(path) != record[hash_key]:
                    raise ProjectError(f"Card {current['timeline_index'] + 1} hires source/artifact hash mismatch")
        parent = self.hires_parent(manifest, current, verify=verify)
        if (derivative.get("hires_parent_derivative_id") != (parent["id"] if parent else None)
                or derivative.get("hires_parent_sha256") != (parent["artifact_sha256"] if parent else None)):
            raise ProjectError("hires-parent identity or hash mismatch")
        if parent and any(parent[k] != derivative[k] for k in ("target_width", "target_height")):
            raise ProjectError("hires parent and child resolutions do not match")
        return derivative

    def hires_parent(self, manifest, card, *, verify=True):
        from .project import ProjectError
        if card.get("continuation_strategy") != "direct_mmh3":
            return None
        parent = self._card(manifest, card.get("generation_parent_id"))
        if parent["timeline_index"] >= card["timeline_index"]:
            raise ProjectError("hires generation parent must be an earlier card")
        derivative = self.latest_ready_hires(manifest, parent, verify=verify)
        if derivative is None:
            raise ProjectError(
                f"Card {card['timeline_index'] + 1} high-resolution upscale requires a ready "
                f"Card {parent['timeline_index'] + 1} high-resolution derivative."
            )
        return derivative

    def begin_hires_refine(self, *, card_id, recipe, upscaler_model, scale=2.0,
                           target_width=0, target_height=0, temporal_chunking=True,
                           precision="bf16"):
        from .project import ProjectError, sha256_file, utc_now, _RUNTIME_ID
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("another project operation is already pending")
            if manifest.get("hires_cadence") == "off":
                raise ProjectError("High Resolution is Off")
            card = self._card(manifest, card_id)
            if card.get("status") != "ACCEPTED":
                raise ProjectError("high-resolution upscale requires an accepted card")
            source = self.absolute_path(card["master_path"])
            if not source.is_file() or sha256_file(source) != card["artifact_sha256"]:
                raise ProjectError("accepted master is missing or corrupt")
            target = resolve_target(manifest["width"], manifest["height"], scale=scale,
                                    target_width=target_width, target_height=target_height)
            parent = self.hires_parent(manifest, card)
            if parent and any(parent[k] != target[k] for k in ("target_width", "target_height")):
                raise ProjectError("hires target resolution must match the preceding hires derivative")
            if precision not in {"bf16", "fp16"} or not isinstance(temporal_chunking, bool):
                raise ProjectError("invalid hires precision or temporal chunking")
            if not isinstance(upscaler_model, str) or not upscaler_model or not isinstance(recipe, dict):
                raise ProjectError("hires requires an upscaler model and recipe")
            now = utc_now()
            derivative = {
                "id": str(uuid.uuid4()), "type": "hires_refine", "status": "PROCESSING",
                "source_card_id": card_id, "source_master_path": card["master_path"],
                "source_artifact_sha256": card["artifact_sha256"], **target,
                "upscaler_model": upscaler_model, "upscaler_version": "lbh_3d_temporal32_v1",
                "upscaler_config": None, "temporal_chunking": temporal_chunking,
                "precision": precision, "force_unload": True,
                "hires_parent_derivative_id": parent["id"] if parent else None,
                "hires_parent_sha256": parent["artifact_sha256"] if parent else None,
                "recipe": deepcopy(recipe), "artifact_path": None, "artifact_sha256": None,
                "prefix_protection": None, "audio_handling": AUDIO_HANDLING,
                "created_at": now, "started_at": now, "completed_at": None,
                "execution_seconds": None, "last_error": None,
            }
            card["derivatives"].append(derivative)
            manifest["pending_operation"] = {
                "id": str(uuid.uuid4()), "kind": "hires_refine", "status": "running",
                "card_id": card_id, "derivative_id": derivative["id"], "started_at": now,
                "owner_runtime_id": _RUNTIME_ID, "owner_pid": os.getpid(),
            }
            self._commit_unlocked(manifest)
            return deepcopy(manifest), deepcopy(derivative)

    def hires_destination(self, card, derivative_id):
        uuid.UUID(str(derivative_id))
        return self.path / "derivatives" / card["id"] / f"hires_{derivative_id}.mmh3"

    def finish_hires_refine(self, *, operation_id, derivative_path, artifact_sha256,
                            execution_seconds, prefix_protection, diagnostics=None):
        from .project import ProjectError, sha256_file, utc_now
        with self.locked():
            manifest = self._load_unlocked()
            pending = manifest.get("pending_operation") or {}
            if pending.get("id") != operation_id or pending.get("kind") != "hires_refine":
                raise ProjectError("hires result is stale or no longer pending")
            card = self._card(manifest, pending["card_id"])
            derivative = self._derivative(card, pending["derivative_id"])
            parent = self.hires_parent(manifest, card)
            if (card.get("status") != "ACCEPTED"
                    or card.get("master_path") != derivative["source_master_path"]
                    or card.get("artifact_sha256") != derivative["source_artifact_sha256"]
                    or sha256_file(self.absolute_path(card["master_path"])) != derivative["source_artifact_sha256"]):
                raise ProjectError("accepted source changed while hires was running")
            if (derivative["hires_parent_derivative_id"] != (parent["id"] if parent else None)
                    or derivative["hires_parent_sha256"] != (parent["artifact_sha256"] if parent else None)):
                raise ProjectError("hires parent changed while sampling")
            relative = self.relative_path(derivative_path)
            if relative != self.relative_path(self.hires_destination(card, derivative["id"])):
                raise ProjectError("hires artifact must use its own derivative destination")
            path = self.absolute_path(relative)
            if not path.is_file() or sha256_file(path) != artifact_sha256:
                raise ProjectError("hires derivative is missing or its hash does not match")
            for old in card["derivatives"]:
                if old["type"] == "hires_refine" and old["status"] == "READY":
                    old.update(status="INVALIDATED", last_error="Replaced by a newer hires derivative")
            derivative.update(status="READY", artifact_path=relative,
                              artifact_sha256=artifact_sha256, completed_at=utc_now(),
                              execution_seconds=max(0, float(execution_seconds)),
                              prefix_protection=deepcopy(prefix_protection),
                              diagnostics=deepcopy(diagnostics or {}), last_error=None)
            if diagnostics:
                for key in ("upscaler_config", "upscaler_sha256", "precision"):
                    if key in diagnostics:
                        derivative[key] = deepcopy(diagnostics[key])
            manifest["last_operation"] = {k: v for k, v in pending.items() if k != "owner_runtime_id"}
            manifest["last_operation"].update(status="complete", ended_at=utc_now())
            manifest["pending_operation"] = None
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    def fail_hires_refine(self, operation_id, error):
        from .project import utc_now
        with self.locked():
            manifest = self._load_unlocked()
            pending = manifest.get("pending_operation") or {}
            if pending.get("id") != operation_id or pending.get("kind") != "hires_refine":
                return None
            card = self._card(manifest, pending["card_id"])
            self._derivative(card, pending["derivative_id"]).update(
                status="FAILED", completed_at=utc_now(), last_error=str(error)[:4000])
            manifest["last_operation"] = {k: v for k, v in pending.items() if k != "owner_runtime_id"}
            manifest["last_operation"].update(status="failed", ended_at=utc_now(), error=str(error)[:4000])
            manifest["pending_operation"] = None
            self._commit_unlocked(manifest)
            return deepcopy(manifest)

    @staticmethod
    def _invalidate_stale_hires(manifest):
        """Topological pass: no disk I/O, no mutation of native cards or sources."""
        ready = {}
        changed = False
        for card in manifest["cards"]:
            for derivative in card.get("derivatives", []):
                if derivative.get("type") != "hires_refine" or derivative.get("status") != "READY":
                    continue
                parent_id = derivative.get("hires_parent_derivative_id")
                parent = ready.get(parent_id)
                direct = card.get("continuation_strategy") == "direct_mmh3"
                valid = (card.get("status") == "ACCEPTED"
                         and card.get("master_path") == derivative.get("source_master_path")
                         and card.get("artifact_sha256") == derivative.get("source_artifact_sha256"))
                if direct:
                    valid = valid and bool(parent) and (
                        parent["source_card_id"] == card.get("generation_parent_id")
                        and parent["artifact_sha256"] == derivative.get("hires_parent_sha256")
                        and parent["target_width"] == derivative.get("target_width")
                        and parent["target_height"] == derivative.get("target_height"))
                else:
                    valid = valid and parent_id is None and derivative.get("hires_parent_sha256") is None
                if not valid:
                    derivative.update(status="INVALIDATED", last_error="Native source or hires ancestry changed")
                    changed = True
                else:
                    ready[derivative["id"]] = derivative
        return changed

    def delete_hires(self, *, card_id, expected_revision=None):
        from .project import ProjectError
        with self.locked():
            manifest = self._load_unlocked()
            if manifest.get("pending_operation"):
                raise ProjectError("cannot delete hires during a pending operation")
            if expected_revision is not None and manifest["revision"] != expected_revision:
                raise ProjectError("project changed; refresh before deleting hires")
            card = self._card(manifest, card_id)
            removed = [d for d in card["derivatives"] if d["type"] == "hires_refine"]
            card["derivatives"] = [d for d in card["derivatives"] if d["type"] != "hires_refine"]
            self._commit_unlocked(manifest)
            for derivative in removed:
                for relative in (derivative.get("artifact_path"), (derivative.get("preview") or {}).get("asset_path")):
                    if relative:
                        path = self.absolute_path(relative)
                        # Never allow corrupted metadata to delete a native master.
                        directory = (self.path / "derivatives" / card["id"]).resolve()
                        if path.resolve().is_relative_to(directory):
                            path.unlink(missing_ok=True)
            return deepcopy(manifest)

    def record_hires_preview(self, *, card_id, preview_path, preview_sha256, source_artifact_sha256):
        from .project import ProjectError, sha256_file
        with self.locked():
            manifest = self._load_unlocked()
            card = self._card(manifest, card_id)
            derivative = self.latest_ready_hires(manifest, card)
            if derivative is None or derivative["artifact_sha256"] != source_artifact_sha256:
                raise ProjectError("hires preview source changed while decoding")
            relative = self.relative_path(preview_path)
            expected = self.relative_path(self.hires_destination(card, derivative["id"]).with_suffix(".mp4"))
            if relative != expected or sha256_file(self.absolute_path(relative)) != preview_sha256:
                raise ProjectError("hires preview destination or hash mismatch")
            derivative["preview"] = {"asset_path": relative, "asset_sha256": preview_sha256,
                                     "source_artifact_sha256": source_artifact_sha256}
            self._commit_unlocked(manifest)
            return deepcopy(manifest)
