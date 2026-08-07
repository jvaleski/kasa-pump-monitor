"""Configuration loading, validation, and secret resolution."""

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConfigError(Exception):
    """Raised when the config file is missing required values."""


def _resolve_secret(raw: Optional[str], env_var: Optional[str], label: str) -> Optional[str]:
    """Prefer an environment variable over an inline value.

    Keeps credentials out of the config file when the caller wants that, while
    still tolerating the inline form for a quick local setup.
    """
    if env_var:
        value = os.environ.get(env_var)
        if value:
            return value
        if not raw:
            raise ConfigError(
                f"{label}: environment variable {env_var} is not set and no inline value was given"
            )
    return raw


@dataclass
class DeviceConfig:
    """How to reach the smart plug."""

    ip: str
    username: str
    password: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceConfig":
        ip = data.get("ip")
        username = data.get("username")
        if not ip:
            raise ConfigError("device.ip is required")
        if not username:
            raise ConfigError("device.username is required")

        password = _resolve_secret(
            data.get("password"), data.get("password_env"), "device.password"
        )
        if not password:
            raise ConfigError("device.password (or device.password_env) is required")

        return cls(
            ip=ip,
            username=username,
            password=password,
            timeout_seconds=float(data.get("timeout_seconds", 10.0)),
        )


@dataclass
class PumpConfig:
    """The pump's electrical signature, learned by `calibrate`.

    The pump runs continuously, so a healthy reading sits in a tight band around
    `running_watts`. Everything outside that band is a distinct failure mode.

    Line voltage is a confounder: mains here was measured at 113V one evening and
    122V the next morning, and the pump's draw moved 3% with it. That is normal
    grid behaviour, not a pump fault, so it must not eat the alert band. If
    `voltage_exponent` is non-zero, readings are normalized to
    `voltage_reference` as P * (V_ref / V) ** exponent before classification.
    Fit the exponent from real data with `pump_monitor.py fit-voltage`.
    """

    running_watts: Optional[float] = None
    running_tolerance_pct: float = 10.0
    stopped_max_watts: float = 5.0
    voltage_reference: Optional[float] = None
    voltage_exponent: float = 0.0
    calibrated_at: Optional[str] = None
    calibration_samples: Optional[int] = None

    @property
    def is_calibrated(self) -> bool:
        return self.running_watts is not None

    @property
    def normalizes_voltage(self) -> bool:
        return bool(self.voltage_exponent) and bool(self.voltage_reference)

    def normalize(self, watts: float, volts: Optional[float]) -> float:
        """Adjust a reading to what it would be at the reference voltage.

        Returns `watts` unchanged when normalization is off or the plug gave us
        no voltage, so this is always safe to call.
        """
        if not self.normalizes_voltage or not volts or volts <= 0:
            return watts
        return watts * (self.voltage_reference / volts) ** self.voltage_exponent

    @property
    def running_min(self) -> float:
        """Below this (but above standby) the pump is cavitating or lost prime."""
        if self.running_watts is None:
            raise ConfigError("pump is not calibrated; run `pump_monitor.py calibrate`")
        return self.running_watts * (1 - self.running_tolerance_pct / 100)

    @property
    def running_max(self) -> float:
        """Above this the pump is straining against a restriction."""
        if self.running_watts is None:
            raise ConfigError("pump is not calibrated; run `pump_monitor.py calibrate`")
        return self.running_watts * (1 + self.running_tolerance_pct / 100)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PumpConfig":
        running = data.get("running_watts")
        reference = data.get("voltage_reference")
        cfg = cls(
            running_watts=float(running) if running is not None else None,
            running_tolerance_pct=float(data.get("running_tolerance_pct", 10.0)),
            stopped_max_watts=float(data.get("stopped_max_watts", 5.0)),
            voltage_reference=float(reference) if reference is not None else None,
            voltage_exponent=float(data.get("voltage_exponent", 0.0)),
            calibrated_at=data.get("calibrated_at"),
            calibration_samples=data.get("calibration_samples"),
        )
        if cfg.running_tolerance_pct <= 0:
            raise ConfigError("pump.running_tolerance_pct must be positive")
        if cfg.voltage_reference is not None and cfg.voltage_reference <= 0:
            raise ConfigError("pump.voltage_reference must be positive")
        if cfg.voltage_exponent and cfg.voltage_reference is None:
            raise ConfigError(
                "pump.voltage_exponent is set but pump.voltage_reference is missing; "
                "run `pump_monitor.py calibrate` to record one"
            )
        if cfg.is_calibrated and cfg.running_min <= cfg.stopped_max_watts:
            raise ConfigError(
                f"pump.stopped_max_watts ({cfg.stopped_max_watts}W) overlaps the running band "
                f"(starts at {cfg.running_min:.1f}W); lower it or tighten the tolerance"
            )
        return cfg


