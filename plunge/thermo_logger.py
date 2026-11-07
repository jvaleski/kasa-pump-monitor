"""Phase 1 of the plunge scheduler: log the thermostat, change nothing.

Every poll records the water temperature, the controller's settings, whether
the chiller or heater is running, and the outdoor temperature. Two weeks of
that is enough to fit how fast the tub drifts toward ambient and how fast each
unit recovers. It also gives a baseline: how much the equipment runs while the
tub is held at 45°F around the clock, before any scheduling.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from . import store
from .inkbird import OUTPUT_STATES, InkbirdClient, InkbirdError, ThermoReading
from .thermo_config import ThermoConfig
from .weather import OutdoorTemperature

logger = logging.getLogger(__name__)

PRUNE_EVERY_POLLS = 1440


class ThermoLogger:
    def __init__(
        self,
        config: ThermoConfig,
        client: Optional[InkbirdClient] = None,
        outdoor: Optional[OutdoorTemperature] = None,
    ):
        self.config = config
        self.client = client or InkbirdClient(config.inkbird)
        self.outdoor = outdoor or OutdoorTemperature(
            config.location.latitude,
            config.location.longitude,
            refresh_seconds=config.weather_refresh_seconds,
        )
        self.history_path = config.resolve(config.storage.history_file)
        self.failures = 0
        self.last_output: Optional[str] = None
        self.polls = 0

    def poll_once(self) -> Optional[ThermoReading]:
        """One read and one history line. Never raises for a device problem.

        The loop has to outlive any single poll: a logger that dies on the
        first network blip produces exactly the gap you needed data for.
        """
        try:
            reading = self.client.read()
        except InkbirdError as exc:
            self.failures += 1
            # One warning at the threshold, not one a minute for a long outage.
            if self.failures == self.config.unreachable_polls:
                logger.warning("Inkbird unreachable for %d polls: %s", self.failures, exc)
                if exc.hint:
                    logger.warning("Hint: %s", exc.hint)
            else:
                logger.debug("Inkbird read failed (%d in a row): %s", self.failures, exc)
            return None

        if self.failures >= self.config.unreachable_polls:
            logger.info("Inkbird reachable again after %d failed polls", self.failures)
        self.failures = 0

        reading.outdoor_f = self.outdoor.current()
        store.append_reading(self.history_path, reading)

        if reading.output != self.last_output:
            # Start and stop times of each unit are the most useful events in
            # the log when reading it by eye.
            logger.info(
                "Output %s -> %s at water %.1f°F (target %.1f, outdoor %s)",
                OUTPUT_STATES.get(self.last_output, "start") if self.last_output else "start",
                reading.output_name,
                reading.water_f,
                reading.target_f,
                f"{reading.outdoor_f:.1f}°F" if reading.outdoor_f is not None else "unknown",
            )
            self.last_output = reading.output
        return reading

    def prune(self) -> int:
        return store.prune_history(self.history_path, self.config.storage.retain_days)

    def watch(self, sleep: Callable[[float], None] = time.sleep, max_polls: Optional[int] = None) -> None:
        logger.info(
            "Thermostat logger started: Inkbird %s, polling every %.0fs, read-only",
            self.config.inkbird.ip,
            self.config.poll_interval_seconds,
        )
        self.prune()
        while max_polls is None or self.polls < max_polls:
            started = time.monotonic()
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - e.g. a full disk; log it and keep going
                logger.exception("Unexpected error during poll")
            self.polls += 1
            if self.polls % PRUNE_EVERY_POLLS == 0:
                self.prune()
            elapsed = time.monotonic() - started
            sleep(max(0.0, self.config.poll_interval_seconds - elapsed))


def summarize(entries: List[Dict[str, Any]], interval_seconds: float) -> Dict[str, Any]:
    """Runtime and range over a slice of history, for `status`.

    Runtime is estimated as samples-in-state × poll interval, so it's only as
    good as the poll coverage; `coverage_pct` says how good that is.
    """
    if not entries:
        return {"samples": 0}
    water = [e["pv"] for e in entries if "pv" in e]
    outdoor = [e["amb"] for e in entries if e.get("amb") is not None]
    minutes = interval_seconds / 60.0
    by_state: Dict[str, float] = {}
    for entry in entries:
        name = OUTPUT_STATES.get(str(entry.get("out")), f"unknown-{entry.get('out')}")
        by_state[name] = by_state.get(name, 0.0) + minutes

    first = datetime.fromisoformat(entries[0]["t"])
    last = datetime.fromisoformat(entries[-1]["t"])
    span_minutes = (last - first).total_seconds() / 60.0 + minutes
    return {
        "samples": len(entries),
        "from": entries[0]["t"],
        "to": entries[-1]["t"],
        "coverage_pct": round(100.0 * len(entries) * minutes / span_minutes, 1) if span_minutes else None,
        "water_f": {"min": min(water), "max": max(water)} if water else None,
        "outdoor_f": {"min": min(outdoor), "max": max(outdoor)} if outdoor else None,
        "minutes_by_output": {k: round(v, 1) for k, v in sorted(by_state.items())},
    }


def recent_summary(config: ThermoConfig, hours: float = 24.0) -> Dict[str, Any]:
    since = datetime.now() - timedelta(hours=hours)
    entries = store.load_history(config.resolve(config.storage.history_file), since=since)
    return summarize(entries, config.poll_interval_seconds)
