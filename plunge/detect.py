"""Pump state classification, debouncing, and drift detection.

The plunge pump runs continuously, so its power draw is the health signal:

    <= stopped_max_watts .......... STOPPED     (dead, tripped, or unplugged)
    stopped_max .. running_min .... CAVITATING  (spinning against air, lost prime)
    running_min .. running_max .... RUNNING     (healthy)
    > running_max ................. STRAINING   (clogged filter, failing bearings)

A single reading never raises an alert. A state change must be confirmed by
`confirm_samples` consecutive readings, which keeps one noisy poll or a brief
network hiccup from paging you at 3am.
"""

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional

from .config import AlertConfig, PollingConfig, PumpConfig
from .device import Reading


class PumpState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    CAVITATING = "cavitating"
    STRAINING = "straining"
    PLUG_OFF = "plug_off"
    UNREACHABLE = "unreachable"

    @property
    def is_healthy(self) -> bool:
        return self is PumpState.RUNNING


class Severity(Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


# How each failure reads in an alert, and how loud it should be.
STATE_DESCRIPTIONS: Dict[PumpState, str] = {
    PumpState.RUNNING: "Pump is running normally",
    PumpState.STOPPED: "Pump has STOPPED — no meaningful power draw. Water is not circulating.",
    PumpState.CAVITATING: (
        "Pump is drawing less power than normal — likely cavitating or has lost prime. "
        "Check water level and intake."
    ),
    PumpState.STRAINING: (
        "Pump is drawing more power than normal — likely a restriction. "
        "Check the filter and intake for clogs."
    ),
    PumpState.PLUG_OFF: "The smart plug's relay is switched OFF, so the pump has no power.",
    PumpState.UNREACHABLE: "Cannot reach the smart plug. Network issue, or the power is out.",
}

STATE_SEVERITY: Dict[PumpState, Severity] = {
    PumpState.RUNNING: Severity.INFO,
    PumpState.STOPPED: Severity.CRITICAL,
    PumpState.CAVITATING: Severity.CRITICAL,
    PumpState.STRAINING: Severity.WARNING,
    PumpState.PLUG_OFF: Severity.CRITICAL,
    PumpState.UNREACHABLE: Severity.WARNING,
}


@dataclass
class Alert:
    """Something worth telling a human about."""

    key: str  # dedupe/cooldown key
    severity: Severity
    title: str
    body: str
    state: Optional[PumpState] = None

    def __str__(self) -> str:
        return f"[{self.severity.value.upper()}] {self.title}: {self.body}"


@dataclass
class MonitorState:
    """Detector state that must survive process restarts.

    The old monitor tracked consecutive failures on an instance attribute while
    running once per cron invocation, so the counter reset to zero every minute
    and the failure threshold could never be reached. Persisting it fixes that.
    """

    state: PumpState = PumpState.RUNNING
    since: Optional[datetime] = None
    pending_state: Optional[PumpState] = None
    pending_count: int = 0
    last_alert_at: Dict[str, str] = field(default_factory=dict)
    last_reading: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "since": self.since.isoformat() if self.since else None,
            "pending_state": self.pending_state.value if self.pending_state else None,
            "pending_count": self.pending_count,
            "last_alert_at": self.last_alert_at,
            "last_reading": self.last_reading,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MonitorState":
        def parse_state(value: Optional[str]) -> Optional[PumpState]:
            try:
                return PumpState(value) if value else None
            except ValueError:
                return None

        since = data.get("since")
        return cls(
            state=parse_state(data.get("state")) or PumpState.RUNNING,
            since=datetime.fromisoformat(since) if since else None,
            pending_state=parse_state(data.get("pending_state")),
            pending_count=int(data.get("pending_count", 0)),
            last_alert_at=dict(data.get("last_alert_at", {})),
            last_reading=data.get("last_reading"),
        )


class Detector:
    """Turns a stream of readings into a debounced state, and states into alerts."""

    def __init__(
        self,
        pump: PumpConfig,
        polling: PollingConfig,
        alerts: AlertConfig,
        state: Optional[MonitorState] = None,
    ):
        self.pump = pump
        self.polling = polling
        self.alerts = alerts
        self.state = state or MonitorState()

    def classify(self, reading: Reading) -> PumpState:
        """Map one reading to a state. No debouncing here.

        The band comparison uses the voltage-normalized draw so a swing in mains
        voltage doesn't read as a pump fault. The stopped check deliberately uses
        the raw value: at zero draw there's nothing to normalize.
        """
        if not reading.is_on:
            return PumpState.PLUG_OFF
        if reading.watts <= self.pump.stopped_max_watts:
            return PumpState.STOPPED

        watts = self.pump.normalize(reading.watts, reading.volts)
        if watts < self.pump.running_min:
            return PumpState.CAVITATING
        if watts > self.pump.running_max:
            return PumpState.STRAINING
        return PumpState.RUNNING

    def _samples_required(self, target: PumpState) -> int:
        if target is PumpState.UNREACHABLE:
            return self.polling.unreachable_samples
        if target.is_healthy:
            return self.polling.recovery_samples
        return self.polling.confirm_samples

    def _cooldown_expired(self, key: str, now: datetime, cooldown: float) -> bool:
        stamp = self.state.last_alert_at.get(key)
        if not stamp:
            return True
        try:
            last = datetime.fromisoformat(stamp)
        except ValueError:
            return True
        return now - last > timedelta(seconds=cooldown)

    def _mark_alerted(self, key: str, now: datetime) -> None:
        self.state.last_alert_at[key] = now.isoformat()

    def observe(self, observed: PumpState, now: datetime, detail: str) -> Optional[Alert]:
        """Feed in one classified state; return an Alert if it warrants one."""
        # First observation of a fresh state file: anchor the clock so durations
        # in alerts and `status` are meaningful from the start.
        if self.state.since is None:
            self.state.since = now

        if observed is self.state.state:
            # Already in this state. Clear any half-built transition, then decide
            # whether an ongoing problem deserves a reminder.
            self.state.pending_state = None
            self.state.pending_count = 0
            return self._maybe_remind(now, detail)

        # Building toward a new state.
        if observed is self.state.pending_state:
            self.state.pending_count += 1
        else:
            self.state.pending_state = observed
            self.state.pending_count = 1

        if self.state.pending_count < self._samples_required(observed):
            return None

        return self._transition(observed, now, detail)

    def _transition(self, new_state: PumpState, now: datetime, detail: str) -> Optional[Alert]:
        previous = self.state.state
        held_for = now - self.state.since if self.state.since else None

        self.state.state = new_state
        self.state.since = now
        self.state.pending_state = None
        self.state.pending_count = 0

        if new_state.is_healthy:
            if not previous.is_healthy and self.alerts.send_recovery:
                duration = _format_duration(held_for)
                self._mark_alerted(f"recovery:{previous.value}", now)
                return Alert(
                    key=f"recovery:{previous.value}",
                    severity=Severity.INFO,
                    title="Plunge pump recovered",
                    body=(
                        f"Back to normal after {duration} in "
                        f"'{previous.value}'. {detail}"
                    ),
                    state=new_state,
                )
            return None

        self._mark_alerted(f"state:{new_state.value}", now)
        return Alert(
            key=f"state:{new_state.value}",
            severity=STATE_SEVERITY[new_state],
            title=f"Plunge pump: {new_state.value.replace('_', ' ')}",
            body=f"{STATE_DESCRIPTIONS[new_state]} {detail}",
            state=new_state,
        )

    def _maybe_remind(self, now: datetime, detail: str) -> Optional[Alert]:
        """Re-alert on an unresolved problem once the cooldown lapses."""
        state = self.state.state
        if state.is_healthy:
            return None

        key = f"state:{state.value}"
        if not self._cooldown_expired(key, now, self.alerts.cooldown_seconds):
            return None

        self._mark_alerted(key, now)
        duration = _format_duration(now - self.state.since if self.state.since else None)
        return Alert(
            key=key,
            severity=STATE_SEVERITY[state],
            title=f"Plunge pump STILL {state.value.replace('_', ' ')} ({duration})",
            body=f"{STATE_DESCRIPTIONS[state]} {detail}",
            state=state,
        )

    def check_drift(self, history: List[Dict[str, Any]], now: datetime) -> Optional[Alert]:
        """Compare the last 24h of running readings against the calibrated draw.

        A slowly clogging filter pushes the pump a little harder every day and can
        sit inside the instantaneous band for weeks. Comparing medians catches it.
        """
        if not self.pump.is_calibrated or self.alerts.drift_alert_pct <= 0:
            return None
        if not self._cooldown_expired("drift", now, self.alerts.drift_cooldown_seconds):
            return None

        cutoff = now - timedelta(hours=24)
        recent = [
            self.pump.normalize(entry["w"], entry.get("v"))
            for entry in history
            if entry.get("w") is not None
            and entry.get("on")
            and entry["w"] > self.pump.stopped_max_watts
            and _parse_time(entry.get("t")) is not None
            and _parse_time(entry["t"]) > cutoff
        ]
        # Enough samples that a handful of outliers can't move the median.
        if len(recent) < 30:
            return None

        median = statistics.median(recent)
        delta_pct = (median - self.pump.running_watts) / self.pump.running_watts * 100
        if abs(delta_pct) < self.alerts.drift_alert_pct:
            return None

        direction = "higher" if delta_pct > 0 else "lower"
        cause = (
            "Consistent with a gradually clogging filter or increasing mechanical load."
            if delta_pct > 0
            else "Consistent with a dropping water level or a worn impeller."
        )
        self._mark_alerted("drift", now)
        return Alert(
            key="drift",
            severity=Severity.WARNING,
            title=f"Plunge pump draw has drifted {abs(delta_pct):.1f}% {direction}",
            body=(
                f"24h median is {median:.1f}W vs a calibrated {self.pump.running_watts:.1f}W "
                f"({len(recent)} samples). {cause}"
            ),
        )


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_duration(delta: Optional[timedelta]) -> str:
    if delta is None:
        return "an unknown period"
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
