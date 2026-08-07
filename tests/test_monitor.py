"""Tests for orchestration: startup announcement, pruning, and log rotation.

No device and no network. `Monitor.__init__` does no I/O beyond reading state
off disk, so a real Monitor can be built against a temp config and exercised
without ever asking the plug for a reading.
"""

import argparse
import asyncio
import contextlib
import io
import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pump_monitor  # noqa: E402
from plunge.config import Config, ConfigError, StorageConfig  # noqa: E402
from plunge.detect import PumpState, Severity  # noqa: E402
from plunge.monitor import Monitor  # noqa: E402
from plunge.notify import Channel  # noqa: E402


def write_config(tmp: Path, **storage) -> Config:
    """A minimal, calibrated config rooted in a temp dir."""
    settings = {
        "device": {"ip": "10.0.0.9", "username": "u", "password": "p"},
        "pump": {
            "running_watts": 80.0,
            "running_tolerance_pct": 10.0,
            "stopped_max_watts": 5.0,
        },
        "polling": {"interval_seconds": 60},
        "alerts": {"channels": [{"type": "log", "enabled": True}]},
        "storage": {
            "history_file": "h.jsonl",
            "state_file": "s.json",
            "log_file": "m.log",
            "retain_days": 30.0,
            **storage,
        },
    }
    path = tmp / "config.json"
    path.write_text(json.dumps(settings))
    return Config.load(str(path))


def write_history(path: Path, ages_in_days) -> None:
    """One reading per given age, oldest first."""
    now = datetime.now()
    with open(path, "w") as handle:
        for age in ages_in_days:
            stamp = (now - timedelta(days=age)).isoformat(timespec="seconds")
            handle.write(json.dumps({"t": stamp, "on": True, "w": 80.0, "v": 120.0}) + "\n")


class RecordingChannel(Channel):
    type_name = "recording"

    def __init__(self, settings=None):
        super().__init__(settings or {})
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


class MonitorTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def build(self, **storage) -> Monitor:
        return Monitor(write_config(self.tmp, **storage))

    def recording(self, monitor: Monitor, **settings) -> RecordingChannel:
        """Swap the notifier's channels for one that just records."""
        recorder = RecordingChannel(settings)
        monitor.notifier.channels = [recorder]
        return recorder


class TestStartupAnnouncement(MonitorTestCase):
    """A monitor that died and one that is quietly healthy look identical from
    the outside. The startup ping is what distinguishes them."""

    def test_announces_exactly_one_alert(self):
        monitor = self.build()
        recorder = self.recording(monitor)
        monitor.announce_startup()
        self.assertEqual(len(recorder.sent), 1)

    def test_severity_is_info(self):
        monitor = self.build()
        recorder = self.recording(monitor)
        monitor.announce_startup()
        self.assertIs(recorder.sent[0].severity, Severity.INFO)

    def test_body_carries_the_alert_band(self):
        # 80.0W +/- 10% => 72.0 .. 88.0
        monitor = self.build()
        recorder = self.recording(monitor)
        monitor.announce_startup()
        body = recorder.sent[0].body
        self.assertIn("72.0", body)
        self.assertIn("88.0", body)

    def test_body_names_the_device_and_interval(self):
        monitor = self.build()
        recorder = self.recording(monitor)
        monitor.announce_startup()
        body = recorder.sent[0].body
        self.assertIn("10.0.0.9", body)
        self.assertIn("60s", body)

    def test_reports_last_known_state_on_a_clean_start(self):
        monitor = self.build()
        recorder = self.recording(monitor)
        monitor.announce_startup()
        self.assertIn("Last known state: running", recorder.sent[0].body)

    def test_restarting_into_a_problem_says_so(self):
        """The case that matters: the box rebooted while the pump was dead, and
        the ping must not imply everything is fine."""
        monitor = self.build()
        monitor.detector.state.state = PumpState.STOPPED
        recorder = self.recording(monitor)
        monitor.announce_startup()
        self.assertIn("Last known state: stopped", recorder.sent[0].body)

    def test_a_critical_only_channel_is_not_woken_by_startup(self):
        """INFO severity is deliberate: a phone set to critical-only should not
        buzz every time the service restarts."""
        monitor = self.build()
        recorder = self.recording(monitor, min_severity="critical")
        monitor.announce_startup()
        self.assertEqual(recorder.sent, [])

    def test_a_failing_channel_does_not_stop_startup(self):
        monitor = self.build()

        class Exploding(Channel):
            type_name = "exploding"

            def send(self, alert):
                raise RuntimeError("down")

        recorder = RecordingChannel()
        monitor.notifier.channels = [Exploding({}), recorder]
        with self.assertLogs("plunge.notify", level="ERROR"):
            monitor.announce_startup()
        self.assertEqual(len(recorder.sent), 1)


