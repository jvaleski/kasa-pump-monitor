"""Smart plug access: connect, read power, and translate failures into useful errors."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from kasa import AuthenticationError, Credentials, Discover, KasaException, UnsupportedDeviceError

from .config import DeviceConfig

logger = logging.getLogger(__name__)

# The KP125M dropped local access once before, after a firmware update moved it
# to TP-Link's TPAP scheme. python-kasa can't speak TPAP, so surface the fix
# rather than a bare stack trace.
TPAP_HINT = (
    "The plug is advertising the TPAP encryption scheme, which python-kasa cannot "
    "speak. Fix: open the Kasa app, go to the plug's settings, and toggle "
    "'Third-Party Compatibility' (sometimes 'Local Access') off and then on again."
)


class DeviceError(Exception):
    """A read failed. `hint` carries a human-actionable next step when we have one."""

    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.hint = hint


@dataclass
class Reading:
    """One power sample from the plug."""

    timestamp: datetime
    is_on: bool
    watts: float
    volts: Optional[float] = None
    amps: Optional[float] = None

    def to_dict(self) -> dict:
        record = {
            "t": self.timestamp.isoformat(timespec="seconds"),
            "on": self.is_on,
            "w": round(self.watts, 3),
        }
        if self.volts is not None:
            record["v"] = round(self.volts, 2)
        if self.amps is not None:
            record["a"] = round(self.amps, 3)
        return record


class PlugClient:
    """Holds a connection to the plug and reads power from it.

    The connection is reused across polls; `read()` reconnects on demand if the
    device object has gone stale, so callers never have to manage that.
    """

    def __init__(self, config: DeviceConfig):
        self.config = config
        self._device = None

    async def _connect(self):
        credentials = Credentials(self.config.username, self.config.password)
        try:
            return await asyncio.wait_for(
                Discover.discover_single(
                    self.config.ip,
                    credentials=credentials,
                    timeout=int(self.config.timeout_seconds),
                ),
                timeout=self.config.timeout_seconds * 2,
            )
        except UnsupportedDeviceError as exc:
            raise DeviceError(f"Plug at {self.config.ip} speaks an unsupported protocol: {exc}",
                              hint=TPAP_HINT) from exc
        except AuthenticationError as exc:
            raise DeviceError(
                f"Authentication rejected by {self.config.ip}: {exc}",
                hint="Check the TP-Link account password. " + TPAP_HINT,
            ) from exc
        except asyncio.TimeoutError as exc:
            raise DeviceError(
                f"Timed out connecting to {self.config.ip} after "
                f"{self.config.timeout_seconds * 2:.0f}s",
                hint="Is the plug powered and on the network?",
            ) from exc
        except KasaException as exc:
            raise DeviceError(f"Could not connect to {self.config.ip}: {exc}") from exc

    async def read(self) -> Reading:
        """Take one power reading, reconnecting once if the cached device is stale."""
        for attempt in (1, 2):
            if self._device is None:
                self._device = await self._connect()
            try:
                await asyncio.wait_for(
                    self._device.update(), timeout=self.config.timeout_seconds
                )
                break
            except (KasaException, asyncio.TimeoutError, OSError) as exc:
                await self.close()
                if attempt == 2:
                    raise DeviceError(f"Failed to read {self.config.ip}: {exc}") from exc
                logger.debug("Read attempt %d failed (%s), reconnecting", attempt, exc)

        device = self._device
        energy = device.modules.get("Energy")
        if energy is None:
            raise DeviceError(
                f"Plug {device.alias} exposes no Energy module "
                f"(modules: {sorted(device.modules)})",
                hint="This model may not support energy monitoring.",
            )

        watts = energy.current_consumption
        if watts is None:
            raise DeviceError("Energy module returned no current_consumption value")

        return Reading(
            timestamp=datetime.now(),
            is_on=bool(device.is_on),
            watts=float(watts),
            volts=getattr(energy, "voltage", None),
            amps=getattr(energy, "current", None),
        )

    async def describe(self) -> dict:
        """Device identity, for `status` output.

        Discovery alone leaves alias and modules unpopulated, so this has to
        update before reading them.
        """
        if self._device is None:
            self._device = await self._connect()
        device = self._device
        try:
            await asyncio.wait_for(device.update(), timeout=self.config.timeout_seconds)
        except (KasaException, asyncio.TimeoutError, OSError) as exc:
            raise DeviceError(f"Failed to query {self.config.ip}: {exc}") from exc
        return {
            "alias": device.alias,
            "model": device.model,
            "ip": self.config.ip,
            "is_on": device.is_on,
            "modules": sorted(device.modules),
        }

    async def close(self) -> None:
        """Drop the connection, swallowing shutdown races."""
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:  # noqa: BLE001 - never let teardown mask a real error
                logger.debug("Ignoring error while disconnecting", exc_info=True)
            self._device = None

    async def __aenter__(self) -> "PlugClient":
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.close()
