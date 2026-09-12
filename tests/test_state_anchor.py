import unittest
from unittest.mock import patch

from longcaster.state_anchor import (
    CONTINUITY_INSTRUCTION,
    continuation_anchor_frame,
    current_state_anchor,
    decode_identity_preview,
    identity_instruction,
    identity_source_frame,
    reinforce_prompt,
    validate_identity_scope,
)


class StateAnchorTests(unittest.TestCase):
    def test_identity_preview_excludes_continuation_prefix(self):
        class Frames:
            shape = (45, 8, 8, 3)

            def __getitem__(self, key):
                return key

        card = {
            "status": "ACCEPTED",
            "master_path": "clips/card.mmh3",
            "context_frame_count": 39,
            "actual_new_frame_count": 6,
        }
        with patch("longcaster.state_anchor._decoded_images", return_value=Frames()):
            self.assertEqual(
                decode_identity_preview(card=card, packet=object(), video_vae=object()),
                slice(39, 45, None),
            )

    def test_anchor_aligns_to_end_of_continuation_prefix(self):
        self.assertEqual(continuation_anchor_frame(39), 38)
        with self.assertRaises(ValueError):
            continuation_anchor_frame(0)

    def test_prompt_sections_receive_minimal_instruction(self):
        prompt = "summary:\nWalk into the room.\n\nretention_analysis:\nKeep identity."
        updated, injected, sections = reinforce_prompt(prompt)
        self.assertTrue(injected)
        self.assertEqual(sections, ["summary", "retention_analysis"])
        self.assertEqual(updated.count(CONTINUITY_INSTRUCTION), 2)
        self.assertIn("Walk into the room.", updated)
        self.assertIn("Keep identity.", updated)

    def test_plain_prompt_is_preserved_and_instruction_appended(self):
        prompt = "The camera follows him through the doorway."
        updated, injected, sections = reinforce_prompt(prompt)
        self.assertTrue(injected)
        self.assertEqual(sections, ["appended"])
        self.assertTrue(updated.startswith(prompt))
        self.assertTrue(updated.endswith(CONTINUITY_INSTRUCTION))

    def test_latest_enabled_current_state_anchor_is_selected(self):
        card = {"anchors": [
            {"anchor_id": "old", "role": "current_state", "enabled": True, "created_at": "2026-01-01"},
            {"anchor_id": "off", "role": "current_state", "enabled": False, "created_at": "2026-03-01"},
            {"anchor_id": "new", "role": "current_state", "enabled": True, "created_at": "2026-02-01"},
        ]}
        self.assertEqual(current_state_anchor(card)["anchor_id"], "new")

    def test_identity_record_is_not_mistaken_for_current_state(self):
        card = {"anchors": [{"anchor_id": "face", "role": "identity", "enabled": True}]}
        self.assertIsNone(current_state_anchor(card))

    def test_identity_frame_index_excludes_continuation_prefix(self):
        card = {"artifact_number": 3, "context_frame_count": 39, "actual_new_frame_count": 120}
        self.assertEqual(identity_source_frame(card, 0), 39)
        self.assertEqual(identity_source_frame(card, 119), 158)
        with self.assertRaisesRegex(Exception, "between 0 and 119"):
            identity_source_frame(card, 120)

    def test_identity_and_state_prompt_reinforcement_coexist(self):
        prompt = "The swimmer turns toward camera."
        updated, injected, sections = reinforce_prompt(
            prompt, current_state_active=True,
            identity_subject_id="<Subject 1>", identity_picture_index=3,
        )
        self.assertTrue(injected)
        self.assertEqual(sections, ["appended"])
        self.assertIn(CONTINUITY_INSTRUCTION, updated)
        self.assertIn(identity_instruction("<Subject 1>", 3), updated)

    def test_identity_scope_is_first_in_retention_analysis_only(self):
        prompt = "summary:\nWalk out of the pool.\n\nretention_analysis:\nKeep wet hair."
        instruction = identity_instruction("<Subject 1>", 3, "face_only")
        updated, injected, sections = reinforce_prompt(
            prompt,
            current_state_active=True,
            identity_subject_id="<Subject 1>",
            identity_picture_index=3,
            identity_scope="face_only",
        )
        self.assertTrue(injected)
        retention = updated.split("retention_analysis:", 1)[1]
        self.assertTrue(retention.lstrip().startswith(instruction))
        self.assertNotIn(instruction, updated.split("retention_analysis:", 1)[0])
        self.assertEqual(sections, ["summary", "retention_analysis"])

    def test_identity_scope_instructions_limit_unwanted_attributes(self):
        face = identity_instruction("<Subject 1>", 3, "face_only")
        self.assertIn("Do not copy its pose", face)
        self.assertIn("hair condition", face)
        self.assertIn("clothing", face)
        body = identity_instruction("<Subject 1>", 3, "face_body")
        self.assertIn("tattoos, scars, and wounds", body)
        clothing = identity_instruction("<Subject 1>", 3, "face_clothing")
        self.assertIn("clothing design", clothing)

    def test_custom_identity_scope_requires_and_injects_user_text(self):
        with self.assertRaisesRegex(Exception, "requires a custom"):
            validate_identity_scope("custom", "")
        custom = identity_instruction(
            "<Subject 1>", 3, "custom", "Use only the eyes and jawline."
        )
        self.assertIn("Use only the eyes and jawline.", custom)

if __name__ == "__main__":
    unittest.main()
