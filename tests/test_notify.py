"""Tests for alert routing. No network — channels are stubbed."""

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plunge.detect import Alert, Severity  # noqa: E402
from plunge.notify import Channel, Notifier  # noqa: E402

# Several tests deliberately trigger warnings. Keep them out of the test output;
# assertLogs still captures them.
_notify_log = logging.getLogger("plunge.notify")
_notify_log.addHandler(logging.NullHandler())
_notify_log.propagate = False


class RecordingChannel(Channel):
    type_name = "recording"

    def __init__(self, settings):
        super().__init__(settings)
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)


class ExplodingChannel(Channel):
    type_name = "exploding"

    def send(self, alert):
        raise RuntimeError("channel is down")


def alert(severity=Severity.CRITICAL) -> Alert:
    return Alert(key="k", severity=severity, title="t", body="b")


class TestSeverityFiltering(unittest.TestCase):
    def test_channel_accepts_at_or_above_its_floor(self):
        channel = RecordingChannel({"min_severity": "warning"})
        self.assertTrue(channel.accepts(alert(Severity.CRITICAL)))
        self.assertTrue(channel.accepts(alert(Severity.WARNING)))
        self.assertFalse(channel.accepts(alert(Severity.INFO)))

    def test_default_floor_passes_everything(self):
        channel = RecordingChannel({})
        self.assertTrue(channel.accepts(alert(Severity.INFO)))

    def test_unknown_floor_falls_back_to_info(self):
        channel = RecordingChannel({"min_severity": "catastrophic"})
        self.assertTrue(channel.accepts(alert(Severity.INFO)))


class TestNotifier(unittest.TestCase):
    def test_disabled_channels_are_skipped(self):
        notifier = Notifier([{"type": "log", "enabled": False}])
        # Falls back to a log channel rather than going silent.
        self.assertEqual([c.type_name for c in notifier.channels], ["log"])

    def test_unknown_type_is_ignored_not_fatal(self):
        notifier = Notifier([{"type": "carrier-pigeon", "enabled": True}])
        self.assertEqual([c.type_name for c in notifier.channels], ["log"])

    def test_empty_config_still_logs(self):
        self.assertEqual([c.type_name for c in Notifier([]).channels], ["log"])

    def test_none_alert_is_a_noop(self):
        notifier = Notifier([])
        recorder = RecordingChannel({})
        notifier.channels = [recorder]
        notifier.send(None)
        self.assertEqual(recorder.sent, [])

    def test_one_failing_channel_does_not_block_the_others(self):
        notifier = Notifier([])
        recorder = RecordingChannel({})
        notifier.channels = [ExplodingChannel({}), recorder, ExplodingChannel({})]
        with self.assertLogs("plunge.notify", level="ERROR") as captured:
            notifier.send(alert())
        self.assertEqual(len(recorder.sent), 1)
        self.assertEqual(len(captured.records), 2)

    def test_missing_required_setting_is_reported_not_raised(self):
        notifier = Notifier([{"type": "ntfy", "enabled": True}])  # no topic
        with self.assertLogs("plunge.notify", level="ERROR"):
            notifier.send(alert())


if __name__ == "__main__":
    unittest.main()
