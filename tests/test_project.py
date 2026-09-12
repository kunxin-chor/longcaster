import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from longcaster.fingerprint import generation_fingerprint
from longcaster.project import ProjectError, ProjectStore, sha256_file
from longcaster.prompt_sections import assemble_prompt, edit_sections, empty_prompt_sections


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

    def _draft(self, action="generate", body=b"valid mmh3 stand-in"):
        fingerprint = generation_fingerprint({"seed": 11, "sigmas": [12, 0]})
        manifest, card = self.store.begin_generation(
            action=action,
            prompt="opening",
            duration_seconds=5.0,
            seed=11,
            recipe={"sigmas": [12, 0]},
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
        self.assertFalse(copied_draft.exists())
        self.assertEqual(replacement["status"], "DRAFT")
        manifest = self.store.accept()
        replacement = self.store.active_card(manifest)
        self.assertEqual(replacement["master_path"], "clips/card_0002.mmh3")
        self.assertTrue(old_master.is_file())
        self.assertTrue(self.store.absolute_path(replacement["master_path"]).is_file())

    def test_unpublish_rejects_nonaccepted_active_card(self):
        with self.assertRaises(ProjectError):
            self.store.unpublish_tail()

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

    def test_schema_one_project_migrates_with_empty_anchor_lists(self):
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        for card in document["cards"]:
            card.pop("anchors", None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        migrated = self.store.load()
        self.assertEqual(migrated["schema_version"], 6)
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
        self.assertEqual(migrated["schema_version"], 6)
        self.assertEqual(migrated["cards"][0]["anchors"], [existing])
        self.assertEqual(migrated["active_identity_anchors"], {})

    def test_schema_three_project_adds_publication_history(self):
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 3
        for card in document["cards"]:
            card.pop("publication_history", None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        migrated = self.store.load()
        self.assertEqual(migrated["schema_version"], 6)
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
        self.assertEqual(migrated["schema_version"], 6)
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
        self.assertEqual(migrated["schema_version"], 6)
        self.assertEqual(card["prompt_format"], "legacy_flat")
        self.assertEqual(card["prompt"], original)
        self.assertEqual(card["assembled_prompt"], original)
        self.assertIsNone(card["timeline_predecessor_id"])

    def test_new_project_with_empty_prompt_starts_structured(self):
        store = ProjectStore(self.root, "empty_prompt_project")
        manifest = store.create(
            prompt="", duration_seconds=5, seed=1, width=1344, height=768,
            generation_mode="ref2va",
        )
        card = store.active_card(manifest)
        self.assertEqual(card["prompt_format"], "structured_v1")
        self.assertEqual(card["prompt"], assemble_prompt(empty_prompt_sections()))

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
