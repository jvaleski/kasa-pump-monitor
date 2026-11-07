"""Tests for the read-only thermostat logger. No device, no network."""

import json
import logging
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plunge.config import ConfigError  # noqa: E402
from plunge.inkbird import InkbirdClient, InkbirdError, parse_dps  # noqa: E402
from plunge.thermo_config import InkbirdConfig, ThermoConfig  # noqa: E402
from plunge.thermo_logger import ThermoLogger, summarize  # noqa: E402
from plunge.weather import OutdoorTemperature  # noqa: E402

# Captured from the real controller on 2026-11-07, right after PT and AH were set.
REAL_DPS = {
    "12": 0, "101": "F", "102": 0, "104": 74, "106": 450, "108": 4, "109": 520,
    "110": 370, "111": False, "112": False, "113": False, "115": "1",
    "116": 453, "117": 30, "118": 30,
}


class FakeDevice:
    """Stands in for tinytuya.Device. Any write is a test failure."""

    def __init__(self, responses):
        self.responses = responses

    def status(self):
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def set_value(self, *args, **kwargs):
        raise AssertionError("the logger must never write to the controller")

    set_multiple_values = set_value
    turn_on = set_value
    turn_off = set_value


def make_client(responses, retries=3):
    config = InkbirdConfig(device_id="d", ip="10.0.0.5", local_key="k", retries=retries)
    device = FakeDevice(list(responses))
    return InkbirdClient(config, device_factory=lambda _cfg: device, sleep=lambda _s: None), device


class TestParseDps(unittest.TestCase):
    def test_real_payload(self):
        r = parse_dps(REAL_DPS, datetime(2026, 11, 7, 10, 30))
        self.assertEqual(r.water_f, 45.3)
        self.assertEqual(r.target_f, 45.0)
        self.assertEqual((r.heat_diff_f, r.cool_diff_f), (3.0, 3.0))
        self.assertEqual(r.alarm_high_f, 52.0)
        self.assertEqual(r.alarm_low_f, 37.0)
        self.assertEqual(r.compressor_delay_min, 4)
        self.assertEqual(r.output_name, "cooling")

    def test_celsius_unit_converts_settings_and_scales_differentials(self):
        dps = dict(REAL_DPS, **{"101": "C", "106": 72, "117": 17, "118": 17})
        r = parse_dps(dps)
        self.assertAlmostEqual(r.target_f, 44.96, places=2)
        # A 1.7°C span is a 3.06°F span: no +32 offset on a differential.
        self.assertAlmostEqual(r.heat_diff_f, 3.06, places=2)
        # dp 116 is always °F regardless of the display unit.
        self.assertEqual(r.water_f, 45.3)

    def test_missing_required_dp_raises(self):
        partial = {k: v for k, v in REAL_DPS.items() if k != "116"}
        with self.assertRaises(InkbirdError):
            parse_dps(partial)

    def test_record_is_compact_and_skips_empty_fields(self):
        record = parse_dps(REAL_DPS, datetime(2026, 11, 7, 10, 30)).to_dict()
        self.assertEqual(record["t"], "2026-11-07T10:30:00")
        self.assertEqual(record["out"], "1")
        self.assertNotIn("fault", record)  # 0 means no fault
        self.assertNotIn("amb", record)


class TestInkbirdClient(unittest.TestCase):
    def test_retries_through_a_refused_connection(self):
        refused = {"Error": "Network Error: Unable to Connect", "Err": "901", "Payload": None}
        client, _ = make_client([refused, {"dps": REAL_DPS}])
        self.assertEqual(client.read().water_f, 45.3)

    def test_retries_a_partial_status(self):
        client, _ = make_client([{"dps": {"104": 74}}, {"dps": REAL_DPS}])
        self.assertEqual(client.read().target_f, 45.0)

    def test_gives_up_with_hint_after_retries(self):
        client, _ = make_client([OSError("boom")] * 3)
        with self.assertRaises(InkbirdError) as ctx:
            client.read()
        self.assertIn("3 attempts", str(ctx.exception))
        self.assertIn("local key", ctx.exception.hint)

    def test_client_has_no_write_path(self):
        self.assertFalse(any(name.startswith(("set", "write")) for name in dir(InkbirdClient)))


class FakeResponse:
    def __init__(self, temp):
        self.temp = temp

    def raise_for_status(self):
        pass

    def json(self):
        return {"current": {"temperature_2m": self.temp}}


class FakeSession:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)


class TestOutdoorTemperature(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]

    def make(self, results):
        session = FakeSession(results)
        return OutdoorTemperature(40.0, -105.3, refresh_seconds=900, session=session,
                                  clock=lambda: self.now[0]), session

    def test_caches_between_refreshes(self):
        outdoor, session = self.make([52.4, 60.0])
        self.assertEqual(outdoor.current(), 52.4)
        self.now[0] = 600
        self.assertEqual(outdoor.current(), 52.4)
        self.assertEqual(session.calls, 1)
        self.now[0] = 901
        self.assertEqual(outdoor.current(), 60.0)

    def test_reuses_recent_value_when_fetch_fails(self):
        outdoor, _ = self.make([52.4, ConnectionError("down")])
        outdoor.current()
        self.now[0] = 1000
        with self.assertLogs("plunge.weather", logging.WARNING):
            self.assertEqual(outdoor.current(), 52.4)

    def test_drops_stale_value_rather_than_lying(self):
        outdoor, _ = self.make([52.4, ConnectionError("down")])
        outdoor.current()
        self.now[0] = 3 * 3600
        with self.assertLogs("plunge.weather", logging.WARNING):
            self.assertIsNone(outdoor.current())

    def test_first_fetch_failure_returns_none(self):
        outdoor, _ = self.make([ValueError("bad payload")])
        with self.assertLogs("plunge.weather", logging.WARNING):
            self.assertIsNone(outdoor.current())


