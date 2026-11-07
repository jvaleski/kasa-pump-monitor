"""Read-only access to the Inkbird ITC-308-WIFI over the Tuya local protocol.

There is deliberately no write method here. On 2026-11-07 a local write of two
settings that have nothing to do with temperature (PT and AH) made the
controller ignore its HD/CD dead zone and run the chiller straight to the
target for 13 minutes. Any write changes what the equipment does, so writing
belongs to the scheduler phase, behind its own guards, and not to the logger.

The data-point map comes from the device's Tuya model
(`/v2.0/cloud/thing/<id>/model`) and was checked against the front panel.
Temperatures are tenths of a degree in the controller's unit (CF), except
dp 116, which is always tenths of a °F.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from .thermo_config import InkbirdConfig

logger = logging.getLogger(__name__)

DP_FAULT = "12"          # bitmap: AH, AL, Er
DP_UNIT = "101"          # "C" / "F"
DP_CALIBRATION = "102"   # CA, tenths
DP_WATER_C = "104"       # current temperature, tenths of °C (read-only)
DP_TARGET = "106"        # TS, tenths
DP_COMPRESSOR_DELAY = "108"  # PT, whole minutes
DP_ALARM_HIGH = "109"    # AH, tenths
DP_ALARM_LOW = "110"     # AL, tenths
DP_OUTPUT = "115"        # output state enum
DP_WATER_F = "116"       # current temperature, tenths of °F
DP_HEAT_DIFF = "117"     # HD, tenths
DP_COOL_DIFF = "118"     # CD, tenths

REQUIRED_DPS = (DP_UNIT, DP_TARGET, DP_OUTPUT, DP_WATER_F, DP_HEAT_DIFF, DP_COOL_DIFF)

# "0" and "1" were confirmed on the real unit on 2026-11-07: dp 115 went from
# "0" to "1" exactly while the chiller ran, and back to "0" when it stopped.
# The front-panel COOLING light stayed off the whole time, so this field, not
# the panel, is the source of truth. "2" is presumed to mean heating, by
# symmetry, and is unconfirmed until the heater is seen running.
OUTPUT_STATES = {"0": "idle", "1": "cooling", "2": "heating?", "3": "unknown-3"}

INKBIRD_HINT = (
    "Check the controller is powered and on Wi-Fi (2.4GHz) at its reserved IP. "
    "If the Smart Life app is open on a phone, close it: the unit appears to "
    "allow only one local session at a time. If it was re-paired, the local key "
    "has changed; re-run `tinytuya wizard`."
)


class InkbirdError(Exception):
    """A read failed. `hint` carries a human-actionable next step."""

    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.hint = hint


def _f_to_c(value: float) -> float:
    return (value - 32) * 5 / 9


@dataclass
class ThermoReading:
    """One snapshot of the controller. All temperatures in °F."""

    timestamp: datetime
    water_f: float
    target_f: float
    heat_diff_f: float
    cool_diff_f: float
    output: str
    compressor_delay_min: Optional[int] = None
    alarm_high_f: Optional[float] = None
    alarm_low_f: Optional[float] = None
    calibration_f: Optional[float] = None
    fault: Optional[int] = None
    outdoor_f: Optional[float] = None

    @property
    def output_name(self) -> str:
        return OUTPUT_STATES.get(self.output, f"unknown-{self.output}")

    def to_dict(self) -> dict:
        # Short keys: this is written once a minute for months.
        record = {
            "t": self.timestamp.isoformat(timespec="seconds"),
            "pv": self.water_f,
            "ts": self.target_f,
            "hd": self.heat_diff_f,
            "cd": self.cool_diff_f,
            "out": self.output,
        }
        optional = {
            "pt": self.compressor_delay_min,
            "ah": self.alarm_high_f,
            "al": self.alarm_low_f,
            "ca": self.calibration_f,
            "fault": self.fault or None,
            "amb": self.outdoor_f,
        }
        record.update({k: v for k, v in optional.items() if v is not None})
        return record


def parse_dps(dps: Dict[str, Any], timestamp: Optional[datetime] = None) -> ThermoReading:
    """Turn a raw Tuya `dps` dict into a reading. Raises InkbirdError if incomplete."""
    missing = [dp for dp in REQUIRED_DPS if dp not in dps]
    if missing:
        raise InkbirdError(f"Controller status is missing data points {missing}")

    unit = str(dps[DP_UNIT]).upper()

    def setting(dp: str) -> Optional[float]:
        """Settings are in the controller's own unit; normalize to °F."""
        raw = dps.get(dp)
        if raw is None:
            return None
        value = raw / 10.0
        if unit == "C":
            value = value * 9 / 5 + 32
        return round(value, 2)

    def differential(dp: str) -> Optional[float]:
        """A differential is a span, not a temperature: scale, don't offset."""
        raw = dps.get(dp)
        if raw is None:
            return None
        value = raw / 10.0
        return round(value * 9 / 5 if unit == "C" else value, 2)

    delay = dps.get(DP_COMPRESSOR_DELAY)
    fault = dps.get(DP_FAULT)
    return ThermoReading(
        timestamp=timestamp or datetime.now(),
        water_f=round(dps[DP_WATER_F] / 10.0, 2),
        target_f=setting(DP_TARGET),
        heat_diff_f=differential(DP_HEAT_DIFF),
        cool_diff_f=differential(DP_COOL_DIFF),
        output=str(dps[DP_OUTPUT]),
        compressor_delay_min=int(delay) if delay is not None else None,
        alarm_high_f=setting(DP_ALARM_HIGH),
        alarm_low_f=setting(DP_ALARM_LOW),
        calibration_f=differential(DP_CALIBRATION),
        fault=int(fault) if fault is not None else None,
    )


def _default_device_factory(config: InkbirdConfig):
    # Imported lazily so the tests, and a laptop without tinytuya, can still
    # import this module.
    import tinytuya

    device = tinytuya.Device(config.device_id, config.ip, config.local_key, version=config.version)
    device.set_socketTimeout(config.timeout_seconds)
    device.set_socketRetryLimit(1)
    return device


class InkbirdClient:
    """Reads the controller. Never writes to it.

    Each read opens a fresh session. A persistent session was tried and came
    back with partial `dps` updates on most polls, which is the wrong shape
    for a logger that wants a complete snapshot every time.
    """

    def __init__(
        self,
        config: InkbirdConfig,
        device_factory: Optional[Callable[[InkbirdConfig], Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self._factory = device_factory or _default_device_factory
        self._sleep = sleep

    def read(self) -> ThermoReading:
        last_error = "no attempt made"
        attempts = max(1, self.config.retries)
        for attempt in range(1, attempts + 1):
            try:
                device = self._factory(self.config)
                status = device.status()
            except Exception as exc:  # noqa: BLE001 - tinytuya raises a grab bag
                last_error = f"{type(exc).__name__}: {exc}"
                status = None
            if isinstance(status, dict) and "dps" in status:
                try:
                    return parse_dps(status["dps"])
                except InkbirdError as exc:
                    last_error = str(exc)
            elif isinstance(status, dict):
                last_error = f"{status.get('Error', 'error')} (Err {status.get('Err', '?')})"
            if attempt < attempts:
                logger.debug("Inkbird read attempt %d failed: %s", attempt, last_error)
                self._sleep(self.config.retry_delay_seconds)
        raise InkbirdError(
            f"Could not read Inkbird at {self.config.ip} after {attempts} attempts: {last_error}",
            hint=INKBIRD_HINT,
        )
