"""Configuration for the plunge thermostat logger (Inkbird ITC-308-WIFI).

This is a separate file from `pump_config.json` because it is a separate
service. The pump monitor assumes an always-on load; the thermostat side is
about temperature over time, and keeping the two configs apart means neither
service can be broken by an edit meant for the other.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ConfigError, _resolve_secret


@dataclass
class InkbirdConfig:
    """How to reach the controller over the LAN (Tuya local protocol)."""

    device_id: str
    ip: str
    local_key: str
    # 3.3 was confirmed against the real unit on 2026-11-07; 3.4/3.5 failed.
    version: float = 3.3
    timeout_seconds: float = 6.0
    # The unit refused connections for about a minute on 2026-11-07 and then
    # recovered on its own, most likely because something else held its single
    # local session. Retry inside a poll rather than declaring it gone.
    retries: int = 3
    retry_delay_seconds: float = 4.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "InkbirdConfig":
        device_id = data.get("device_id")
        ip = data.get("ip")
        if not device_id:
            raise ConfigError("inkbird.device_id is required (from `tinytuya wizard`)")
        if not ip:
            raise ConfigError("inkbird.ip is required; give the controller a DHCP reservation")
        local_key = _resolve_secret(
            data.get("local_key"), data.get("local_key_env"), "inkbird.local_key"
        )
        if not local_key:
            raise ConfigError("inkbird.local_key (or inkbird.local_key_env) is required")
        return cls(
            device_id=device_id,
            ip=ip,
            local_key=local_key,
            version=float(data.get("version", 3.3)),
            timeout_seconds=float(data.get("timeout_seconds", 6.0)),
            retries=int(data.get("retries", 3)),
            retry_delay_seconds=float(data.get("retry_delay_seconds", 4.0)),
        )


@dataclass
class LocationConfig:
    """Where the tub is, for the outdoor temperature."""

    latitude: float
    longitude: float
    timezone: str = "auto"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "LocationConfig":
        try:
            return cls(
                latitude=float(data["latitude"]),
                longitude=float(data["longitude"]),
                timezone=str(data.get("timezone", "auto")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("location.latitude and location.longitude are required numbers") from exc


@dataclass
class ThermoStorageConfig:
    history_file: str = "thermo_history.jsonl"
    log_file: str = "thermo.log"
    # Longer than the pump's 30 days on purpose: fitting the tub's thermal model
    # wants a spread of weather, and seasons only come round once a year.
    # One line a minute is ~150 bytes, so 120 days is roughly 26MB.
    retain_days: float = 120.0
    log_max_mb: float = 5.0
    log_backups: int = 3

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ThermoStorageConfig":
        defaults = cls()
        return cls(
            history_file=str(data.get("history_file", defaults.history_file)),
            log_file=str(data.get("log_file", defaults.log_file)),
            retain_days=float(data.get("retain_days", defaults.retain_days)),
            log_max_mb=float(data.get("log_max_mb", defaults.log_max_mb)),
            log_backups=int(data.get("log_backups", defaults.log_backups)),
        )


@dataclass
class ThermoConfig:
    inkbird: InkbirdConfig
    location: LocationConfig
    poll_interval_seconds: float = 60.0
    weather_refresh_seconds: float = 900.0
    # Consecutive failed polls before the log says the controller is
    # unreachable. A one-minute blip is normal for this unit.
    unreachable_polls: int = 5
    storage: ThermoStorageConfig = field(default_factory=ThermoStorageConfig)
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve(self, name: str) -> Path:
        path = Path(name)
        return path if path.is_absolute() else self.base_dir / path

    @classmethod
    def from_dict(cls, data: Dict[str, Any], base_dir: Optional[Path] = None) -> "ThermoConfig":
        if "inkbird" not in data:
            raise ConfigError("config is missing the 'inkbird' section")
        if "location" not in data:
            raise ConfigError("config is missing the 'location' section")
        polling = data.get("polling", {})
        return cls(
            inkbird=InkbirdConfig.from_dict(data["inkbird"]),
            location=LocationConfig.from_dict(data["location"]),
            poll_interval_seconds=float(polling.get("interval_seconds", 60.0)),
            weather_refresh_seconds=float(polling.get("weather_refresh_seconds", 900.0)),
            unreachable_polls=int(polling.get("unreachable_polls", 5)),
            storage=ThermoStorageConfig.from_dict(data.get("storage", {})),
            base_dir=base_dir or Path.cwd(),
        )

    @classmethod
    def load(cls, path: Path) -> "ThermoConfig":
        path = Path(path)
        if not path.exists():
            raise ConfigError(
                f"{path} not found. Copy thermo_config.example.json to {path.name} and fill it in."
            )
        try:
            with open(path) as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data, base_dir=path.resolve().parent)
