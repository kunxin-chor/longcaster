import unittest

from longcaster.h3_runtime import sigma_values


class FakeTensor:
    def __init__(self, values):
        self.values = values

    def detach(self):
        return self

    def cpu(self):
        return self

    def double(self):
        return self

    def flatten(self):
        return self

    def tolist(self):
        return list(self.values)


class SigmaContractTests(unittest.TestCase):
    def test_valid_external_schedule_is_read_without_replacement(self):
        sigmas = FakeTensor([12.0, 3.0, 1.0, 0.0])
        self.assertEqual(sigma_values(sigmas), [12.0, 3.0, 1.0, 0.0])

    def test_invalid_schedules_fail_early(self):
        invalid = ([0.0], [12.0, 3.0], [12.0, 13.0, 0.0], [float("nan"), 0.0])
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                sigma_values(FakeTensor(values))


if __name__ == "__main__":
    unittest.main()
