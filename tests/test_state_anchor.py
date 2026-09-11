import unittest

from longcaster.state_anchor import (
    CONTINUITY_INSTRUCTION,
    continuation_anchor_frame,
    current_state_anchor,
    reinforce_prompt,
)


class StateAnchorTests(unittest.TestCase):
    def test_anchor_aligns_to_end_of_continuation_prefix(self):
        self.assertEqual(continuation_anchor_frame(39), 38)
        with self.assertRaises(ValueError):
            continuation_anchor_frame(0)

    def test_prompt_sections_receive_minimal_instruction(self):
        prompt = "<summary>Walk into the room.</summary>\n<retention_analysis>Keep identity.</retention_analysis>"
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

if __name__ == "__main__":
    unittest.main()
