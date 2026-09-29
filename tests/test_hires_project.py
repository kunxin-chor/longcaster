from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from longcaster.duration import resolve_duration
from longcaster.project import ProjectStore, ProjectError, sha256_file


class HiresProjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProjectStore(Path(self.temp.name), "hires")
        self.store.create(prompt="opening", duration_seconds=5, seed=7,
                          width=544, height=960, generation_mode="t2va")

    def accept(self, *, append=False, strategy="direct_mmh3"):
        if append:
            m = self.store.append(prompt="next", duration_seconds=5, seed=8)
            if strategy != "direct_mmh3":
                self.store.update_card_editor(card_id=m["active_card_id"], expected_revision=m["revision"],
                                               continuation_strategy=strategy)
        m, card = self.store.begin_generation(action="generate", prompt="frozen", duration_seconds=5,
                                               seed=7, recipe={}, fingerprint="test")
        context = 39 if card["continuation_strategy"] == "direct_mmh3" else 0
        plan = resolve_duration(5, context_frames=context)
        path = self.store.draft_destination(card)
        path.write_bytes(card["id"].encode())
        self.store.finish_generation(operation_id=m["pending_operation"]["id"], draft_path=path,
                                     artifact_sha256=sha256_file(path), context_frame_count=context,
                                     generated_frame_count=plan.generated_frames,
                                     actual_new_frame_count=plan.actual_new_frames,
                                     actual_duration_seconds=plan.actual_new_seconds)
        m = self.store.accept()
        return self.store.active_card(m)

    def begin(self, card):
        return self.store.begin_hires_refine(card_id=card["id"], recipe={"sigmas": [0.3, 0]},
                                             upscaler_model="h3_3d.safetensors")

    def ready(self, card):
        m, d = self.begin(card)
        path = self.store.hires_destination(card, d["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(d["id"].encode())
        m = self.store.finish_hires_refine(operation_id=m["pending_operation"]["id"],
                                          derivative_path=path, artifact_sha256=sha256_file(path),
                                          execution_seconds=1.25, prefix_protection={"strict": True})
        return self.store.latest_ready_hires(m, card)

    def test_schema_21_migrates_to_manual_without_touching_cards(self):
        m = self.store.load()
        m["schema_version"] = 21
        m.pop("hires_cadence")
        original = deepcopy(m["cards"])
        self.store.manifest_path.write_text(json.dumps(m), encoding="utf-8")
        m = self.store.load()
        self.assertEqual(m["schema_version"], 24)
        self.assertEqual(m["hires_cadence"], "manual")
        self.assertEqual(m["cards"], original)

    def test_create_finish_never_changes_native_master_or_canvas(self):
        card = self.accept()
        original = deepcopy(card)
        d = self.ready(card)
        m = self.store.load()
        current = self.store.card(m, card["id"])
        self.assertEqual((d["target_width"], d["target_height"]), (1088, 1920))
        self.assertEqual(d["audio_handling"], "locked_native_audio")
        self.assertEqual(d["status"], "READY")
        self.assertEqual((m["width"], m["height"]), (544, 960))
        for key in ("master_path", "artifact_sha256", "recipe", "continuation_source_preference"):
            self.assertEqual(current[key], original[key])
        self.assertEqual(sha256_file(self.store.absolute_path(card["master_path"])), card["artifact_sha256"])

    def test_hires_cannot_be_native_continuation_source(self):
        card = self.accept()
        d = self.ready(card)
        m = self.store.append(prompt="child", duration_seconds=5, seed=1)
        child = self.store.active_card(m)
        source = self.store.continuation_artifact(m, child)
        self.assertEqual(source["type"], "accepted_master")
        with self.assertRaisesRegex(ProjectError, "no ready refine"):
            self.store.set_continuation_source_preference(card_id=card["id"], source_type="derivative", expected_revision=m["revision"])
        child = m["cards"][-1]
        child["continuation_source"] = {"source_card_id": card["id"], "type": "derivative", "derivative_id": d["id"]}
        with self.assertRaisesRegex(ProjectError, "missing or not ready"):
            self.store._validate(m)
        self.assertEqual(self.store.continuation_artifact(m, child)["type"], "accepted_master")

    def test_direct_requires_parent_hires(self):
        self.accept()
        card = self.accept(append=True)
        with self.assertRaisesRegex(ProjectError, "Card 2.*requires a ready Card 1"):
            self.begin(card)

    def test_organization_keeps_cross_group_hires_chain_ready(self):
        parent = self.accept()
        self.ready(parent)
        child = self.accept(append=True)
        self.ready(child)
        before = self.store.load()
        grouped = self.store.organize_timeline(
            expected_revision=before["revision"], action="create", name="Another scene",
        )
        grouped = self.store.organize_timeline(
            expected_revision=grouped["revision"], action="assign", card_id=child["id"],
            timeline_id=grouped["organization_timelines"][0]["id"],
        )
        for old, current in zip(before["cards"], grouped["cards"]):
            self.assertEqual(current["derivatives"], old["derivatives"])
            self.assertEqual(current["recipe"], old["recipe"])
            self.assertIsNotNone(self.store.latest_ready_hires(grouped, current))
        self.assertEqual(self.store.hires_parent(grouped, grouped["cards"][-1])["id"], before["cards"][0]["derivatives"][-1]["id"])

    def test_independent_and_restart_start_new_chains(self):
        self.accept()
        for strategy in ("independent", "image_restart"):
            card = self.accept(append=True, strategy=strategy)
            d = self.ready(card)
            self.assertIsNone(d["hires_parent_derivative_id"])
            self.assertIsNone(d["hires_parent_sha256"])

    def test_regeneration_invalidates_only_dependent_hires(self):
        first = self.accept(); self.ready(first)
        second = self.accept(append=True); self.ready(second)
        third = self.accept(append=True); self.ready(third)
        independent = self.accept(append=True, strategy="independent"); self.ready(independent)
        before = self.store.load()
        new = self.ready(first)
        after = self.store.load()
        self.assertEqual(self.store.latest_ready_hires(after, first)["id"], new["id"])
        self.assertIsNone(self.store.latest_ready_hires(after, second))
        self.assertIsNone(self.store.latest_ready_hires(after, third))
        self.assertIsNotNone(self.store.latest_ready_hires(after, independent))
        for old, card in zip(before["cards"], after["cards"]):
            for key in ("master_path", "artifact_sha256", "status", "continuation_source", "recipe"):
                self.assertEqual(card[key], old[key])
            self.assertEqual(sha256_file(self.store.absolute_path(card["master_path"])), card["artifact_sha256"])

    def test_failed_retry_keeps_previous_chain(self):
        first = self.accept(); old = self.ready(first)
        second = self.accept(append=True); child = self.ready(second)
        m, _ = self.begin(first)
        m = self.store.fail_hires_refine(m["pending_operation"]["id"], "sampler failed")
        self.assertEqual(self.store.latest_ready_hires(m, first)["id"], old["id"])
        self.assertEqual(self.store.latest_ready_hires(m, second)["id"], child["id"])
        self.assertIsNone(m["pending_operation"])

    def test_cancel_and_restart_recovery_keep_native_accepted(self):
        card = self.accept()
        self.begin(card)
        m, changed = self.store.cancel_pending()
        self.assertTrue(changed)
        self.assertEqual(m["cards"][0]["status"], "ACCEPTED")
        self.assertEqual(m["cards"][0]["derivatives"][-1]["status"], "FAILED")
        m, _ = self.begin(card)
        m["pending_operation"]["owner_runtime_id"] = "previous-runtime"
        self.store.manifest_path.write_text(json.dumps(m), encoding="utf-8")
        m = self.store.load()
        self.assertEqual(m["cards"][0]["derivatives"][-1]["status"], "FAILED")
        self.assertEqual(m["cards"][0]["status"], "ACCEPTED")

    def test_artifact_corruption_is_rejected(self):
        card = self.accept(); d = self.ready(card)
        self.store.absolute_path(d["artifact_path"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ProjectError, "hash mismatch"):
            self.store.latest_ready_hires(self.store.load(), card)

    def test_parent_hash_and_source_hash_are_verified(self):
        first = self.accept(); d = self.ready(first)
        second = self.accept(append=True); self.ready(second)
        m = self.store.load()
        m["cards"][1]["derivatives"][-1]["hires_parent_sha256"] = "0" * 64
        with self.assertRaisesRegex(ProjectError, "hires-parent"):
            self.store.latest_ready_hires(m, second)
        self.store.absolute_path(d["artifact_path"]).write_bytes(b"corrupt parent")
        with self.assertRaisesRegex(ProjectError, "hash mismatch"):
            self.store.latest_ready_hires(self.store.load(), second)

    def test_finish_rejects_wrong_hash_and_native_destination(self):
        card = self.accept(); m, d = self.begin(card)
        args = dict(operation_id=m["pending_operation"]["id"], execution_seconds=0, prefix_protection={})
        with self.assertRaisesRegex(ProjectError, "own derivative destination"):
            self.store.finish_hires_refine(**args, derivative_path=card["master_path"], artifact_sha256=card["artifact_sha256"])
        path = self.store.hires_destination(card, d["id"])
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"hires")
        with self.assertRaisesRegex(ProjectError, "hash does not match"):
            self.store.finish_hires_refine(**args, derivative_path=path, artifact_sha256="0" * 64)

    def test_deletion_invalidates_descendants_and_keeps_native_files(self):
        first = self.accept(); d = self.ready(first)
        second = self.accept(append=True); self.ready(second)
        m = self.store.delete_hires(card_id=first["id"])
        self.assertFalse(self.store.absolute_path(d["artifact_path"]).exists())
        self.assertIsNone(self.store.latest_ready_hires(m, second))
        self.assertEqual(m["cards"][1]["status"], "ACCEPTED")
        self.assertTrue(self.store.absolute_path(first["master_path"]).exists())

    def test_unpublish_removes_hires(self):
        card = self.accept(); d = self.ready(card)
        m = self.store.unpublish_tail()
        self.assertEqual(m["cards"][0]["derivatives"], [])
        self.assertFalse(self.store.absolute_path(d["artifact_path"]).exists())

    def test_native_invalidation_marks_hires_chain_stale(self):
        first = self.accept(); self.ready(first)
        second = self.accept(append=True); self.ready(second)
        m = self.store.load()
        # Isolated dependency reconciliation also handles a removed native source.
        m["cards"][0]["artifact_sha256"] = "f" * 64
        self.assertTrue(self.store._invalidate_stale_hires(m))
        self.assertEqual([c["derivatives"][-1]["status"] for c in m["cards"]], ["INVALIDATED", "INVALIDATED"])
        self.assertEqual(m["cards"][1]["status"], "ACCEPTED")

    def test_off_and_auto_are_not_executed(self):
        card = self.accept()
        self.store.set_hires_cadence("off")
        with self.assertRaisesRegex(ProjectError, "Off"):
            self.begin(card)
        with self.assertRaisesRegex(ProjectError, "auto awaits"):
            self.store.set_hires_cadence("auto")

    def test_stale_completion_after_cancel_is_rejected(self):
        card = self.accept(); m, d = self.begin(card)
        self.store.cancel_pending()
        path = self.store.hires_destination(card, d["id"])
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b"late")
        with self.assertRaisesRegex(ProjectError, "stale or no longer pending"):
            self.store.finish_hires_refine(operation_id=m["pending_operation"]["id"],
                                          derivative_path=path, artifact_sha256=sha256_file(path),
                                          execution_seconds=1, prefix_protection={})

    def test_hires_preview_is_removed_with_unpublish(self):
        card = self.accept(); d = self.ready(card)
        preview = self.store.hires_destination(card, d["id"]).with_suffix(".mp4")
        preview.write_bytes(b"preview")
        self.store.record_hires_preview(card_id=card["id"], preview_path=preview,
                                        preview_sha256=sha256_file(preview), source_artifact_sha256=d["artifact_sha256"])
        self.store.unpublish_tail()
        self.assertFalse(preview.exists())

    def test_duplicate_rebinds_hires_parent_hashes(self):
        from longcaster.duplicate import duplicate_project
        first = self.accept(); a = self.ready(first)
        second = self.accept(append=True); b = self.ready(second)
        def clone(source, target, name, source_hash):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes() + name.encode())
            return sha256_file(target)
        m = duplicate_project(self.store.projects_root, "hires", "copy", archive_cloner=clone)
        copied = ProjectStore(self.store.projects_root, "copy")
        da = copied.latest_ready_hires(m, first)
        db = copied.latest_ready_hires(m, second)
        self.assertNotEqual(da["artifact_sha256"], a["artifact_sha256"])
        self.assertNotEqual(db["artifact_sha256"], b["artifact_sha256"])
        self.assertEqual(db["hires_parent_sha256"], da["artifact_sha256"])
        self.assertEqual(copied.validate_artifacts(m), [])

    def test_export_rejects_missing_ancestor_even_outside_selected_range(self):
        import sys
        import types
        from unittest.mock import patch
        with patch.dict(sys.modules, {"folder_paths": types.ModuleType("folder_paths")}):
            from longcaster.timeline_export import export_hires_sources
        first = self.accept(); self.ready(first)
        second = self.accept(append=True); self.ready(second)
        m = self.store.load()
        self.assertEqual(len(export_hires_sources(self.store, m, [m["cards"][1]], source="hires")), 1)
        m["cards"][0]["derivatives"][-1]["status"] = "INVALIDATED"
        with self.assertRaisesRegex(ProjectError, "requires a ready Card 1"):
            export_hires_sources(self.store, m, [m["cards"][1]], source="hires")


if __name__ == "__main__":
    unittest.main()
