"""Outdoor temperature from Open-Meteo (free, no API key).

The logger records outdoor temperature next to every water reading, because
the tub's heat loss is driven by the gap between the two. Fitting that
relationship later is the whole point of phase 1.

Open-Meteo's "current" value is a model estimate refreshed every 15 minutes,
not a thermometer in the yard. That's fine for fitting a slow thermal model,
but it won't see a microclimate.
"""

import logging
import time
from typing import Any, Callable, Optional

import requests

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# How long a cached reading may stand in for a failed fetch. Boulder can move
# 20°F in a few hours when a front or a chinook comes through, so don't let a
# stale number pose as current for long.
MAX_STALE_SECONDS = 2 * 3600


class OutdoorTemperature:
    """Caches the current outdoor temperature and refreshes it on a schedule."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        refresh_seconds: float = 900.0,
        session: Any = requests,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 10.0,
    ):
        self.latitude = latitude
        self.longitude = longitude
        self.refresh_seconds = refresh_seconds
        self._session = session
        self._clock = clock
        self._timeout = timeout_seconds
        self._value: Optional[float] = None
        self._fetched_at: Optional[float] = None

    def _fetch(self) -> float:
        response = self._session.get(
            OPEN_METEO_URL,
            params={
                "latitude": self.latitude,
                "longitude": self.longitude,
                "current": "temperature_2m",
                "temperature_unit": "fahrenheit",
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        return float(response.json()["current"]["temperature_2m"])

    def current(self) -> Optional[float]:
        """Outdoor °F, or None if there is no reasonably fresh value.

        Never raises: a weather outage must not stop the water from being logged.
        """
        now = self._clock()
        if self._fetched_at is not None and now - self._fetched_at < self.refresh_seconds:
            return self._value
        try:
            self._value = self._fetch()
            self._fetched_at = now
            return self._value
        except Exception as exc:  # noqa: BLE001 - network, HTTP, or a changed payload
            age = None if self._fetched_at is None else now - self._fetched_at
            if age is not None and age < MAX_STALE_SECONDS:
                logger.warning("Outdoor temperature fetch failed (%s); reusing a %.0f-min-old value",
                               exc, age / 60)
                return self._value
            logger.warning("Outdoor temperature fetch failed (%s); logging without it", exc)
            return None
