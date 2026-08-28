import os
import unittest
from unittest.mock import patch

from polytrade_esports.cli import PANDASCORE_ENABLED_ENV, _env_flag


class EnvironmentFlagTests(unittest.TestCase):
    def test_pandascore_is_disabled_when_the_flag_is_absent(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_env_flag(PANDASCORE_ENABLED_ENV, False))

    def test_common_true_and_false_values_are_accepted(self):
        for value in ("1", "true", "YES", "on"):
            with patch.dict(os.environ, {PANDASCORE_ENABLED_ENV: value}):
                self.assertTrue(_env_flag(PANDASCORE_ENABLED_ENV))
        for value in ("0", "false", "NO", "off"):
            with patch.dict(os.environ, {PANDASCORE_ENABLED_ENV: value}):
                self.assertFalse(_env_flag(PANDASCORE_ENABLED_ENV, True))

    def test_invalid_value_fails_fast(self):
        with patch.dict(os.environ, {PANDASCORE_ENABLED_ENV: "maybe"}):
            with self.assertRaisesRegex(ValueError, "must be true or false"):
                _env_flag(PANDASCORE_ENABLED_ENV)


if __name__ == "__main__":
    unittest.main()
