"""Tests for config persistence.

The concern here is the file the calibration gets written back into: it holds
the TP-Link account password and the ntfy topic, so how it lands on disk is a
security property, not a cosmetic one.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plunge.config import Config, PumpConfig  # noqa: E402


def write_config(tmp: Path, mode: int) -> Path:
    settings = {
        "device": {"ip": "10.0.0.9", "username": "u", "password": "secret"},
        "pump": {
            "running_watts": 80.0,
            "running_tolerance_pct": 10.0,
            "stopped_max_watts": 5.0,
        },
        "polling": {"interval_seconds": 60},
        "alerts": {"channels": [{"type": "log", "enabled": True}]},
        "storage": {"history_file": "h.jsonl", "state_file": "s.json", "log_file": "m.log"},
    }
    path = tmp / "config.json"
    path.write_text(json.dumps(settings))
    os.chmod(path, mode)
    return path


class TestCalibrationPreservesPermissions(unittest.TestCase):
    """install.sh chmod 600s the config; calibrate must not undo that.

    The write goes to a temp file and is renamed into place, and a rename keeps
    the *source* file's mode. The temp file is created under the process umask,
    so without an explicit chmod a 0600 config silently becomes 0644 on every
    calibration.
    """

    def test_restrictive_mode_survives_a_calibration_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(Path(tmp), 0o600)
            config = Config.load(str(path))

            config.save_pump_calibration(
                PumpConfig(running_watts=127.459, running_tolerance_pct=10.0)
            )

            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(
                mode,
                0o600,
                f"config was widened to {oct(mode)}; the password is readable by any local user",
            )

    def test_calibration_actually_landed(self):
        """Guards the obvious wrong fix: don't pass the permission test by not writing."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(Path(tmp), 0o600)
            config = Config.load(str(path))

            config.save_pump_calibration(
                PumpConfig(running_watts=127.459, running_tolerance_pct=10.0)
            )

            raw = json.loads(path.read_text())
            self.assertAlmostEqual(raw["pump"]["running_watts"], 127.459)
            self.assertEqual(raw["device"]["password"], "secret", "unrelated keys must survive")

    def test_no_temp_file_is_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_config(Path(tmp), 0o600)
            config = Config.load(str(path))

            config.save_pump_calibration(
                PumpConfig(running_watts=127.459, running_tolerance_pct=10.0)
            )

            self.assertEqual([p.name for p in Path(tmp).glob("*.tmp")], [])


if __name__ == "__main__":
    unittest.main()
