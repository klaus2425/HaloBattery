import unittest

from providers.xinput import TYPE_NIMH, interpret


class XInputBatteryTests(unittest.TestCase):
    def test_xinput_full_bucket_is_shown_as_a_range(self):
        level, charging, approx = interpret(TYPE_NIMH, 3, None)

        self.assertEqual(level, 85)
        self.assertFalse(charging)
        self.assertEqual(approx, "about 85% (70-100% full range)")


if __name__ == "__main__":
    unittest.main()