@dataclass
class PollingConfig:
    """Cadence and how much evidence an alert requires."""

    interval_seconds: float = 60.0
    # Consecutive abnormal readings before alerting. Debounces a single bad poll.
    confirm_samples: int = 3
    # Consecutive read failures before treating the plug as unreachable.
    unreachable_samples: int = 3
    # Consecutive healthy readings before declaring recovery.
    recovery_samples: int = 2

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PollingConfig":
        cfg = cls(
            interval_seconds=float(data.get("interval_seconds", 60.0)),
            confirm_samples=int(data.get("confirm_samples", 3)),
            unreachable_samples=int(data.get("unreachable_samples", 3)),
            recovery_samples=int(data.get("recovery_samples", 2)),
        )
        if cfg.interval_seconds < 5:
            raise ConfigError("polling.interval_seconds must be at least 5")
        for name in ("confirm_samples", "unreachable_samples", "recovery_samples"):
            if getattr(cfg, name) < 1:
                raise ConfigError(f"polling.{name} must be at least 1")
        return cfg


@dataclass
class AlertConfig:
    """Alert routing and rate limiting."""

    cooldown_seconds: float = 1800.0
    send_recovery: bool = True
    # Slow drift of the 24h median away from the calibrated draw. Catches a
    # gradually clogging filter that never leaves the instantaneous band.
    drift_alert_pct: float = 4.0
    drift_cooldown_seconds: float = 86400.0
    channels: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlertConfig":
        return cls(
            cooldown_seconds=float(data.get("cooldown_seconds", 1800.0)),
            send_recovery=bool(data.get("send_recovery", True)),
            drift_alert_pct=float(data.get("drift_alert_pct", 4.0)),
            drift_cooldown_seconds=float(data.get("drift_cooldown_seconds", 86400.0)),
            channels=list(data.get("channels", [])),
        )


@dataclass
class StorageConfig:
    """Where readings, state, and logs live. Paths are relative to the config file."""

    history_file: str = "pump_history.jsonl"
    state_file: str = "pump_state.json"
    log_file: str = "pump_monitor.log"
    retain_days: float = 30.0
    # The log gains a line per poll and is never truncated on its own. On a box
    # meant to run unattended for years that has to be capped, so the handler
    # rotates. 5MB x 3 backups is roughly four months at a 60s interval.
    log_max_mb: float = 5.0
    log_backups: int = 3

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StorageConfig":
        cfg = cls(
            history_file=data.get("history_file", "pump_history.jsonl"),
            state_file=data.get("state_file", "pump_state.json"),
            log_file=data.get("log_file", "pump_monitor.log"),
            retain_days=float(data.get("retain_days", 30.0)),
            log_max_mb=float(data.get("log_max_mb", 5.0)),
            log_backups=int(data.get("log_backups", 3)),
        )
        if cfg.log_max_mb <= 0:
            raise ConfigError("storage.log_max_mb must be positive")
        if cfg.log_backups < 0:
            raise ConfigError("storage.log_backups cannot be negative")
        return cfg


@dataclass
class Config:
    """Top-level config, with paths resolved against the config file's directory."""

    device: DeviceConfig
    pump: PumpConfig
    polling: PollingConfig
    alerts: AlertConfig
    storage: StorageConfig
    path: Path

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def resolve(self, filename: str) -> Path:
        """Resolve a storage filename against the config directory."""
        candidate = Path(filename)
        return candidate if candidate.is_absolute() else self.base_dir / candidate

    @classmethod
    def load(cls, path: str) -> "Config":
        config_path = Path(path).expanduser().resolve()
        if not config_path.exists():
            raise ConfigError(
                f"config file not found: {config_path}\n"
                "Copy pump_config.example.json to pump_config.json and fill it in."
            )

        with open(config_path) as handle:
            try:
                raw = json.load(handle)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{config_path} is not valid JSON: {exc}") from exc

        return cls(
            device=DeviceConfig.from_dict(raw.get("device", {})),
            pump=PumpConfig.from_dict(raw.get("pump", {})),
            polling=PollingConfig.from_dict(raw.get("polling", {})),
            alerts=AlertConfig.from_dict(raw.get("alerts", {})),
            storage=StorageConfig.from_dict(raw.get("storage", {})),
            path=config_path,
        )

    def save_pump_calibration(self, pump: PumpConfig) -> None:
        """Write calibration back to the config file, preserving everything else.

        Re-reads from disk so a long-running process doesn't clobber unrelated
        edits made while it was running.
        """
        with open(self.path) as handle:
            raw = json.load(handle)

        section = raw.setdefault("pump", {})
        section.update(
            {
                "running_watts": round(pump.running_watts, 3) if pump.running_watts else None,
                "running_tolerance_pct": pump.running_tolerance_pct,
                "stopped_max_watts": pump.stopped_max_watts,
                "voltage_reference": (
                    round(pump.voltage_reference, 2) if pump.voltage_reference else None
                ),
                "voltage_exponent": round(pump.voltage_exponent, 4),
                "calibrated_at": pump.calibrated_at,
                "calibration_samples": pump.calibration_samples,
            }
        )

        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w") as handle:
            json.dump(raw, handle, indent=2)
            handle.write("\n")
        tmp.replace(self.path)
        self.pump = pump

    def redacted(self) -> Dict[str, Any]:
        """Config as a dict with secrets masked, for logging and `status`."""
        data = {
            "device": asdict(self.device),
            "pump": asdict(self.pump),
            "polling": asdict(self.polling),
            "alerts": asdict(self.alerts),
            "storage": asdict(self.storage),
        }
        data["device"]["password"] = "***"
        for channel in data["alerts"]["channels"]:
            for key in list(channel):
                if any(s in key for s in ("key", "token", "password", "secret")):
                    channel[key] = "***"
        return data
