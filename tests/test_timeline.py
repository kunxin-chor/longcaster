import unittest

from longcaster.timeline import retained_frame_span, selected_cards


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "active_card_id": "draft",
            "cards": [
                {"id": "draft", "status": "DRAFT", "timeline_index": 2},
                {"id": "second", "status": "ACCEPTED", "timeline_index": 1},
                {"id": "first", "status": "ACCEPTED", "timeline_index": 0},
            ],
        }

    def test_accepted_cards_are_joined_in_timeline_order(self):
        self.assertEqual(
            [card["id"] for card in selected_cards(self.manifest, False)],
            ["first", "second"],
        )

    def test_active_draft_can_be_appended_for_review(self):
        self.assertEqual(
            [card["id"] for card in selected_cards(self.manifest, True)],
            ["first", "second", "draft"],
        )

    def test_continuation_prefix_is_excluded(self):
        self.assertEqual(retained_frame_span({"context_frame_count": 39}, 124), (39, 124))

    def test_invalid_context_is_rejected(self):
        with self.assertRaises(ValueError):
            retained_frame_span({"context_frame_count": 39}, 39)


if __name__ == "__main__":
    unittest.main()
