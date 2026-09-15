import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from longcaster.duplicate import clone_card_archive, duplicate_project
from longcaster.fingerprint import generation_fingerprint
from longcaster.project import ProjectError, ProjectStore, sha256_file
from longcaster.prompt_sections import assemble_prompt, edit_sections, empty_prompt_sections, hash_prompt


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ProjectStore(self.root, "film_01")
        self.manifest = self.store.create(
            prompt="opening",
            duration_seconds=5.0,
            seed=11,
            width=1344,
            height=768,
            generation_mode="ref2va",
        )

    def tearDown(self):
        self.temp.cleanup()

    def _draft(self, action="generate", body=b"valid mmh3 stand-in", *, prompt="opening", seed=11, duration=5.0, ref_image_size=None):
        recipe = {"sigmas": [12, 0]}
        if ref_image_size is not None:
            recipe["ref_image_size"] = ref_image_size
        fingerprint = generation_fingerprint({"seed": seed, **recipe})
        manifest, card = self.store.begin_generation(
            action=action,
            prompt=prompt,
            duration_seconds=duration,
            seed=seed,
            recipe=recipe,
            fingerprint=fingerprint,
        )
        destination = self.store.draft_destination(card)
        destination.write_bytes(body)
        return self.store.finish_generation(
            operation_id=manifest["pending_operation"]["id"],
            draft_path=destination,
            artifact_sha256=sha256_file(destination),
            context_frame_count=0,
            generated_frame_count=124,
            actual_new_frame_count=124,
            actual_duration_seconds=124 / 24,
        )

    def test_generate_accept_append_state_machine(self):
        manifest = self._draft()
        draft = self.store.active_card(manifest)
        self.assertEqual(draft["status"], "DRAFT")

        manifest = self.store.accept()
        accepted = self.store.active_card(manifest)
        self.assertEqual(accepted["status"], "ACCEPTED")
        master = self.store.absolute_path(accepted["master_path"])
        self.assertTrue(master.is_file())
        self.assertEqual(sha256_file(master), accepted["artifact_sha256"])

        manifest = self.store.append(prompt="next", duration_seconds=5, seed=12)
        current = self.store.active_card(manifest)
        self.assertEqual(current["status"], "EMPTY")
        self.assertEqual(current["generation_parent_id"], accepted["id"])
        self.assertEqual(current["timeline_predecessor_id"], accepted["id"])

    def test_new_project_dimensions_must_be_multiples_of_32(self):
        store = ProjectStore(self.root, "invalid_resolution")
        with self.assertRaisesRegex(ProjectError, "multiples of 32"):
            store.create(
                prompt="",
                duration_seconds=5.0,
                seed=1,
                width=833,
                height=480,
                generation_mode="ref2va",
            )

    def test_ref_image_size_is_per_card_dirty_and_frozen_per_take(self):
        self.assertEqual(self.store.active_card(self.manifest)["ref_image_size"], "match")
        first = self._draft(ref_image_size="match")
        card = self.store.active_card(first)
        first_take = card["draft_takes"][0]
        self.assertEqual(first_take["ref_image_size"], "match")

        changed = self.store.update_card_editor(
            card_id=card["id"], expected_revision=first["revision"], ref_image_size="max",
        )
        self.assertTrue(self.store.active_card(changed)["draft_inputs_dirty"])
        retried = self._draft(action="retry", body=b"max take", ref_image_size="max")
        card = self.store.active_card(retried)
        self.assertEqual(card["ref_image_size"], "max")
        self.assertEqual(card["draft_takes"][-1]["ref_image_size"], "max")
        self.assertNotEqual(
            card["draft_takes"][0]["generation_fingerprint"],
            card["draft_takes"][-1]["generation_fingerprint"],
        )
        self.assertFalse(card["draft_inputs_dirty"])

        selected = self.store.select_draft_take(
            card_id=card["id"], take_id=first_take["id"],
            expected_revision=retried["revision"],
        )
        self.assertEqual(self.store.active_card(selected)["ref_image_size"], "match")

    def test_ref_image_size_validation_and_append_inheritance(self):
        card = self.store.active_card(self.manifest)
        with self.assertRaisesRegex(ProjectError, "match or max"):
            self.store.update_card_editor(
                card_id=card["id"], expected_revision=self.manifest["revision"],
                ref_image_size="largest",
            )
        manifest = self.store.update_card_editor(
            card_id=card["id"], expected_revision=self.manifest["revision"],
            ref_image_size="max",
        )
        self._draft(ref_image_size="max")
        accepted = self.store.accept()
        appended = self.store.append(prompt="next", duration_seconds=5, seed=12)
        self.assertEqual(self.store.active_card(appended)["ref_image_size"], "max")

    def test_schema_twelve_migrates_ref_image_size_from_recipe(self):
        manifest = self._draft(ref_image_size="max")
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 12
        card = document["cards"][0]
        card.pop("ref_image_size", None)
        for take in card["draft_takes"]:
            take.pop("ref_image_size", None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")

        migrated = self.store.load()
        card = self.store.active_card(migrated)
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(card["ref_image_size"], "max")
        self.assertEqual(card["draft_takes"][0]["ref_image_size"], "max")

    def test_unpublish_tail_preserves_master_and_allows_retry(self):
        self._draft(body=b"published version")
        manifest = self.store.accept()
        accepted = self.store.active_card(manifest)
        old_master = self.store.absolute_path(accepted["master_path"])
        old_hash = accepted["artifact_sha256"]

        manifest = self.store.unpublish_tail()
        reopened = self.store.active_card(manifest)
        self.assertEqual(reopened["status"], "DRAFT")
        self.assertEqual(reopened["artifact_number"], 2)
        self.assertIsNone(reopened["master_path"])
        self.assertTrue(old_master.is_file())
        self.assertEqual(sha256_file(old_master), old_hash)
        self.assertEqual(len(reopened["publication_history"]), 1)
        publication = reopened["publication_history"][0]
        self.assertEqual(publication["artifact_number"], 1)
        self.assertEqual(publication["master_path"], "clips/card_0001.mmh3")
        self.assertEqual(publication["prompt_hash"], accepted["prompt_hash"])
        self.assertEqual(publication["generation_fingerprint"], accepted["generation_fingerprint"])
        self.assertEqual(publication["seed"], accepted["seed"])
        self.assertEqual(self.store.validate_artifacts(manifest), [])
        copied_draft = self.store.absolute_path(reopened["draft_path"])
        self.assertTrue(copied_draft.is_file())
        self.assertEqual(sha256_file(copied_draft), old_hash)

        manifest = self._draft(action="retry", body=b"replacement version")
        replacement = self.store.active_card(manifest)
        self.assertTrue(copied_draft.exists())
        self.assertEqual(len(replacement["draft_takes"]), 2)
        self.assertEqual(replacement["status"], "DRAFT")
        manifest = self.store.accept()
        replacement = self.store.active_card(manifest)
        self.assertEqual(replacement["master_path"], "clips/card_0002.mmh3")
        self.assertTrue(old_master.is_file())
        self.assertTrue(self.store.absolute_path(replacement["master_path"]).is_file())

    def test_unpublish_rejects_nonaccepted_active_card(self):
        with self.assertRaises(ProjectError):
            self.store.unpublish_tail()

    def test_remove_unpublished_draft_restores_previous_card_for_editing(self):
        self._draft(body=b"card one")
        manifest = self.store.accept()
        first = self.store.active_card(manifest)
        manifest = self.store.append(prompt="card two", duration_seconds=5, seed=12)
        manifest = self._draft(body=b"card two publication")
        manifest = self.store.accept()
        second = self.store.active_card(manifest)
        second_master = self.store.absolute_path(second["master_path"])

        manifest = self.store.unpublish_tail()
        second_draft = self.store.absolute_path(self.store.active_card(manifest)["draft_path"])
        self.assertTrue(second_master.is_file())
        self.assertTrue(second_draft.is_file())

        manifest = self.store.remove_draft_tail()
        self.assertEqual(len(manifest["cards"]), 1)
        self.assertEqual(self.store.active_card(manifest)["id"], first["id"])
        self.assertEqual(self.store.active_card(manifest)["status"], "ACCEPTED")
        self.assertFalse(second_master.exists())
        self.assertFalse(second_draft.exists())
        self.assertEqual(manifest["last_operation"]["removed_publication_count"], 1)

        manifest = self.store.unpublish_tail()
        self.assertEqual(self.store.active_card(manifest)["status"], "DRAFT")

    def test_accept_records_anchor_without_changing_master_hash(self):
        manifest = self._draft()
        card = self.store.active_card(manifest)
        anchor_id = "ae4f2b4c-82ea-4d4c-9f95-319039345594"
        anchor_path = self.store.path / "anchors" / card["id"] / f"{anchor_id}.png"
        anchor_path.parent.mkdir(parents=True, exist_ok=True)
        anchor_path.write_bytes(b"lossless-anchor")
        anchor = {
            "anchor_id": anchor_id,
            "source_card_id": card["id"],
            "source_frame_index": 123,
            "source_timestamp_seconds": 123 / 24,
            "role": "current_state",
            "asset_path": self.store.relative_path(anchor_path),
            "asset_sha256": sha256_file(anchor_path),
            "media_type": "image/png",
            "created_at": "2026-09-11T00:00:00Z",
            "enabled": True,
            "mode": "MiniMaxH3AddGuide/minimax_keyframes",
        }
        accepted_manifest = self.store.accept(anchor=anchor)
        accepted = self.store.active_card(accepted_manifest)
        self.assertEqual(accepted["anchors"], [anchor])
        self.assertEqual(sha256_file(self.store.absolute_path(accepted["master_path"])), accepted["artifact_sha256"])
        anchor_path.write_bytes(b"changed-anchor")
        self.assertTrue(any("anchor hash mismatch" in error for error in self.store.validate_artifacts(accepted_manifest)))

    def test_project_preview_is_recorded_for_the_current_artifact(self):
        manifest = self._draft()
        card = self.store.active_card(manifest)
        preview = self.store.path / "previews" / card["id"] / "card_preview.mp4"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"derived mp4 preview")
        manifest = self.store.record_preview(
            card_id=card["id"],
            preview_path=preview,
            preview_sha256=sha256_file(preview),
            source_artifact_sha256=card["artifact_sha256"],
        )
        recorded = self.store.active_card(manifest)["preview"]
        self.assertEqual(recorded["asset_path"], self.store.relative_path(preview))
        self.assertEqual(recorded["source_artifact_sha256"], card["artifact_sha256"])
        with self.assertRaises(ProjectError):
            self.store.record_preview(
                card_id=card["id"],
                preview_path=preview,
                preview_sha256=sha256_file(preview),
                source_artifact_sha256="0" * 64,
            )
        pending, _ = self.store.begin_generation(
            action="retry", prompt="opening", duration_seconds=5.0, seed=11,
            recipe={"sigmas": [12, 0]}, fingerprint=generation_fingerprint({"retry": 1}),
        )
        self.store.fail_generation(pending["pending_operation"]["id"], "retry failed")
        self.assertEqual(
            self.store.active_card(self.store.load())["preview"]["asset_sha256"],
            recorded["asset_sha256"],
        )
        replacement = self._draft(action="retry", body=b"replacement mmh3")
        self.assertIsNone(self.store.active_card(replacement)["preview"])

    def test_schema_one_project_migrates_with_empty_anchor_lists(self):
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        for card in document["cards"]:
            card.pop("anchors", None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        migrated = self.store.load()
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(migrated["cards"][0]["publication_history"], [])
        self.assertEqual(migrated["cards"][0]["anchors"], [])
        self.assertEqual(migrated["active_identity_anchors"], {})

    def test_schema_two_project_preserves_current_state_anchors(self):
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 2
        document.pop("active_identity_anchors", None)
        existing = {
            "anchor_id": "a6a81a1a-3e95-452d-9e38-c523e596420e",
            "source_card_id": document["cards"][0]["id"],
            "source_frame_index": 1,
            "source_timestamp_seconds": 1 / 24,
            "role": "current_state",
            "asset_path": "anchors/existing.png",
            "asset_sha256": "a" * 64,
            "media_type": "image/png",
            "created_at": "2026-09-11T00:00:00Z",
            "enabled": True,
            "mode": "MiniMaxH3AddGuide/minimax_keyframes",
        }
        document["cards"][0]["anchors"] = [existing]
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        migrated = self.store.load()
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(migrated["cards"][0]["anchors"], [existing])
        self.assertEqual(migrated["active_identity_anchors"], {})

    def test_schema_three_project_adds_publication_history(self):
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 3
        for card in document["cards"]:
            card.pop("publication_history", None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        migrated = self.store.load()
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(migrated["cards"][0]["publication_history"], [])

    def test_identity_anchor_binding_survives_disable_enable_clear_and_restart(self):
        manifest = self._draft()
        manifest = self.store.accept()
        card = self.store.active_card(manifest)
        anchor_id = "27a5d7a0-df0c-43b4-8f06-fdc7a96372ab"
        anchor_path = self.store.path / "anchors" / card["id"] / f"{anchor_id}.png"
        anchor_path.parent.mkdir(parents=True, exist_ok=True)
        anchor_path.write_bytes(b"identity-anchor")
        anchor = {
            "anchor_id": anchor_id,
            "role": "identity",
            "source_card_id": card["id"],
            "source_preview_frame_index": 23,
            "source_frame_index": 23,
            "source_preview_timestamp_seconds": 23 / 24,
            "source_timestamp_seconds": 23 / 24,
            "source_timestamp_sec": 23 / 24,
            "asset_path": self.store.relative_path(anchor_path),
            "asset_sha256": sha256_file(anchor_path),
            "media_type": "image/png",
            "created_at": "2026-09-11T00:00:00Z",
            "enabled": True,
            "subject_id": "<Subject 1>",
            "label": "clean face",
            "mode": "MiniMaxH3ReferenceToVideo/minimax_refs",
            "strength": None,
        }
        manifest = self.store.add_identity_anchor(card["id"], anchor)
        self.assertEqual(self.store.active_identity_anchor(manifest)["anchor_id"], anchor_id)
        self.assertEqual(self.store.active_identity_anchor(manifest)["identity_scope"], "face_only")
        restarted = ProjectStore(self.root, "film_01")
        manifest = restarted.load()
        self.assertEqual(restarted.active_identity_anchor(manifest)["label"], "clean face")
        manifest = restarted.set_identity_anchor_enabled("<Subject 1>", False)
        self.assertIsNone(restarted.active_identity_anchor(manifest))
        self.assertFalse(restarted.active_identity_anchor(manifest, include_disabled=True)["enabled"])
        manifest = restarted.set_identity_anchor_enabled("<Subject 1>", True)
        self.assertTrue(restarted.active_identity_anchor(manifest)["enabled"])
        manifest = restarted.clear_identity_anchor("<Subject 1>")
        self.assertIsNone(restarted.active_identity_anchor(manifest, include_disabled=True))
        self.assertTrue(anchor_path.is_file())
        manifest = restarted.bind_identity_anchor(
            subject_id="<Subject 1>", anchor_id=anchor_id,
            expected_revision=manifest["revision"],
        )
        self.assertEqual(restarted.active_identity_anchor(manifest)["anchor_id"], anchor_id)
        manifest = restarted.delete_identity_anchor(
            anchor_id=anchor_id,
            expected_revision=manifest["revision"],
        )
        self.assertIsNone(restarted.active_identity_anchor(manifest, include_disabled=True))
        self.assertFalse(anchor_path.exists())
        self.assertFalse(any(
            item.get("anchor_id") == anchor_id
            for item in restarted.active_card(manifest)["anchors"]
        ))
        self.assertEqual(manifest["last_operation"]["kind"], "delete_identity_anchor")

    def test_identity_anchor_rejects_unaccepted_source(self):
        card = self.store.active_card(self._draft())
        with self.assertRaisesRegex(ProjectError, "accepted source"):
            self.store.add_identity_anchor(card["id"], {"role": "identity"})

    def test_schema_four_identity_anchor_migrates_to_face_only(self):
        manifest = self._draft()
        manifest = self.store.accept()
        card = self.store.active_card(manifest)
        anchor_id = "33b36c72-c7cc-4fc9-94df-40cc5497c875"
        anchor_path = self.store.path / "anchors" / card["id"] / f"{anchor_id}.png"
        anchor_path.parent.mkdir(parents=True, exist_ok=True)
        anchor_path.write_bytes(b"legacy-identity-anchor")
        anchor = {
            "anchor_id": anchor_id,
            "role": "identity",
            "source_card_id": card["id"],
            "source_preview_frame_index": 0,
            "source_frame_index": 0,
            "source_timestamp_seconds": 0.0,
            "asset_path": self.store.relative_path(anchor_path),
            "asset_sha256": sha256_file(anchor_path),
            "created_at": "2026-09-11T00:00:00Z",
            "enabled": True,
            "subject_id": "<Subject 1>",
            "mode": "MiniMaxH3ReferenceToVideo/minimax_refs",
        }
        manifest = self.store.add_identity_anchor(card["id"], anchor)
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 4
        stored = document["cards"][0]["anchors"][-1]
        stored.pop("identity_scope", None)
        stored.pop("custom_identity_instruction", None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")

        migrated = self.store.load()
        active = self.store.active_identity_anchor(migrated)
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(active["identity_scope"], "face_only")
        self.assertIsNone(active["custom_identity_instruction"])

    def test_card_uuid_is_stable_across_restart(self):
        original_id = self.store.active_card(self.manifest)["id"]
        restarted = ProjectStore(self.root, "film_01").load()
        self.assertEqual(self.store.active_card(restarted)["id"], original_id)

    def test_illegal_transitions_are_rejected(self):
        with self.assertRaises(ProjectError):
            self.store.accept()
        with self.assertRaises(ProjectError):
            self.store.append(prompt="", duration_seconds=5, seed=1)
        with self.assertRaises(ProjectError):
            self.store.begin_generation(
                action="retry", prompt="", duration_seconds=5, seed=1,
                recipe={}, fingerprint=generation_fingerprint({}),
            )

    def test_retry_is_non_destructive_until_new_draft_commits(self):
        first = self._draft()
        original = self.store.active_card(first)
        pending, _ = self.store.begin_generation(
            action="retry",
            prompt="changed",
            duration_seconds=6,
            seed=99,
            recipe={"sigmas": [12, 1, 0]},
            fingerprint=generation_fingerprint({"attempt": 2}),
        )
        self.store.fail_generation(pending["pending_operation"]["id"], "GPU stopped")
        recovered = self.store.load()
        card = self.store.active_card(recovered)
        self.assertEqual(card["status"], "DRAFT")
        self.assertEqual(card["draft_path"], original["draft_path"])
        self.assertEqual(card["prompt"], original["prompt"])
        self.assertEqual(card["seed"], original["seed"])
        self.assertEqual(card["generation_fingerprint"], original["generation_fingerprint"])
        self.assertTrue(self.store.absolute_path(card["draft_path"]).is_file())

    def test_retry_retains_takes_and_selecting_one_restores_exact_generation(self):
        first_manifest = self._draft(body=b"first take", prompt="first prompt", seed=11)
        first_card = self.store.active_card(first_manifest)
        first_take = first_card["draft_takes"][0]
        preview = self.store.path / "previews" / first_card["id"] / "first.mp4"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"first preview")
        first_manifest = self.store.record_preview(
            card_id=first_card["id"], preview_path=preview,
            preview_sha256=sha256_file(preview),
            source_artifact_sha256=first_card["artifact_sha256"],
        )

        second_manifest = self._draft(
            action="retry", body=b"second take", prompt="second prompt", seed=99, duration=6.0,
        )
        second_card = self.store.active_card(second_manifest)
        self.assertEqual(len(second_card["draft_takes"]), 2)
        self.assertEqual(second_card["selected_draft_take_id"], second_card["draft_takes"][1]["id"])
        self.assertTrue(self.store.absolute_path(first_take["artifact_path"]).is_file())
        self.assertTrue(preview.is_file())
        self.assertIsNotNone(second_card["draft_takes"][0]["preview"])

        selected = self.store.select_draft_take(
            card_id=second_card["id"], take_id=first_take["id"],
            expected_revision=second_manifest["revision"],
        )
        selected_card = self.store.active_card(selected)
        self.assertEqual(selected_card["prompt"], "first prompt")
        self.assertEqual(selected_card["seed"], 11)
        self.assertEqual(selected_card["requested_duration_seconds"], 5.0)
        self.assertEqual(selected_card["draft_path"], first_take["artifact_path"])
        accepted = self.store.accept()
        master = self.store.absolute_path(self.store.active_card(accepted)["master_path"])
        self.assertEqual(master.read_bytes(), b"first take")

    def test_delete_draft_take_removes_only_unselected_assets(self):
        first_manifest = self._draft(body=b"first take")
        first_card = self.store.active_card(first_manifest)
        first_take = first_card["draft_takes"][0]
        preview = self.store.path / "previews" / first_card["id"] / "first.mp4"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"preview")
        self.store.record_preview(
            card_id=first_card["id"], preview_path=preview,
            preview_sha256=sha256_file(preview),
            source_artifact_sha256=first_card["artifact_sha256"],
        )
        second_manifest = self._draft(action="retry", body=b"second take", seed=22)
        second_card = self.store.active_card(second_manifest)
        with self.assertRaisesRegex(ProjectError, "selected take"):
            self.store.delete_draft_take(
                card_id=second_card["id"], take_id=second_card["selected_draft_take_id"],
                expected_revision=second_manifest["revision"],
            )
        deleted = self.store.delete_draft_take(
            card_id=second_card["id"], take_id=first_take["id"],
            expected_revision=second_manifest["revision"],
        )
        self.assertEqual(len(self.store.active_card(deleted)["draft_takes"]), 1)
        self.assertFalse(self.store.absolute_path(first_take["artifact_path"]).exists())
        self.assertFalse(preview.exists())

    def test_invalidate_accepted_tail_deletes_render_lineage_but_keeps_prompt(self):
        first_manifest = self._draft(body=b"first render", prompt="keep this prompt", seed=31)
        first_card = self.store.active_card(first_manifest)
        preview = self.store.path / "previews" / first_card["id"] / "first.mp4"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"first preview")
        self.store.record_preview(
            card_id=first_card["id"], preview_path=preview,
            preview_sha256=sha256_file(preview),
            source_artifact_sha256=first_card["artifact_sha256"],
        )
        accepted = self.store.accept()
        self.store.unpublish_tail()
        second_draft = self._draft(
            action="retry", body=b"second render", prompt="keep this prompt", seed=32,
        )
        accepted = self.store.accept()
        card = self.store.active_card(accepted)
        artifact_paths = {
            card["master_path"],
            *(take["artifact_path"] for take in card["draft_takes"]),
            *(publication["master_path"] for publication in card["publication_history"]),
        }
        preview_paths = {
            (take.get("preview") or {}).get("asset_path") for take in card["draft_takes"]
        } | {
            (publication.get("preview") or {}).get("asset_path")
            for publication in card["publication_history"]
        }
        existing_paths = {
            path for path in artifact_paths | preview_paths
            if path and self.store.absolute_path(path).is_file()
        }
        self.assertGreaterEqual(len(existing_paths), 3)

        invalidated = self.store.invalidate_render()
        card = self.store.active_card(invalidated)
        self.assertEqual(card["status"], "INVALIDATED")
        self.assertEqual(card["prompt"], "keep this prompt")
        self.assertEqual(card["seed"], 32)
        self.assertEqual(card["artifact_number"], 3)
        self.assertIsNone(card["artifact_sha256"])
        self.assertIsNone(card["recipe"])
        self.assertEqual(card["draft_takes"], [])
        self.assertEqual(card["publication_history"], [])
        self.assertEqual(card["anchors"], [])
        self.assertEqual(card["derivatives"], [])
        self.assertTrue(all(not self.store.absolute_path(path).exists() for path in existing_paths))

        regenerated = self._draft(
            action="generate", body=b"third model render", prompt="keep this prompt", seed=32,
        )
        regenerated_card = self.store.active_card(regenerated)
        self.assertEqual(regenerated_card["status"], "DRAFT")
        self.assertEqual(regenerated_card["attempt"], 1)
        self.assertEqual(regenerated_card["prompt"], "keep this prompt")

    def test_invalidate_rejects_a_non_tail_card_with_dependents(self):
        first_manifest = self._draft(body=b"first")
        accepted = self.store.accept()
        first_id = self.store.active_card(accepted)["id"]
        appended = self.store.append(prompt="second", duration_seconds=5, seed=12)
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["active_card_id"] = first_id
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ProjectError, "timeline tail"):
            self.store.invalidate_render()
        self.assertEqual(len(self.store.load()["cards"]), 2)

    def test_invalidate_restores_quarantined_files_if_manifest_commit_fails(self):
        manifest = self._draft(body=b"must survive")
        card = self.store.active_card(manifest)
        draft = self.store.absolute_path(card["draft_path"])
        with patch.object(self.store, "_commit_unlocked", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.invalidate_render()
        self.assertTrue(draft.is_file())
        restored = self.store.active_card(self.store.load())
        self.assertEqual(restored["status"], "DRAFT")
        self.assertEqual(restored["artifact_sha256"], card["artifact_sha256"])

    def test_invalidate_all_enables_resolution_change_and_sequential_regeneration(self):
        first_draft = self._draft(body=b"card one", prompt="prompt one", seed=21)
        first_accepted = self.store.accept()
        first_id = self.store.active_card(first_accepted)["id"]
        appended = self.store.append(prompt="prompt two", duration_seconds=6, seed=22)
        second_id = self.store.active_card(appended)["id"]
        second_draft = self._draft(body=b"card two", prompt="prompt two", seed=22, duration=6)
        accepted = self.store.accept()
        paths = {
            path
            for card in accepted["cards"]
            for path in (
                card.get("master_path"),
                *(take.get("artifact_path") for take in card.get("draft_takes", [])),
            )
            if path
        }
        with self.assertRaisesRegex(ProjectError, "invalidate all cards first"):
            self.store.update_project_settings(
                expected_revision=accepted["revision"], generation_mode="ref2va",
                width=1024, height=1024,
            )

        invalidated = self.store.invalidate_all_renders()
        self.assertEqual(invalidated["active_card_id"], first_id)
        self.assertTrue(all(card["status"] == "INVALIDATED" for card in invalidated["cards"]))
        self.assertEqual([card["prompt"] for card in invalidated["cards"]], ["prompt one", "prompt two"])
        self.assertTrue(all(not self.store.absolute_path(path).exists() for path in paths))
        self.assertEqual(
            [card["artifact_number"] for card in invalidated["cards"]], [3, 4]
        )

        resized = self.store.update_project_settings(
            expected_revision=invalidated["revision"], generation_mode="ref2va",
            width=1024, height=1024,
        )
        self.assertEqual((resized["width"], resized["height"]), (1024, 1024))
        regenerated = self._draft(
            action="generate", body=b"new card one", prompt="prompt one", seed=21,
        )
        accepted_first = self.store.accept()
        advanced, did_advance = self.store.activate_next_invalidated(first_id)
        self.assertTrue(did_advance)
        self.assertEqual(advanced["active_card_id"], second_id)
        active = self.store.active_card(advanced)
        self.assertEqual(active["status"], "INVALIDATED")
        self.assertEqual(active["generation_parent_id"], first_id)
        self.assertEqual(active["continuation_source"]["type"], "accepted_master")

    def test_schema_ten_completed_card_migrates_to_one_selected_take(self):
        manifest = self._draft(body=b"legacy draft")
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 10
        card = document["cards"][0]
        card.pop("draft_takes", None)
        card.pop("selected_draft_take_id", None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")

        migrated = self.store.load()
        migrated_card = self.store.active_card(migrated)
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(len(migrated_card["draft_takes"]), 1)
        self.assertEqual(
            migrated_card["selected_draft_take_id"], migrated_card["draft_takes"][0]["id"]
        )
        self.assertEqual(migrated_card["draft_takes"][0]["artifact_sha256"], card["artifact_sha256"])

    def test_schema_eleven_draft_takes_migrate_unchanged(self):
        manifest = self._draft(body=b"schema eleven draft")
        original_card = self.store.active_card(manifest)
        original_take_id = original_card["selected_draft_take_id"]
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 11
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")

        migrated = self.store.load()
        card = self.store.active_card(migrated)
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(card["selected_draft_take_id"], original_take_id)
        self.assertEqual(len(card["draft_takes"]), 1)

    def test_accept_never_overwrites_existing_master(self):
        manifest = self._draft()
        card = self.store.active_card(manifest)
        master = self.store.path / "clips" / f"card_{card['artifact_number']:04d}.mmh3"
        master.write_bytes(b"pre-existing")
        with self.assertRaises(ProjectError):
            self.store.accept()
        self.assertEqual(master.read_bytes(), b"pre-existing")

    def test_resume_finishes_published_accept_transaction(self):
        manifest = self._draft()
        card = self.store.active_card(manifest)
        source = self.store.absolute_path(card["draft_path"])
        destination_relative = f"clips/card_{card['artifact_number']:04d}.mmh3"
        destination = self.store.absolute_path(destination_relative)
        destination.write_bytes(source.read_bytes())
        transaction = {
            "schema_version": 1,
            "kind": "accept",
            "card_id": card["id"],
            "source": card["draft_path"],
            "destination": destination_relative,
            "sha256": card["artifact_sha256"],
            "created_at": "test",
        }
        journal = self.store.path / "transactions" / f"accept_{card['id']}.json"
        journal.write_text(json.dumps(transaction), encoding="utf-8")

        recovered = ProjectStore(self.root, "film_01").load()
        recovered_card = self.store.active_card(recovered)
        self.assertEqual(recovered_card["status"], "ACCEPTED")
        self.assertEqual(recovered_card["master_path"], destination_relative)
        self.assertFalse(journal.exists())

    def test_interrupted_generation_is_reconciled_on_restart(self):
        pending, _ = self.store.begin_generation(
            action="generate",
            prompt="opening",
            duration_seconds=5,
            seed=11,
            recipe={},
            fingerprint=generation_fingerprint({}),
        )
        self.assertIsNotNone(pending["pending_operation"])
        with patch("longcaster.project._RUNTIME_ID", "restarted-runtime"):
            restarted = ProjectStore(self.root, "film_01").load()
        self.assertIsNone(restarted["pending_operation"])
        self.assertEqual(restarted["last_operation"]["status"], "interrupted")

    def test_same_runtime_does_not_cancel_active_generation(self):
        pending, _ = self.store.begin_generation(
            action="generate", prompt="opening", duration_seconds=5, seed=11,
            recipe={}, fingerprint=generation_fingerprint({}),
        )
        observed = ProjectStore(self.root, "film_01").load()
        self.assertEqual(observed["pending_operation"]["id"], pending["pending_operation"]["id"])

    def test_cancel_pending_unlocks_empty_card_and_rejects_late_result(self):
        pending, card = self.store.begin_generation(
            action="generate", prompt="opening", duration_seconds=5, seed=11,
            recipe={}, fingerprint=generation_fingerprint({}),
        )
        operation_id = pending["pending_operation"]["id"]
        cancelled, changed = self.store.cancel_pending()
        self.assertTrue(changed)
        self.assertIsNone(cancelled["pending_operation"])
        self.assertEqual(cancelled["last_operation"]["status"], "cancelled")
        self.assertEqual(self.store.active_card(cancelled)["status"], "EMPTY")

        destination = self.store.draft_destination(card)
        destination.write_bytes(b"late result")
        with self.assertRaisesRegex(ProjectError, "stale or no longer pending"):
            self.store.finish_generation(
                operation_id=operation_id,
                draft_path=destination,
                artifact_sha256=sha256_file(destination),
                context_frame_count=0,
                generated_frame_count=124,
                actual_new_frame_count=124,
                actual_duration_seconds=124 / 24,
            )

    def test_cancel_retry_preserves_previous_draft(self):
        first = self._draft()
        original = self.store.active_card(first)
        self.store.begin_generation(
            action="retry", prompt="changed", duration_seconds=6, seed=99,
            recipe={}, fingerprint=generation_fingerprint({"attempt": 2}),
        )
        cancelled, changed = self.store.cancel_pending()
        card = self.store.active_card(cancelled)
        self.assertTrue(changed)
        self.assertEqual(card["status"], "DRAFT")
        self.assertEqual(card["draft_path"], original["draft_path"])
        self.assertEqual(card["artifact_sha256"], original["artifact_sha256"])

        unchanged, changed = self.store.cancel_pending()
        self.assertFalse(changed)
        self.assertIsNone(unchanged["pending_operation"])

    def test_path_traversal_and_invalid_names_are_rejected(self):
        with self.assertRaises(ProjectError):
            ProjectStore(self.root, "../escape")
        with self.assertRaises(ProjectError):
            self.store.absolute_path("../escape.mmh3")

    def test_manifest_is_valid_json_after_each_commit(self):
        self._draft()
        with self.store.manifest_path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
        self.assertEqual(document["project_name"], "film_01")
        self.assertFalse(list(self.store.path.glob(".project.json.*.tmp")))

    def test_missing_and_corrupt_artifacts_are_reported(self):
        manifest = self._draft()
        card = self.store.active_card(manifest)
        draft = self.store.absolute_path(card["draft_path"])
        draft.write_bytes(b"corrupt")
        errors = self.store.validate_artifacts(manifest)
        self.assertEqual(len(errors), 1)
        self.assertIn("hash mismatch", errors[0])
        draft.unlink()
        errors = self.store.validate_artifacts(manifest)
        self.assertEqual(len(errors), 1)
        self.assertIn("missing", errors[0])

    def test_five_accepted_cards_restart_and_sixth_draft(self):
        for number in range(1, 6):
            manifest = self._draft(body=f"card-{number}".encode())
            manifest = self.store.accept()
            if number < 5:
                manifest = self.store.append(prompt="", duration_seconds=5, seed=number + 11)
        masters = sorted((self.store.path / "clips").glob("card_*.mmh3"))
        self.assertEqual([item.name for item in masters], [f"card_{n:04d}.mmh3" for n in range(1, 6)])

        restarted = ProjectStore(self.root, "film_01")
        manifest = restarted.load()
        self.assertEqual(restarted.active_card(manifest)["status"], "ACCEPTED")
        restarted.append(prompt="", duration_seconds=5, seed=17)
        self.store = restarted
        sixth = self._draft(body=b"card-6")
        self.assertEqual(self.store.active_card(sixth)["artifact_number"], 6)
        self.assertEqual(self.store.active_card(sixth)["status"], "DRAFT")

    def test_schema_five_flat_prompt_migrates_without_rewriting_it(self):
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 5
        original = document["cards"][0]["prompt"]
        for field in (
            "timeline_predecessor_id", "prompt_sections", "prompt_format", "assembled_prompt",
            "prompt_hash", "continuation_strategy", "reference_set", "accepted_publication_id",
        ):
            document["cards"][0].pop(field, None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        migrated = self.store.load()
        card = self.store.active_card(migrated)
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(card["prompt_format"], "legacy_flat")
        self.assertEqual(card["prompt"], original)
        self.assertEqual(card["assembled_prompt"], original)
        self.assertIsNone(card["timeline_predecessor_id"])

    def test_schema_six_structured_xml_assembly_migrates_to_plain_labels(self):
        store = ProjectStore(self.root, "schema_six_prompt")
        manifest = store.create(
            prompt="", duration_seconds=5, seed=1, width=544, height=960,
            generation_mode="ref2va",
        )
        document = json.loads(store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 6
        card = document["cards"][0]
        card["prompt_sections"]["summary"]["text"] = "A player enters the court."
        xml = "\n\n".join(
            f"<{name}>\n{card['prompt_sections'][name]['text']}\n</{name}>"
            for name in card["prompt_sections"]
        )
        card["prompt"] = card["assembled_prompt"] = xml
        card["prompt_hash"] = hash_prompt(xml)
        store.manifest_path.write_text(json.dumps(document), encoding="utf-8")

        migrated = store.load()
        card = store.active_card(migrated)
        self.assertEqual(migrated["schema_version"], 13)
        self.assertIn("summary:\nA player enters the court.", card["assembled_prompt"])
        self.assertNotIn("<summary>", card["assembled_prompt"])
        self.assertEqual(card["prompt"], card["assembled_prompt"])

    def test_new_project_with_empty_prompt_starts_structured(self):
        store = ProjectStore(self.root, "empty_prompt_project")
        manifest = store.create(
            prompt="", duration_seconds=5, seed=1, width=1344, height=768,
            generation_mode="ref2va",
        )
        card = store.active_card(manifest)
        self.assertEqual(card["prompt_format"], "structured_v1")
        self.assertEqual(card["prompt"], assemble_prompt(empty_prompt_sections()))

    def test_project_mode_changes_only_before_first_render(self):
        manifest = self.store.update_project_settings(
            expected_revision=self.manifest["revision"], generation_mode="t2va",
        )
        self.assertEqual(manifest["generation_mode"], "t2va")
        self._draft()
        manifest = self.store.load()
        with self.assertRaisesRegex(ProjectError, "mode is locked"):
            self.store.update_project_settings(
                expected_revision=manifest["revision"], generation_mode="ref2va",
            )

    def test_project_lora_words_persist_and_dirty_an_existing_draft(self):
        manifest = self.store.update_project_settings(
            expected_revision=self.manifest["revision"],
            generation_mode="ref2va",
            lora_activation_words="  ohwxPerson, filmStyle  ",
        )
        self.assertEqual(manifest["lora_activation_words"], "ohwxPerson, filmStyle")
        manifest = self._draft()
        self.assertFalse(self.store.active_card(manifest)["draft_inputs_dirty"])
        manifest = self.store.update_project_settings(
            expected_revision=manifest["revision"],
            generation_mode="ref2va",
            lora_activation_words="newTrigger",
        )
        self.assertTrue(self.store.active_card(manifest)["draft_inputs_dirty"])
        with self.assertRaisesRegex(ProjectError, "2000"):
            self.store.update_project_settings(
                expected_revision=manifest["revision"],
                generation_mode="ref2va",
                lora_activation_words="x" * 2001,
            )

    def test_duplicate_project_rebinds_artifacts_and_preserves_source(self):
        source_manifest = self._draft(body=b"source archive")
        source_card = self.store.active_card(source_manifest)
        source_path = self.store.absolute_path(source_card["draft_path"])
        source_hash = source_card["artifact_sha256"]

        def fake_archive_cloner(source, destination, project_name, _source_hash):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes() + b"|" + project_name.encode("utf-8"))
            return sha256_file(destination)

        duplicate = duplicate_project(
            self.root,
            "film_01",
            "film_01_copy",
            archive_cloner=fake_archive_cloner,
        )
        duplicate_store = ProjectStore(self.root, "film_01_copy")
        duplicate_card = duplicate_store.active_card(duplicate)

        self.assertEqual(duplicate["project_name"], "film_01_copy")
        self.assertEqual(duplicate["revision"], 1)
        self.assertEqual(duplicate["last_operation"]["source_project"], "film_01")
        self.assertEqual(duplicate_card["id"], source_card["id"])
        self.assertNotEqual(duplicate_card["artifact_sha256"], source_hash)
        self.assertEqual(duplicate_store.validate_artifacts(duplicate), [])
        self.assertEqual(source_path.read_bytes(), b"source archive")
        self.assertEqual(self.store.load()["project_name"], "film_01")

        with self.assertRaisesRegex(ProjectError, "already exists"):
            duplicate_project(self.root, "film_01", "film_01_copy")

    def test_duplicate_and_invalidate_copies_definitions_without_render_assets(self):
        source_manifest = self._draft(body=b"large source render", prompt="preserved prompt")
        source_card = self.store.active_card(source_manifest)
        source_path = self.store.absolute_path(source_card["draft_path"])

        def forbidden_cloner(*_args):
            raise AssertionError("render archives must not be cloned")

        duplicate = duplicate_project(
            self.root,
            "film_01",
            "film_01_rerender",
            archive_cloner=forbidden_cloner,
            invalidate_renders=True,
        )
        duplicate_store = ProjectStore(self.root, "film_01_rerender")
        duplicate_card = duplicate_store.active_card(duplicate)
        self.assertEqual(duplicate["last_operation"]["kind"], "duplicate_and_invalidate_project")
        self.assertEqual(duplicate_card["status"], "INVALIDATED")
        self.assertEqual(duplicate_card["prompt"], "preserved prompt")
        self.assertEqual(duplicate_card["artifact_number"], 1)
        self.assertEqual(duplicate_card["draft_takes"], [])
        self.assertIsNone(duplicate_card["artifact_sha256"])
        self.assertEqual(duplicate_store.validate_artifacts(duplicate), [])
        self.assertEqual(list((duplicate_store.path / "drafts").iterdir()), [])
        self.assertTrue(source_path.is_file())
        self.assertEqual(self.store.active_card(self.store.load())["status"], "DRAFT")

    def test_clone_card_archive_rewrites_embedded_project_and_source_hash(self):
        class FakePacket:
            def __init__(self):
                self.manifest = {"extensions": {"longcaster": {
                    "card": {"project_name": "film_01", "card_id": "card-id"},
                    "derivative": {"source_artifact_sha256": "0" * 64},
                }}}

            def set_extension_value(self, namespace, key, value):
                self.manifest["extensions"].setdefault(namespace, {})[key] = value
                return self

        class FakeApi:
            def __init__(self, packet):
                self.packet = packet

            def save_archive(self, packet, path):
                Path(path).write_bytes(b"rewritten archive")
                return packet, path

        packet = FakePacket()
        destination = self.root / "clone.mmh3"
        with (
            patch("longcaster.mmh3_adapter.load_packet", return_value=packet),
            patch("longcaster.mmh3_adapter.mmh3_api", return_value=FakeApi(packet)),
            patch("longcaster.mmh3_adapter.primary_latent"),
        ):
            digest = clone_card_archive(
                self.root / "source.mmh3",
                destination,
                "film_01_copy",
                "a" * 64,
            )

        metadata = packet.manifest["extensions"]["longcaster"]
        self.assertEqual(metadata["card"]["project_name"], "film_01_copy")
        self.assertEqual(metadata["derivative"]["source_artifact_sha256"], "a" * 64)
        self.assertEqual(digest, sha256_file(destination))

    def test_continuation_strategy_updates_generation_parent(self):
        self._draft()
        manifest = self.store.accept()
        accepted = self.store.active_card(manifest)
        manifest = self.store.append(prompt="", duration_seconds=5, seed=12)
        card = self.store.active_card(manifest)
        manifest = self.store.update_card_editor(
            card_id=card["id"], expected_revision=manifest["revision"],
            continuation_strategy="independent",
        )
        independent = self.store.active_card(manifest)
        self.assertEqual(independent["continuation_strategy"], "independent")
        self.assertIsNone(independent["generation_parent_id"])
        manifest = self.store.update_card_editor(
            card_id=card["id"], expected_revision=manifest["revision"],
            continuation_strategy="direct_mmh3",
        )
        continued = self.store.active_card(manifest)
        self.assertEqual(continued["generation_parent_id"], accepted["id"])

    def test_labeled_flat_prompt_conversion_assigns_all_sections(self):
        prompt = "\n\n".join(
            f"{name}:\n{name} from swim04" for name in (
                "subject_definitions", "summary", "retention_analysis",
                "detailed_description", "overall_soundscape", "non_diegetic_music",
            )
        )
        store = ProjectStore(self.root, "swim04_conversion")
        manifest = store.create(
            prompt=prompt, duration_seconds=10, seed=7, width=544, height=960,
            generation_mode="ref2va",
        )
        card = store.active_card(manifest)
        self.assertEqual(card["prompt_format"], "legacy_flat")
        manifest = store.update_card_editor(
            card_id=card["id"], expected_revision=manifest["revision"],
            convert_legacy=True,
        )
        converted = store.active_card(manifest)
        self.assertEqual(converted["prompt_format"], "structured_v1")
        self.assertEqual(converted["prompt_sections"]["summary"]["text"], "summary from swim04")
        self.assertEqual(converted["assembled_prompt"], prompt)

    def test_full_prompt_import_replaces_structured_sections(self):
        store = ProjectStore(self.root, "prompt_import")
        manifest = store.create(
            prompt="", duration_seconds=5, seed=7, width=544, height=960,
            generation_mode="ref2va",
        )
        card = store.active_card(manifest)
        manifest = store.update_card_editor(
            card_id=card["id"], expected_revision=manifest["revision"],
            import_prompt="Summary:\nImported summary\n\nOverall Soundscape:\nPool ambience",
        )
        imported = store.active_card(manifest)
        self.assertEqual(imported["prompt_format"], "structured_v1")
        self.assertEqual(imported["prompt_sections"]["summary"]["text"], "Imported summary")
        self.assertEqual(
            imported["prompt_sections"]["overall_soundscape"]["text"], "Pool ambience"
        )

    def test_structured_card_autosave_tracks_revision_and_copy_provenance(self):
        sections = edit_sections(empty_prompt_sections(), {
            "subject_definitions": "Subject A",
            "summary": "Opening summary",
            "overall_soundscape": "Room tone",
        })
        assembled = assemble_prompt(sections)
        self.store.update_card_editor(
            card_id=self.manifest["active_card_id"],
            expected_revision=self.manifest["revision"],
            section_changes={name: record["text"] for name, record in sections.items()},
            convert_legacy=True,
        )
        manifest = self.store.load()
        first = self.store.active_card(manifest)
        self.assertEqual(first["prompt_format"], "structured_v1")
        self.assertEqual(first["prompt"], assembled)
        self._draft()
        manifest = self.store.accept()
        accepted = self.store.active_card(manifest)
        self.assertIsNotNone(accepted["accepted_publication_id"])
        manifest = self.store.append(prompt="", duration_seconds=6, seed=22)
        second = self.store.active_card(manifest)
        self.assertEqual(second["prompt_sections"]["subject_definitions"]["text"], "Subject A")
        self.assertEqual(second["prompt_sections"]["summary"]["text"], "")
        self.assertEqual(
            second["prompt_sections"]["overall_soundscape"]["provenance"]["source_type"],
            "copied_previous",
        )

        with self.assertRaisesRegex(ProjectError, "stale project revision"):
            self.store.update_card_editor(
                card_id=second["id"], expected_revision=manifest["revision"] - 1,
                section_changes={"summary": "stale"},
            )
        manifest = self.store.update_card_editor(
            card_id=second["id"], expected_revision=manifest["revision"],
            section_changes={"summary": "Card two"}, duration_seconds=7, seed=23,
        )
        updated = self.store.active_card(manifest)
        self.assertEqual(updated["requested_duration_seconds"], 7)
        self.assertEqual(updated["seed"], 23)

        manifest = self.store.copy_card_sections(
            target_card_id=second["id"], source_card_id=accepted["id"],
            names=["summary"], expected_revision=manifest["revision"],
        )
        copied = self.store.active_card(manifest)["prompt_sections"]["summary"]
        self.assertEqual(copied["text"], "Opening summary")
        self.assertEqual(copied["provenance"]["source_type"], "copied_previous")
        self.assertEqual(copied["provenance"]["source_card_id"], accepted["id"])
        manifest = self.store.update_card_editor(
            card_id=second["id"], expected_revision=manifest["revision"],
            clear_sections=["summary"],
        )
        cleared = self.store.active_card(manifest)["prompt_sections"]["summary"]
        self.assertEqual(cleared["text"], "")
        self.assertEqual(cleared["provenance"]["source_type"], "manual")
        self.assertIsNone(cleared["provenance"]["source_card_id"])

    def test_accepted_card_editor_is_read_only(self):
        self._draft()
        manifest = self.store.accept()
        card = self.store.active_card(manifest)
        with self.assertRaisesRegex(ProjectError, "read-only"):
            self.store.update_card_editor(
                card_id=card["id"], expected_revision=manifest["revision"],
                section_changes={"summary": "no"},
            )

    def test_editing_generated_draft_requires_retry_before_accept(self):
        manifest = self.store.update_card_editor(
            card_id=self.manifest["active_card_id"], expected_revision=self.manifest["revision"],
            section_changes={"summary": "First version"}, convert_legacy=True,
        )
        manifest = self._draft()
        card = self.store.active_card(manifest)
        self.assertFalse(card["draft_inputs_dirty"])
        manifest = self.store.update_card_editor(
            card_id=card["id"], expected_revision=manifest["revision"],
            section_changes={"summary": "Changed after render"},
        )
        self.assertTrue(self.store.active_card(manifest)["draft_inputs_dirty"])
        with self.assertRaisesRegex(ProjectError, "Retry Draft"):
            self.store.accept()
        manifest = self._draft(action="retry", body=b"matching replacement")
        self.assertFalse(self.store.active_card(manifest)["draft_inputs_dirty"])
        self.assertEqual(self.store.active_card(self.store.accept())["status"], "ACCEPTED")

    def test_failed_initial_generation_has_failed_status_but_failed_retry_keeps_draft(self):
        pending, _ = self.store.begin_generation(
            action="generate", prompt="opening", duration_seconds=5, seed=11,
            recipe={}, fingerprint=generation_fingerprint({}),
        )
        self.store.fail_generation(pending["pending_operation"]["id"], "GPU failed")
        failed = self.store.load()
        self.assertEqual(self.store.active_card(failed)["status"], "FAILED")
        pending, _ = self.store.begin_generation(
            action="generate", prompt="opening", duration_seconds=5, seed=11,
            recipe={}, fingerprint=generation_fingerprint({"retry": 1}),
        )
        card = self.store.active_card(pending)
        destination = self.store.draft_destination(card)
        destination.write_bytes(b"working draft")
        manifest = self.store.finish_generation(
            operation_id=pending["pending_operation"]["id"], draft_path=destination,
            artifact_sha256=sha256_file(destination), context_frame_count=0,
            generated_frame_count=124, actual_new_frame_count=124,
            actual_duration_seconds=124 / 24,
        )
        pending, _ = self.store.begin_generation(
            action="retry", prompt="opening", duration_seconds=5, seed=11,
            recipe={}, fingerprint=generation_fingerprint({"retry": 2}),
        )
        self.store.fail_generation(pending["pending_operation"]["id"], "retry failed")
        self.assertEqual(self.store.active_card(self.store.load())["status"], "DRAFT")


if __name__ == "__main__":
    unittest.main()
