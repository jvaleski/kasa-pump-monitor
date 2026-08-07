"""Orchestration: the poll loop, calibration, and status reporting."""

import asyncio
import logging
import math
import statistics
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from . import store
from .config import Config
from .detect import Alert, Detector, PumpState, Severity
from .device import DeviceError, PlugClient, Reading
from .notify import Notifier

logger = logging.getLogger(__name__)

# Drift needs a day of data to mean anything, and pruning is housekeeping.
# Neither belongs on the every-minute path.
DRIFT_CHECK_EVERY = 60
PRUNE_EVERY = 1440


class Monitor:
    """Owns the plug connection, the detector, and the notifiers."""

    def __init__(self, config: Config):
        self.config = config
        self.history_path = config.resolve(config.storage.history_file)
        self.state_path = config.resolve(config.storage.state_file)

        self.client = PlugClient(config.device)
        self.notifier = Notifier(config.alerts.channels)
        self.detector = Detector(
            pump=config.pump,
            polling=config.polling,
            alerts=config.alerts,
            state=store.load_state(self.state_path),
        )
        self._polls = 0

    # ---------------------------------------------------------------- polling

    async def poll_once(self) -> Optional[Reading]:
        """One read → classify → alert → persist cycle. Never raises on a read failure."""
        now = datetime.now()

        try:
            reading = await self.client.read()
        except DeviceError as exc:
            detail = str(exc)
            if exc.hint:
                detail += f"\n\n{exc.hint}"
            logger.warning("Read failed: %s", exc)
            self._dispatch(self.detector.observe(PumpState.UNREACHABLE, now, detail))
            self._save_state()
            return None

        store.append_reading(self.history_path, reading)
        self.detector.state.last_reading = reading.to_dict()

        observed = self.detector.classify(reading)
        self._dispatch(self.detector.observe(observed, now, self._describe(reading)))

        self._polls += 1
        if self._polls % DRIFT_CHECK_EVERY == 0:
            self._check_drift(now)
        if self._polls % PRUNE_EVERY == 0:
            store.prune_history(self.history_path, self.config.storage.retain_days)

        self._save_state()
        logger.info(
            "%.2fW — %s%s",
            reading.watts,
            self.detector.state.state.value,
            "" if observed is self.detector.state.state else f" (observing {observed.value})",
        )
        return reading

    def prune(self) -> int:
        """Drop history past the retention window.

        Public because `once` runs under cron and never reaches the periodic
        prune inside the poll loop; without calling this, cron-driven history
        would grow forever.
        """
        return store.prune_history(self.history_path, self.config.storage.retain_days)

    def announce_startup(self) -> None:
        """Tell the channels the monitor is up.

        Without this, a monitor that died and a monitor that is quietly healthy
        look identical from the outside — you find out the difference only when
        an alert you needed never arrives.
        """
        pump = self.config.pump
        self.notifier.send(
            Alert(
                key="startup",
                severity=Severity.INFO,
                title="Plunge pump monitor started",
                body=(
                    f"Watching {self.config.device.ip} every "
                    f"{self.config.polling.interval_seconds:.0f}s. "
                    f"Normal band {pump.running_min:.1f}–{pump.running_max:.1f}W. "
                    f"Last known state: {self.detector.state.state.value}. "
                    f"Started {datetime.now():%Y-%m-%d %H:%M:%S}."
                ),
            )
        )

    async def watch(self) -> None:
        """Poll forever at the configured interval."""
        interval = self.config.polling.interval_seconds
        logger.info(
            "Watching %s every %.0fs — normal band %.1f–%.1fW, alerting via %s",
            self.config.device.ip,
            interval,
            self.config.pump.running_min,
            self.config.pump.running_max,
            ", ".join(self.notifier.describe()),
        )
        self.prune()
        self.announce_startup()

        try:
            while True:
                started = asyncio.get_running_loop().time()
                try:
                    await self.poll_once()
                except Exception:  # noqa: BLE001 - the loop must outlive any single poll
                    logger.exception("Unexpected error during poll; continuing")
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.0, interval - elapsed))
        except asyncio.CancelledError:
            logger.info("Watch cancelled; shutting down")
            raise
        finally:
            self._save_state()
            await self.client.close()

    def _describe(self, reading: Reading) -> str:
        pump = self.config.pump
        band = f"normal {pump.running_min:.1f}–{pump.running_max:.1f}W"
        detail = f"Currently drawing {reading.watts:.1f}W ({band})."
        if reading.volts is not None:
            detail += f" Line voltage {reading.volts:.0f}V."
            if pump.normalizes_voltage:
                normalized = pump.normalize(reading.watts, reading.volts)
                detail += f" Voltage-adjusted: {normalized:.1f}W."
        return detail

    def _check_drift(self, now: datetime) -> None:
        history = store.load_history(self.history_path, since=now - timedelta(hours=25))
        self._dispatch(self.detector.check_drift(history, now))

    def _dispatch(self, alert: Optional[Alert]) -> None:
        if alert is not None:
            self.notifier.send(alert)

    def _save_state(self) -> None:
        store.save_state(self.state_path, self.detector.state)

    # ------------------------------------------------------------ calibration

    async def calibrate(self, duration_seconds: float, interval_seconds: float) -> Dict[str, Any]:
        """Sample a healthy, running pump and write its signature to the config.

        Refuses to write a calibration that doesn't look like a steadily running
        pump, so a reading taken while the pump is off can't poison the baseline.
        """
        samples: List[float] = []
        volts: List[float] = []
        failures = 0
        deadline = asyncio.get_running_loop().time() + duration_seconds
        expected = max(1, int(duration_seconds / interval_seconds))

        print(
            f"Sampling {self.config.device.ip} for {duration_seconds / 60:.0f} min "
            f"every {interval_seconds:.0f}s. The pump must be running normally.\n"
        )

        while asyncio.get_running_loop().time() < deadline:
            try:
                reading = await self.client.read()
                if not reading.is_on:
                    raise DeviceError("Plug relay is switched off; turn it on and re-run calibrate")
                samples.append(reading.watts)
                if reading.volts:
                    volts.append(reading.volts)
                suffix = f"  @ {reading.volts:.1f}V" if reading.volts else ""
                print(
                    f"  [{len(samples):3d}/{expected}] {reading.watts:8.3f}W{suffix}", flush=True
                )
            except DeviceError as exc:
                failures += 1
                print(f"  read failed: {exc}", flush=True)
                if failures >= 5 and not samples:
                    raise
            await asyncio.sleep(interval_seconds)

        await self.client.close()

        if len(samples) < 10:
            raise DeviceError(
                f"Only collected {len(samples)} samples; need at least 10. "
                "Check the plug connection and try a longer window."
            )

        median = statistics.median(samples)
        spread = statistics.pstdev(samples)
        low, high = min(samples), max(samples)

        if median <= self.config.pump.stopped_max_watts:
            raise DeviceError(
                f"Median draw was {median:.2f}W, at or below the "
                f"{self.config.pump.stopped_max_watts}W 'stopped' threshold. "
                "The pump does not appear to have been running."
            )

        # A pump that cycled mid-calibration produces a bimodal sample; the
        # resulting median is a fiction halfway between on and off.
        swing_pct = (high - low) / median * 100
        tolerance = self.config.pump.running_tolerance_pct
        if swing_pct > tolerance:
            raise DeviceError(
                f"Readings swung {swing_pct:.1f}% ({low:.2f}–{high:.2f}W), wider than the "
                f"{tolerance:.1f}% tolerance. The pump may have cycled during calibration, "
                "or the tolerance needs raising."
            )

        # Anchor voltage normalization to the conditions we calibrated under, so
        # the reference and the baseline always describe the same moment.
        reference_volts = statistics.median(volts) if volts else None

        calibrated = replace(
            self.config.pump,
            running_watts=median,
            voltage_reference=reference_volts,
            calibrated_at=datetime.now().isoformat(timespec="seconds"),
            calibration_samples=len(samples),
        )
        self.config.save_pump_calibration(calibrated)
        self.detector.pump = calibrated

        return {
            "samples": len(samples),
            "failures": failures,
            "median": median,
            "stdev": spread,
            "min": low,
            "max": high,
            "swing_pct": swing_pct,
            "reference_volts": reference_volts,
            "running_min": calibrated.running_min,
            "running_max": calibrated.running_max,
            "headroom_ratio": (swing_pct / tolerance) if tolerance else 0.0,
            "normalizing": calibrated.normalizes_voltage,
        }

    # ---------------------------------------------------------- voltage model

    def fit_voltage(self, apply: bool) -> Dict[str, Any]:
        """Fit the power-vs-voltage exponent from collected history.

        Mains voltage drifts through the day and the pump's draw follows it. That
        variation is not a fault, but it consumes alert-band headroom. Modelling
        it as P = C * V**k and normalizing it away buys back that headroom.

        k is estimated by least squares on log(P) against log(V), which is just a
        linear fit in log space.
        """
        history = store.load_history(self.history_path)
        points = [
            (entry["v"], entry["w"])
            for entry in history
            if entry.get("on")
            and entry.get("v")
            and entry.get("w")
            and entry["v"] > 0
            and entry["w"] > self.config.pump.stopped_max_watts
        ]

        if len(points) < 100:
            raise DeviceError(
                f"Only {len(points)} usable readings in {self.history_path.name}; need at least "
                "100. Let the monitor run for a day or so, then try again."
            )

        v_values = [v for v, _ in points]
        spread_pct = (max(v_values) - min(v_values)) / statistics.median(v_values) * 100
        if spread_pct < 2.0:
            raise DeviceError(
                f"Line voltage only varied {spread_pct:.2f}% ({min(v_values):.1f}–"
                f"{max(v_values):.1f}V) across this history. That is too flat to fit a "
                "reliable exponent — collect readings spanning more of the daily cycle."
            )

        log_v = [math.log(v) for v, _ in points]
        log_w = [math.log(w) for _, w in points]
        mean_v, mean_w = statistics.fmean(log_v), statistics.fmean(log_w)
        covariance = sum((a - mean_v) * (b - mean_w) for a, b in zip(log_v, log_w))
        variance = sum((a - mean_v) ** 2 for a in log_v)
        exponent = covariance / variance

        reference = self.config.pump.voltage_reference or statistics.median(v_values)
        candidate = replace(
            self.config.pump, voltage_reference=reference, voltage_exponent=exponent
        )

        raw = [w for _, w in points]
        adjusted = [candidate.normalize(w, v) for v, w in points]
        raw_cv = statistics.pstdev(raw) / statistics.fmean(raw) * 100
        adjusted_cv = statistics.pstdev(adjusted) / statistics.fmean(adjusted) * 100

        if apply:
            self.config.save_pump_calibration(candidate)
            self.detector.pump = candidate

        return {
            "samples": len(points),
            "exponent": exponent,
            "reference_volts": reference,
            "voltage_range": (min(v_values), max(v_values)),
            "voltage_spread_pct": spread_pct,
            "scatter_before_pct": raw_cv,
            "scatter_after_pct": adjusted_cv,
            "improvement_pct": (raw_cv - adjusted_cv) / raw_cv * 100 if raw_cv else 0.0,
            "applied": apply,
        }

    # ----------------------------------------------------------------- status

    async def status(self) -> Dict[str, Any]:
        """Snapshot of the device, the detector, and recent history."""
        report: Dict[str, Any] = {"config_path": str(self.config.path)}

        try:
            report["device"] = await self.client.describe()
            reading = await self.client.read()
            report["reading"] = reading.to_dict()
            report["classified"] = self.detector.classify(reading).value
        except DeviceError as exc:
            report["device_error"] = str(exc)
            if exc.hint:
                report["hint"] = exc.hint
        finally:
            await self.client.close()

        state = self.detector.state
        report["state"] = state.state.value
        report["since"] = state.since.isoformat(timespec="seconds") if state.since else None
        if state.pending_count:
            report["pending"] = {
                "state": state.pending_state.value if state.pending_state else None,
                "count": state.pending_count,
            }

        pump = self.config.pump
        if pump.is_calibrated:
            report["band"] = {
                "running_watts": pump.running_watts,
                "min": round(pump.running_min, 2),
                "max": round(pump.running_max, 2),
                "stopped_below": pump.stopped_max_watts,
                "calibrated_at": pump.calibrated_at,
                "voltage_normalization": (
                    {
                        "reference_volts": pump.voltage_reference,
                        "exponent": pump.voltage_exponent,
                    }
                    if pump.normalizes_voltage
                    else "off (run fit-voltage after a day of history)"
                ),
            }
            if "reading" in report:
                report["reading"]["normalized_w"] = round(
                    pump.normalize(report["reading"]["w"], report["reading"].get("v")), 3
                )
        else:
            report["band"] = None

        history = store.load_history(self.history_path, since=datetime.now() - timedelta(hours=24))
        watts = [entry["w"] for entry in history if entry.get("w") is not None]
        if watts:
            report["last_24h"] = {
                "samples": len(watts),
                "median": round(statistics.median(watts), 2),
                "min": round(min(watts), 2),
                "max": round(max(watts), 2),
            }
        report["channels"] = self.notifier.describe()
        return report

    async def test_alert(self) -> None:
        """Push a synthetic alert through every configured channel."""
        self.notifier.send(
            Alert(
                key="test",
                severity=Severity.WARNING,
                title="Plunge pump monitor: test alert",
                body=(
                    "This is a test. If you are reading this, the channel works. "
                    f"Sent {datetime.now():%Y-%m-%d %H:%M:%S}."
                ),
            )
        )
