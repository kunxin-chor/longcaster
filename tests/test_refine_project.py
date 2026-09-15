from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from longcaster.fingerprint import generation_fingerprint
from longcaster.project import ProjectError, ProjectStore, sha256_file


class RefineProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root, "refine_test")
        self.store.create(
            prompt="opening",
            duration_seconds=5,
            seed=7,
            width=1344,
            height=768,
            generation_mode="ref2va",
            refine_cadence="every_accepted_card",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _accepted(self):
        manifest, card = self.store.begin_generation(
            action="generate",
            prompt="opening",
            duration_seconds=5,
            seed=7,
            recipe={},
            fingerprint=generation_fingerprint({"card": 1}),
        )
        destination = self.store.draft_destination(card)
        destination.write_bytes(b"accepted master stand-in")
        manifest = self.store.finish_generation(
            operation_id=manifest["pending_operation"]["id"],
            draft_path=destination,
            artifact_sha256=sha256_file(destination),
            context_frame_count=0,
            generated_frame_count=121,
            actual_new_frame_count=121,
            actual_duration_seconds=5,
        )
        return self.store.accept()

    def _ready_refine(self, *, use_as_default):
        manifest = self.store.load()
        card = self.store.active_card(manifest)
        pending, derivative = self.store.begin_refine(
            card_id=card["id"],
            cadence=manifest["refine_cadence"],
            recipe={"sigmas": [0.85, 0.42, 0]},
            use_as_default=use_as_default,
        )
        destination = self.store.derivative_destination(card, derivative["id"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"refined derivative stand-in")
        return self.store.finish_refine(
            operation_id=pending["pending_operation"]["id"],
            derivative_path=destination,
            artifact_sha256=sha256_file(destination),
            execution_seconds=1.25,
            prefix_protection={"method": "none_first_card"},
            audio_handling="joint_av_second_sample",
        )

    def test_ready_refine_becomes_frozen_child_source_without_mutating_master(self):
        manifest = self._accepted()
        accepted = self.store.active_card(manifest)
        master = self.store.absolute_path(accepted["master_path"])
        master_hash = sha256_file(master)
        manifest = self._ready_refine(use_as_default=True)
        accepted = self.store.active_card(manifest)
        derivative = accepted["derivatives"][-1]
        self.assertEqual(derivative["status"], "READY")
        self.assertEqual(accepted["continuation_source_preference"], "derivative")
        self.assertEqual(sha256_file(master), master_hash)

        manifest = self.store.append(prompt="", duration_seconds=5, seed=8)
        child = self.store.active_card(manifest)
        self.assertEqual(child["continuation_source"]["type"], "derivative")
        self.assertEqual(child["continuation_source"]["derivative_id"], derivative["id"])
        resolved = self.store.continuation_artifact(manifest, child)
        self.assertEqual(resolved["artifact_sha256"], derivative["artifact_sha256"])
        self.assertFalse(resolved["fallback"])
        self.assertEqual(self.store.validate_artifacts(manifest), [])

    def test_failed_refine_falls_back_to_accepted_master(self):
        manifest = self._accepted()
        card = self.store.active_card(manifest)
        pending, derivative = self.store.begin_refine(
            card_id=card["id"], cadence="every_accepted_card", recipe={}, use_as_default=True
        )
        manifest = self.store.fail_refine(pending["pending_operation"]["id"], "sampler failed")
        card = self.store.active_card(manifest)
        self.assertEqual(card["derivatives"][-1]["status"], "FAILED")
        self.assertEqual(card["continuation_source_preference"], "accepted_master")
        manifest = self.store.append(prompt="next", duration_seconds=5, seed=8)
        child = self.store.active_card(manifest)
        self.assertEqual(child["continuation_source"]["type"], "accepted_master")

    def test_missing_selected_derivative_resolves_to_explicit_master_fallback(self):
        self._accepted()
        manifest = self._ready_refine(use_as_default=True)
        accepted = self.store.active_card(manifest)
        derivative = accepted["derivatives"][-1]
        manifest = self.store.append(prompt="next", duration_seconds=5, seed=8)
        self.store.absolute_path(derivative["artifact_path"]).unlink()
        child = self.store.active_card(manifest)
        resolved = self.store.continuation_artifact(manifest, child)
        self.assertEqual(resolved["type"], "accepted_master")
        self.assertTrue(resolved["fallback"])
        self.assertIn("unavailable", resolved["fallback_reason"])

    def test_manual_refine_requires_explicit_selection_and_updates_empty_child(self):
        manifest = self._accepted()
        accepted = self.store.active_card(manifest)
        manifest = self._ready_refine(use_as_default=False)
        accepted = self.store.active_card(manifest)
        self.assertEqual(accepted["continuation_source_preference"], "accepted_master")
        manifest = self.store.append(prompt="next", duration_seconds=5, seed=8)
        child = self.store.active_card(manifest)
        self.assertEqual(child["continuation_source"]["type"], "accepted_master")
        manifest = self.store.set_continuation_source_preference(
            card_id=accepted["id"],
            source_type="derivative",
            expected_revision=manifest["revision"],
        )
        child = self.store.active_card(manifest)
        self.assertEqual(child["continuation_source"]["type"], "derivative")

    def test_cancelled_refine_is_failed_without_changing_accepted_card(self):
        manifest = self._accepted()
        card = self.store.active_card(manifest)
        self.store.begin_refine(
            card_id=card["id"], cadence="manual", recipe={}, use_as_default=False
        )
        manifest, changed = self.store.cancel_pending("cancelled")
        self.assertTrue(changed)
        card = self.store.active_card(manifest)
        self.assertEqual(card["status"], "ACCEPTED")
        self.assertEqual(card["derivatives"][-1]["status"], "FAILED")

    def test_manual_checkbox_is_saved_on_editable_card_and_locked_after_accept(self):
        manifest = self.store.load()
        card = self.store.active_card(manifest)
        manifest = self.store.update_card_editor(
            card_id=card["id"],
            expected_revision=manifest["revision"],
            refine_enabled=True,
        )
        card = self.store.active_card(manifest)
        self.assertTrue(card["refine_enabled"])
        self.assertFalse(card["draft_inputs_dirty"])
        manifest = self._accepted()
        card = self.store.active_card(manifest)
        with self.assertRaises(ProjectError):
            self.store.update_card_editor(
                card_id=card["id"],
                expected_revision=manifest["revision"],
                refine_enabled=False,
            )

    def test_failed_regeneration_preserves_previous_ready_derivative(self):
        self._accepted()
        manifest = self._ready_refine(use_as_default=True)
        card = self.store.active_card(manifest)
        ready_id = card["derivatives"][-1]["id"]
        pending, _ = self.store.begin_refine(
            card_id=card["id"], cadence="every_accepted_card", recipe={}, use_as_default=True
        )
        manifest = self.store.fail_refine(pending["pending_operation"]["id"], "retry failed")
        card = self.store.active_card(manifest)
        self.assertEqual(card["continuation_source_preference"], "derivative")
        self.assertEqual(self.store._latest_ready_refine(card)["id"], ready_id)

    def test_unpublish_removes_all_refine_files_and_metadata(self):
        self._accepted()
        manifest = self._ready_refine(use_as_default=True)
        card = self.store.active_card(manifest)
        derivative_path = self.store.absolute_path(card["derivatives"][-1]["artifact_path"])
        self.assertTrue(derivative_path.is_file())

        manifest = self.store.unpublish_tail()
        card = self.store.active_card(manifest)
        self.assertEqual(card["status"], "DRAFT")
        self.assertEqual(card["derivatives"], [])
        self.assertEqual(card["continuation_source_preference"], "accepted_master")
        self.assertEqual(card["publication_history"][-1]["derivatives"], [])
        self.assertFalse(derivative_path.exists())

    def test_switching_to_auto_does_not_retroactively_create_a_derivative(self):
        manifest = self._accepted()
        card = self.store.active_card(manifest)
        self.assertEqual(card["derivatives"], [])
        manifest = self.store.set_refine_cadence(
            "every_accepted_card", expected_revision=manifest["revision"]
        )
        self.assertEqual(self.store.active_card(manifest)["derivatives"], [])

    def test_schema_nine_migrates_with_empty_project_lora_words(self):
        manifest = self.store.load()
        manifest["schema_version"] = 9
        manifest.pop("lora_activation_words")
        self.store.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        migrated = self.store.load()
        self.assertEqual(migrated["schema_version"], 13)
        self.assertEqual(migrated["lora_activation_words"], "")


if __name__ == "__main__":
    unittest.main()
