from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

from longcaster.refine import (
    RefineError,
    assert_protected_prefix_unchanged,
    prepare_refine_latent,
    refine_seed,
    should_refine_on_accept,
    validate_refine_cadence,
)


class _Scalar:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class _Tensor:
    def __init__(self, values):
        self.values = list(values)
        self.shape = (len(self.values),)
        self.device = "cpu"
        self.dtype = "float32"

    def to(self, **_kwargs):
        return _Tensor(self.values)

    def __le__(self, value):
        return _BoolTensor([item <= value for item in self.values])

    def __getitem__(self, key):
        if isinstance(key, _BoolTensor):
            return _Tensor([item for item, keep in zip(self.values, key.values) if keep])
        return self.values[key]

    def __sub__(self, other):
        return _Tensor([left - right for left, right in zip(self.values, other.values)])

    def abs(self):
        return _Tensor([abs(item) for item in self.values])

    def max(self):
        return _Scalar(max(self.values))


class _BoolTensor(_Tensor):
    def sum(self):
        return _Scalar(sum(self.values))


class _Joint:
    def __init__(self, streams):
        self.streams = tuple(streams)

    def unbind(self):
        return self.streams


class _FakeTorch:
    @staticmethod
    def is_tensor(value):
        return isinstance(value, _Tensor)

    @staticmethod
    def broadcast_to(value, shape):
        if value.shape != tuple(shape):
            raise RuntimeError("not broadcastable")
        return value

    @staticmethod
    def where(condition, left, right):
        return _Tensor(
            [a if choose else b for choose, a, b in zip(condition.values, left.values, right.values)]
        )

    @staticmethod
    def equal(left, right):
        return left.values == right.values


class RefineContractTests(unittest.TestCase):
    def test_cadence_and_seed_contract(self):
        self.assertEqual(validate_refine_cadence("manual"), "manual")
        with self.assertRaises(RefineError):
            validate_refine_cadence("weekly")

    def test_accept_precedence_is_off_then_auto_then_manual_checkbox(self):
        self.assertFalse(should_refine_on_accept("off", True))
        self.assertTrue(should_refine_on_accept("every_accepted_card", False))
        self.assertFalse(should_refine_on_accept("manual", False))
        self.assertTrue(should_refine_on_accept("manual", True))
        with self.assertRaises(RefineError):
            should_refine_on_accept("manual", 1)
        self.assertEqual(refine_seed(10, "inherit"), 10)
        self.assertEqual(refine_seed(10, "offset"), 11)
        with self.assertRaises(RefineError):
            refine_seed(10, "random")

    def test_first_card_refine_has_no_inherited_mask(self):
        accepted = {"samples": _Joint((_Tensor([1, 2]), _Tensor([3, 4]))), "noise_mask": "stale"}
        work, report = prepare_refine_latent(accepted, None)
        self.assertNotIn("noise_mask", work)
        self.assertEqual(report.method, "none_first_card")

    def test_joint_av_prefix_is_restored_and_verified(self):
        accepted = {"samples": _Joint((_Tensor([8, 8, 3, 4]), _Tensor([9, 6, 7, 8])))}
        lock = {
            "samples": _Joint((_Tensor([1, 2, 0, 0]), _Tensor([5, 0, 0, 0]))),
            "noise_mask": _Joint((_Tensor([0, 0, 1, 1]), _Tensor([0, 1, 1, 1]))),
        }
        with patch.dict(sys.modules, {"torch": _FakeTorch()}):
            work, report = prepare_refine_latent(accepted, lock)
            video, audio = work["samples"].unbind()
            self.assertEqual(video.values, [1, 2, 3, 4])
            self.assertEqual(audio.values, [5, 6, 7, 8])
            self.assertEqual(report.video_values, 2)
            self.assertEqual(report.audio_values, 1)
            assert_protected_prefix_unchanged(work, {"samples": work["samples"]})

            changed = {"samples": _Joint((_Tensor([99, 2, 3, 4]), audio))}
            with self.assertRaisesRegex(RefineError, "protected video"):
                assert_protected_prefix_unchanged(work, changed)

    def test_missing_audio_lock_fails_closed(self):
        accepted = {"samples": _Joint((_Tensor([1, 2]), _Tensor([3, 4])))}
        lock = {
            "samples": _Joint((_Tensor([1, 2]), _Tensor([3, 4]))),
            "noise_mask": _Joint((_Tensor([0, 1]), _Tensor([1, 1]))),
        }
        with patch.dict(sys.modules, {"torch": _FakeTorch()}):
            with self.assertRaisesRegex(RefineError, "audio.*no hard-protected"):
                prepare_refine_latent(accepted, lock)


if __name__ == "__main__":
    unittest.main()