class TestPrune(MonitorTestCase):
    def test_drops_entries_past_the_retention_window(self):
        monitor = self.build(retain_days=30.0)
        write_history(monitor.history_path, [60, 45, 31, 10, 1])
        removed = monitor.prune()
        self.assertEqual(removed, 3)

    def test_keeps_everything_inside_the_window(self):
        monitor = self.build(retain_days=30.0)
        write_history(monitor.history_path, [29, 10, 1])
        self.assertEqual(monitor.prune(), 0)
        self.assertEqual(len(open(monitor.history_path).readlines()), 3)

    def test_survivors_are_the_recent_ones(self):
        monitor = self.build(retain_days=30.0)
        write_history(monitor.history_path, [90, 2])
        monitor.prune()
        rows = [json.loads(line) for line in open(monitor.history_path)]
        self.assertEqual(len(rows), 1)
        age = datetime.now() - datetime.fromisoformat(rows[0]["t"])
        self.assertLess(age.days, 30)

    def test_missing_history_is_not_an_error(self):
        monitor = self.build()
        self.assertFalse(monitor.history_path.exists())
        self.assertEqual(monitor.prune(), 0)

    def test_zero_retention_disables_pruning(self):
        monitor = self.build(retain_days=0.0)
        write_history(monitor.history_path, [500, 400])
        self.assertEqual(monitor.prune(), 0)
        self.assertEqual(len(open(monitor.history_path).readlines()), 2)

    def test_repeated_short_lived_runs_still_prune(self):
        """The `once` regression. Cron re-enters the process every poll, so the
        poll loop's periodic prune never fires and `_polls` resets to zero each
        time. A fresh Monitor must still trim on demand."""
        config = write_config(self.tmp, retain_days=30.0)
        history = config.resolve(config.storage.history_file)
        write_history(history, [90, 80, 70, 2])

        for _ in range(3):
            monitor = Monitor(config)  # a brand new process, as cron would
            self.assertEqual(monitor._polls, 0)
            monitor.prune()

        rows = [json.loads(line) for line in open(history)]
        self.assertEqual(len(rows), 1, "old readings survived repeated short runs")


class TestWatchStartupWiring(MonitorTestCase):
    """announce_startup() and prune() working in isolation does not prove that
    watch() actually calls them on the way into the loop."""

    def test_watch_announces_and_prunes_before_looping(self):
        config = write_config(self.tmp, retain_days=30.0)
        history = config.resolve(config.storage.history_file)
        write_history(history, [90, 2])

        monitor = Monitor(config)
        recorder = self.recording(monitor)

        async def stop_on_first_poll(self):
            raise asyncio.CancelledError

        with mock.patch.object(Monitor, "poll_once", stop_on_first_poll):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(monitor.watch())

        self.assertEqual(len(recorder.sent), 1, "watch() did not announce startup")
        rows = [json.loads(line) for line in open(history)]
        self.assertEqual(len(rows), 1, "watch() did not prune on startup")

    def test_startup_is_announced_before_the_first_poll(self):
        """If the ping came after the first poll, a plug that hangs on connect
        would delay the only signal that the service came up at all."""
        monitor = self.build()
        recorder = self.recording(monitor)
        order = []

        async def record_poll(self):
            order.append("poll")
            raise asyncio.CancelledError

        real_announce = Monitor.announce_startup

        def record_announce(self):
            order.append("announce")
            return real_announce(self)

        with mock.patch.object(Monitor, "poll_once", record_poll):
            with mock.patch.object(Monitor, "announce_startup", record_announce):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(monitor.watch())

        self.assertEqual(order, ["announce", "poll"])
        self.assertEqual(len(recorder.sent), 1)


class TestOnceCommandWiring(MonitorTestCase):
    """Exercising Monitor.prune() directly proves the method works, not that the
    `once` subcommand actually calls it. This closes that gap."""

    def setUp(self):
        super().setUp()
        self._handlers = logging.root.handlers[:]
        self.addCleanup(self._restore)

    def _restore(self):
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        for handler in self._handlers:
            logging.root.addHandler(handler)

    def test_once_prunes_before_polling(self):
        config = write_config(self.tmp, retain_days=30.0)
        history = config.resolve(config.storage.history_file)
        write_history(history, [90, 60, 3])

        async def no_poll(self):
            return None

        args = argparse.Namespace(
            command="once", config=str(config.path), verbose=False
        )
        with mock.patch.object(Monitor, "poll_once", no_poll):
            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(pump_monitor.run(args))

        rows = [json.loads(line) for line in open(history)]
        self.assertEqual(len(rows), 1, "`once` did not prune; cron history would grow forever")


