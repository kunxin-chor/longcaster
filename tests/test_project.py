import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from longcaster.fingerprint import generation_fingerprint
from longcaster.project import ProjectError, ProjectStore, sha256_file


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

    def test_schema_one_project_migrates_with_empty_anchor_lists(self):
        document = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        document["schema_version"] = 1
        for card in document["cards"]:
            card.pop("anchors", None)
        self.store.manifest_path.write_text(json.dumps(document), encoding="utf-8")
        migrated = self.store.load()
        self.assertEqual(migrated["schema_version"], 2)
        self.assertEqual(migrated["cards"][0]["anchors"], [])

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


if __name__ == "__main__":
    unittest.main()