class StubOutdoor:
    def __init__(self, value=40.0):
        self.value = value

    def current(self):
        return self.value


def make_config(tmp: Path, **polling) -> ThermoConfig:
    data = {
        "inkbird": {"device_id": "d", "ip": "10.0.0.5", "local_key": "k"},
        "location": {"latitude": 40.0, "longitude": -105.3},
        "polling": dict({"interval_seconds": 60, "unreachable_polls": 2}, **polling),
    }
    path = tmp / "thermo_config.json"
    path.write_text(json.dumps(data))
    return ThermoConfig.load(path)


class TestThermoLogger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = make_config(self.tmp)
        self.history = self.tmp / "thermo_history.jsonl"

    def lines(self):
        if not self.history.exists():
            return []
        return [json.loads(line) for line in self.history.read_text().splitlines()]

    def test_poll_appends_reading_with_outdoor_temp(self):
        client, _ = make_client([{"dps": REAL_DPS}])
        ThermoLogger(self.config, client=client, outdoor=StubOutdoor(52.4)).poll_once()
        (line,) = self.lines()
        self.assertEqual((line["pv"], line["amb"], line["out"]), (45.3, 52.4, "1"))

    def test_failed_poll_does_not_raise_or_write(self):
        client, _ = make_client([OSError("down")] * 3)
        thermo = ThermoLogger(self.config, client=client, outdoor=StubOutdoor())
        self.assertIsNone(thermo.poll_once())
        self.assertEqual(self.lines(), [])

    def test_unreachable_warns_once_at_threshold_then_recovers(self):
        down = [OSError("down")] * 3
        client, _ = make_client(down * 3 + [{"dps": REAL_DPS}])
        thermo = ThermoLogger(self.config, client=client, outdoor=StubOutdoor())
        with self.assertLogs("plunge.thermo_logger", logging.DEBUG) as logs:
            for _ in range(4):
                thermo.poll_once()
        warnings = [r for r in logs.records if r.levelno == logging.WARNING]
        self.assertEqual(len([w for w in warnings if "unreachable" in w.getMessage()]), 1)
        self.assertTrue(any("reachable again" in r.getMessage() for r in logs.records))
        self.assertEqual(thermo.failures, 0)

    def test_output_changes_are_logged(self):
        idle = dict(REAL_DPS, **{"115": "0"})
        client, _ = make_client([{"dps": idle}, {"dps": idle}, {"dps": REAL_DPS}])
        thermo = ThermoLogger(self.config, client=client, outdoor=StubOutdoor())
        with self.assertLogs("plunge.thermo_logger", logging.INFO) as logs:
            for _ in range(3):
                thermo.poll_once()
        changes = [r.getMessage() for r in logs.records if r.getMessage().startswith("Output")]
        self.assertEqual(len(changes), 2)
        self.assertIn("idle -> cooling", changes[1])

    def test_watch_survives_unexpected_errors_and_prunes_at_start(self):
        class Exploding:
            def read(self):
                raise RuntimeError("disk full, say")

        thermo = ThermoLogger(self.config, client=Exploding(), outdoor=StubOutdoor())
        pruned = []
        thermo.prune = lambda: pruned.append(True) or 0
        with self.assertLogs("plunge.thermo_logger", logging.ERROR):
            thermo.watch(sleep=lambda _s: None, max_polls=3)
        self.assertEqual(thermo.polls, 3)
        self.assertTrue(pruned)


class TestSummarize(unittest.TestCase):
    def test_runtime_by_output(self):
        entries = [
            {"t": "2026-11-07T10:00:00", "pv": 45.3, "out": "1", "amb": 52.0},
            {"t": "2026-11-07T10:01:00", "pv": 45.1, "out": "1", "amb": 52.0},
            {"t": "2026-11-07T10:02:00", "pv": 44.9, "out": "0"},
        ]
        s = summarize(entries, 60)
        self.assertEqual(s["minutes_by_output"], {"cooling": 2.0, "idle": 1.0})
        self.assertEqual(s["water_f"], {"min": 44.9, "max": 45.3})
        self.assertEqual(s["coverage_pct"], 100.0)

    def test_empty(self):
        self.assertEqual(summarize([], 60), {"samples": 0})


class TestThermoConfig(unittest.TestCase):
    def test_missing_key_is_a_clear_error(self):
        with self.assertRaises(ConfigError):
            InkbirdConfig.from_dict({"device_id": "d", "ip": "10.0.0.5"})

    def test_paths_resolve_next_to_config(self):
        tmp = Path(tempfile.mkdtemp())
        config = make_config(tmp)
        self.assertEqual(config.resolve("thermo_history.jsonl"), tmp.resolve() / "thermo_history.jsonl")


if __name__ == "__main__":
    unittest.main()
