"""Tests for the pump state machine.

Run with:  python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plunge.config import AlertConfig, ConfigError, PollingConfig, PumpConfig  # noqa: E402
from plunge.detect import Detector, MonitorState, PumpState, Severity  # noqa: E402
from plunge.device import Reading  # noqa: E402

START = datetime(2026, 8, 5, 12, 0, 0)


def make_detector(**overrides) -> Detector:
    pump = PumpConfig(running_watts=80.8, running_tolerance_pct=6.0, stopped_max_watts=5.0)
    polling = PollingConfig(
        interval_seconds=60, confirm_samples=3, unreachable_samples=3, recovery_samples=2
    )
    alerts = AlertConfig(cooldown_seconds=1800, send_recovery=True, drift_alert_pct=4.0)
    for key, value in overrides.items():
        for target in (pump, polling, alerts):
            if hasattr(target, key):
                setattr(target, key, value)
    state = MonitorState(state=PumpState.RUNNING, since=START)
    return Detector(pump, polling, alerts, state)


def reading(watts: float, is_on: bool = True, volts: float = None) -> Reading:
    return Reading(timestamp=START, is_on=is_on, watts=watts, volts=volts)


class TestClassification(unittest.TestCase):
    """Band edges are where off-by-one errors hide."""

    def setUp(self):
        self.detector = make_detector()
        # 80.8W +/- 6% => 75.952 .. 85.648
        self.low = self.detector.pump.running_min
        self.high = self.detector.pump.running_max

    def test_nominal_draw_is_running(self):
        self.assertIs(self.detector.classify(reading(80.8)), PumpState.RUNNING)

    def test_band_edges_are_inclusive(self):
        self.assertIs(self.detector.classify(reading(self.low)), PumpState.RUNNING)
        self.assertIs(self.detector.classify(reading(self.high)), PumpState.RUNNING)

    def test_just_outside_band(self):
        self.assertIs(self.detector.classify(reading(self.low - 0.01)), PumpState.CAVITATING)
        self.assertIs(self.detector.classify(reading(self.high + 0.01)), PumpState.STRAINING)

    def test_standby_draw_is_stopped(self):
        # The plug itself idles around 0.94W with nothing running.
        self.assertIs(self.detector.classify(reading(0.94)), PumpState.STOPPED)
        self.assertIs(self.detector.classify(reading(0.0)), PumpState.STOPPED)

    def test_stopped_threshold_is_inclusive(self):
        self.assertIs(self.detector.classify(reading(5.0)), PumpState.STOPPED)
        self.assertIs(self.detector.classify(reading(5.01)), PumpState.CAVITATING)

    def test_relay_off_outranks_wattage(self):
        self.assertIs(self.detector.classify(reading(80.8, is_on=False)), PumpState.PLUG_OFF)


class TestDebounce(unittest.TestCase):
    def test_single_bad_reading_does_not_alert(self):
        detector = make_detector()
        self.assertIsNone(detector.observe(PumpState.STOPPED, START, ""))
        self.assertIs(detector.state.state, PumpState.RUNNING)

    def test_alerts_only_on_the_third_consecutive_reading(self):
        detector = make_detector()
        now = START
        for _ in range(2):
            now += timedelta(minutes=1)
            self.assertIsNone(detector.observe(PumpState.STOPPED, now, ""))

        now += timedelta(minutes=1)
        alert = detector.observe(PumpState.STOPPED, now, "")
        self.assertIsNotNone(alert)
        self.assertIs(alert.severity, Severity.CRITICAL)
        self.assertIs(detector.state.state, PumpState.STOPPED)

    def test_a_healthy_reading_resets_the_streak(self):
        detector = make_detector()
        now = START
        for _ in range(2):
            now += timedelta(minutes=1)
            detector.observe(PumpState.STOPPED, now, "")

        now += timedelta(minutes=1)
        detector.observe(PumpState.RUNNING, now, "")
        self.assertEqual(detector.state.pending_count, 0)

        # Streak restarts from scratch, so two more are still not enough.
        for _ in range(2):
            now += timedelta(minutes=1)
            self.assertIsNone(detector.observe(PumpState.STOPPED, now, ""))

    def test_flapping_between_two_faults_does_not_alert(self):
        detector = make_detector()
        now = START
        for state in (PumpState.STOPPED, PumpState.CAVITATING) * 4:
            now += timedelta(minutes=1)
            self.assertIsNone(detector.observe(state, now, ""))
        self.assertIs(detector.state.state, PumpState.RUNNING)

    def test_unreachable_uses_its_own_threshold(self):
        detector = make_detector(unreachable_samples=5)
        now = START
        for _ in range(4):
            now += timedelta(minutes=1)
            self.assertIsNone(detector.observe(PumpState.UNREACHABLE, now, ""))
        now += timedelta(minutes=1)
        self.assertIsNotNone(detector.observe(PumpState.UNREACHABLE, now, ""))


class TestRecovery(unittest.TestCase):
    def _fail(self, detector, now, state=PumpState.STOPPED):
        for _ in range(3):
            now += timedelta(minutes=1)
            detector.observe(state, now, "")
        return now

    def test_recovery_alert_after_confirmed_failure(self):
        detector = make_detector()
        now = self._fail(detector, START)

        now += timedelta(minutes=1)
        self.assertIsNone(detector.observe(PumpState.RUNNING, now, ""))  # needs 2

        now += timedelta(minutes=1)
        alert = detector.observe(PumpState.RUNNING, now, "")
        self.assertIsNotNone(alert)
        self.assertIs(alert.severity, Severity.INFO)
        self.assertIn("recovered", alert.title.lower())

    def test_recovery_can_be_disabled(self):
        detector = make_detector(send_recovery=False)
        now = self._fail(detector, START)
        for _ in range(2):
            now += timedelta(minutes=1)
            self.assertIsNone(detector.observe(PumpState.RUNNING, now, ""))
        self.assertIs(detector.state.state, PumpState.RUNNING)

    def test_no_recovery_alert_when_never_broken(self):
        detector = make_detector()
        now = START
        for _ in range(5):
            now += timedelta(minutes=1)
            self.assertIsNone(detector.observe(PumpState.RUNNING, now, ""))


class TestCooldown(unittest.TestCase):
    def test_ongoing_failure_stays_quiet_during_cooldown(self):
        detector = make_detector()
        now = START
        for _ in range(3):
            now += timedelta(minutes=1)
            detector.observe(PumpState.STOPPED, now, "")

        for _ in range(20):
            now += timedelta(minutes=1)
            self.assertIsNone(detector.observe(PumpState.STOPPED, now, ""))

    def test_reminder_fires_once_cooldown_lapses(self):
        detector = make_detector(cooldown_seconds=600)
        now = START
        for _ in range(3):
            now += timedelta(minutes=1)
            detector.observe(PumpState.STOPPED, now, "")

        now += timedelta(minutes=11)
        alert = detector.observe(PumpState.STOPPED, now, "")
        self.assertIsNotNone(alert)
        self.assertIn("STILL", alert.title)


class TestDrift(unittest.TestCase):
    def _history(self, watts, count=60, now=START):
        return [
            {
                "t": (now - timedelta(minutes=i)).isoformat(),
                "on": True,
                "w": watts,
            }
            for i in range(count)
        ]

    def test_no_alert_when_holding_at_baseline(self):
        detector = make_detector()
        self.assertIsNone(detector.check_drift(self._history(80.8), START))

    def test_alerts_on_sustained_upward_drift(self):
        detector = make_detector()
        # +5%, inside the 6% instantaneous band but past the 4% drift threshold.
        alert = detector.check_drift(self._history(80.8 * 1.05), START)
        self.assertIsNotNone(alert)
        self.assertIn("higher", alert.title)
        self.assertIn("clogging", alert.body)

    def test_alerts_on_sustained_downward_drift(self):
        detector = make_detector()
        alert = detector.check_drift(self._history(80.8 * 0.95), START)
        self.assertIsNotNone(alert)
        self.assertIn("lower", alert.title)

    def test_too_few_samples_is_not_enough_evidence(self):
        detector = make_detector()
        self.assertIsNone(detector.check_drift(self._history(80.8 * 1.05, count=10), START))

    def test_stale_history_is_ignored(self):
        detector = make_detector()
        old = self._history(80.8 * 1.05, now=START - timedelta(days=3))
        self.assertIsNone(detector.check_drift(old, START))

    def test_standby_readings_excluded_from_the_median(self):
        detector = make_detector()
        history = self._history(80.8, count=60) + self._history(0.94, count=40)
        self.assertIsNone(detector.check_drift(history, START))

    def test_drift_alert_respects_its_cooldown(self):
        detector = make_detector()
        self.assertIsNotNone(detector.check_drift(self._history(80.8 * 1.05), START))
        later = START + timedelta(hours=6)
        self.assertIsNone(detector.check_drift(self._history(80.8 * 1.05, now=later), later))


class TestStatePersistence(unittest.TestCase):
    def test_round_trip_preserves_everything(self):
        original = MonitorState(
            state=PumpState.STRAINING,
            since=START,
            pending_state=PumpState.RUNNING,
            pending_count=1,
            last_alert_at={"state:straining": START.isoformat()},
            last_reading={"t": START.isoformat(), "on": True, "w": 91.2},
        )
        restored = MonitorState.from_dict(original.to_dict())
        self.assertEqual(restored.to_dict(), original.to_dict())

    def test_unknown_state_falls_back_to_running(self):
        restored = MonitorState.from_dict({"state": "bogus"})
        self.assertIs(restored.state, PumpState.RUNNING)

    def test_debounce_survives_a_restart(self):
        """The old monitor reset its counter every cron run, so it never alerted."""
        detector = make_detector()
        now = START
        for _ in range(2):
            now += timedelta(minutes=1)
            detector.observe(PumpState.STOPPED, now, "")

        # Simulate the process dying and coming back.
        revived = make_detector()
        revived.state = MonitorState.from_dict(detector.state.to_dict())

        now += timedelta(minutes=1)
        self.assertIsNotNone(revived.observe(PumpState.STOPPED, now, ""))


class TestVoltageNormalization(unittest.TestCase):
    """Mains voltage moved 113V -> 122V overnight and shifted the pump's draw 3%.

    That is grid behaviour, not a pump fault, so it must not consume alert budget.
    """

    def pump(self, exponent=0.4, reference=113.16):
        return PumpConfig(
            running_watts=80.58,
            running_tolerance_pct=6.0,
            stopped_max_watts=5.0,
            voltage_reference=reference,
            voltage_exponent=exponent,
        )

    def test_disabled_by_default(self):
        pump = PumpConfig(running_watts=80.58)
        self.assertFalse(pump.normalizes_voltage)
        self.assertEqual(pump.normalize(83.0, 122.0), 83.0)

    def test_reading_at_reference_voltage_is_unchanged(self):
        pump = self.pump()
        self.assertAlmostEqual(pump.normalize(80.58, 113.16), 80.58, places=6)

    def test_higher_voltage_is_adjusted_down(self):
        pump = self.pump()
        self.assertLess(pump.normalize(83.03, 122.08), 83.03)

    def test_lower_voltage_is_adjusted_up(self):
        pump = self.pump()
        self.assertGreater(pump.normalize(79.0, 108.0), 79.0)

    def test_collapses_the_two_real_observations(self):
        """Both measured points should land on the same normalized draw."""
        pump = self.pump()
        evening = pump.normalize(80.578, 113.157)
        morning = pump.normalize(83.028, 122.08)
        self.assertAlmostEqual(evening, morning, delta=0.15)

    def test_missing_voltage_falls_back_to_raw(self):
        pump = self.pump()
        self.assertEqual(pump.normalize(83.0, None), 83.0)
        self.assertEqual(pump.normalize(83.0, 0), 83.0)

    def test_high_voltage_no_longer_reads_as_straining(self):
        """The 122V morning reading sits near the top of a 6% band without normalization."""
        tight = PumpConfig(running_watts=80.58, running_tolerance_pct=3.0, stopped_max_watts=5.0)
        raw_detector = Detector(tight, PollingConfig(), AlertConfig())
        self.assertIs(raw_detector.classify(reading(83.03, volts=122.08)), PumpState.STRAINING)

        normalized = PumpConfig(
            running_watts=80.58,
            running_tolerance_pct=3.0,
            stopped_max_watts=5.0,
            voltage_reference=113.16,
            voltage_exponent=0.4,
        )
        fixed_detector = Detector(normalized, PollingConfig(), AlertConfig())
        self.assertIs(fixed_detector.classify(reading(83.03, volts=122.08)), PumpState.RUNNING)

    def test_stopped_check_ignores_normalization(self):
        """At standby draw there is nothing meaningful to scale."""
        pump = self.pump(exponent=3.0)
        detector = Detector(pump, PollingConfig(), AlertConfig())
        self.assertIs(detector.classify(reading(0.94, volts=122.0)), PumpState.STOPPED)

    def test_drift_uses_normalized_history(self):
        detector = Detector(self.pump(), PollingConfig(), AlertConfig())
        # A day at high voltage: raw draw is ~3% up, but that is purely the grid.
        history = [
            {
                "t": (START - timedelta(minutes=i)).isoformat(),
                "on": True,
                "w": 83.028,
                "v": 122.08,
            }
            for i in range(60)
        ]
        self.assertIsNone(detector.check_drift(history, START))

    def test_exponent_without_reference_is_rejected(self):
        with self.assertRaises(ConfigError):
            PumpConfig.from_dict({"running_watts": 80.6, "voltage_exponent": 0.4})

    def test_nonpositive_reference_is_rejected(self):
        with self.assertRaises(ConfigError):
            PumpConfig.from_dict({"running_watts": 80.6, "voltage_reference": 0})


class TestConfigValidation(unittest.TestCase):
    def test_rejects_stopped_threshold_overlapping_the_band(self):
        with self.assertRaises(ConfigError):
            PumpConfig.from_dict(
                {"running_watts": 80.8, "running_tolerance_pct": 6.0, "stopped_max_watts": 90.0}
            )

    def test_uncalibrated_band_access_raises(self):
        with self.assertRaises(ConfigError):
            _ = PumpConfig().running_min

    def test_rejects_nonpositive_tolerance(self):
        with self.assertRaises(ConfigError):
            PumpConfig.from_dict({"running_watts": 80.8, "running_tolerance_pct": 0})


if __name__ == "__main__":
    unittest.main()
