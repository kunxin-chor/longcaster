import unittest

from longcaster.fingerprint import generation_fingerprint


class FingerprintTests(unittest.TestCase):
    def test_dictionary_order_does_not_change_fingerprint(self):
        left = {"prompt": "walk", "sampling": {"seed": 7, "sigmas": [12.0, 0.0]}}
        right = {"sampling": {"sigmas": [12.0, 0.0], "seed": 7}, "prompt": "walk"}
        self.assertEqual(generation_fingerprint(left), generation_fingerprint(right))

    def test_schedule_change_changes_fingerprint(self):
        left = generation_fingerprint({"sigmas": [12.0, 3.0, 0.0]})
        right = generation_fingerprint({"sigmas": [12.0, 2.9, 0.0]})
        self.assertNotEqual(left, right)


if __name__ == "__main__":
    unittest.main()