class TestLogRotationConfig(unittest.TestCase):
    def test_defaults(self):
        storage = StorageConfig.from_dict({})
        self.assertEqual(storage.log_max_mb, 5.0)
        self.assertEqual(storage.log_backups, 3)

    def test_values_are_read_from_config(self):
        storage = StorageConfig.from_dict({"log_max_mb": 2.5, "log_backups": 1})
        self.assertEqual(storage.log_max_mb, 2.5)
        self.assertEqual(storage.log_backups, 1)

    def test_zero_size_is_rejected(self):
        # maxBytes=0 means "never rotate" in logging, which is the exact bug
        # this config exists to prevent.
        with self.assertRaises(ConfigError):
            StorageConfig.from_dict({"log_max_mb": 0})

    def test_negative_size_is_rejected(self):
        with self.assertRaises(ConfigError):
            StorageConfig.from_dict({"log_max_mb": -1})

    def test_negative_backup_count_is_rejected(self):
        with self.assertRaises(ConfigError):
            StorageConfig.from_dict({"log_backups": -1})

    def test_zero_backups_is_allowed(self):
        """Truncate-in-place, keeping no history, is a legitimate choice."""
        self.assertEqual(StorageConfig.from_dict({"log_backups": 0}).log_backups, 0)


class TestLogRotationBehaviour(MonitorTestCase):
    """setup_logging reconfigures root logging, so save and restore it."""

    def setUp(self):
        super().setUp()
        self._handlers = logging.root.handlers[:]
        self._level = logging.root.level
        self.addCleanup(self._restore)

    def _restore(self):
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
        for handler in self._handlers:
            logging.root.addHandler(handler)
        logging.root.setLevel(self._level)

    def _configure(self, **storage) -> Config:
        config = write_config(self.tmp, **storage)
        # setup_logging also attaches a stdout handler; keep it out of the run.
        with contextlib.redirect_stdout(io.StringIO()):
            pump_monitor.setup_logging(config, False)
        return config

    def test_installs_a_rotating_handler(self):
        self._configure()
        files = [h for h in logging.root.handlers if isinstance(h, logging.FileHandler)]
        self.assertEqual(len(files), 1)
        self.assertIsInstance(files[0], RotatingFileHandler)

    def test_handler_uses_the_configured_limits(self):
        self._configure(log_max_mb=2.0, log_backups=4)
        handler = next(
            h for h in logging.root.handlers if isinstance(h, RotatingFileHandler)
        )
        self.assertEqual(handler.maxBytes, 2 * 1024 * 1024)
        self.assertEqual(handler.backupCount, 4)

    def test_log_is_capped_under_sustained_writes(self):
        """The property that actually matters: write far more than the cap and
        the bytes on disk stay bounded."""
        cap_bytes = 1024
        config = self._configure(log_max_mb=cap_bytes / (1024 * 1024), log_backups=2)
        log_path = config.resolve(config.storage.log_file)

        logger = logging.getLogger("rotation.test")
        line = "x" * 80
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(400):
                logger.info("%d %s", i, line)

        produced = sorted(log_path.parent.glob("m.log*"))
        total = sum(p.stat().st_size for p in produced)

        # 400 lines of ~120B is ~48KB written; at most 3 files x 1KB survives.
        self.assertLessEqual(len(produced), 3, f"too many files: {produced}")
        self.assertLess(total, cap_bytes * 4, f"log grew to {total}B")
        self.assertFalse((log_path.parent / "m.log.3").exists())

    def test_rotation_keeps_the_newest_lines(self):
        """Rotation must not cost you the most recent events."""
        config = self._configure(log_max_mb=1024 / (1024 * 1024), log_backups=1)
        log_path = config.resolve(config.storage.log_file)

        logger = logging.getLogger("rotation.recency")
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(200):
                logger.info("sequence marker %d padded %s", i, "y" * 60)
            logger.info("FINAL MARKER")

        self.assertIn("FINAL MARKER", log_path.read_text())


if __name__ == "__main__":
    unittest.main()
