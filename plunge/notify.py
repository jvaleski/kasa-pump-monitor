"""Pluggable alert channels.

Add a channel by writing a `Channel` subclass and registering it in `_REGISTRY`.
Channels are configured as a list under `alerts.channels`, each with a `type`,
an `enabled` flag, and an optional `min_severity` filter:

    {"type": "ntfy", "enabled": true, "min_severity": "info", "topic": "plunge-abc123"}
    {"type": "twilio", "enabled": false, "min_severity": "critical", "to_number": "+15551234567"}

A channel that throws is logged and skipped. Losing a notifier must never take
down the monitor, and one dead channel must not block the others.
"""

import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

import requests

from .detect import Alert, Severity

logger = logging.getLogger(__name__)

_SEVERITY_ORDER = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


class Channel:
    """One delivery mechanism for alerts."""

    type_name = "base"

    def __init__(self, settings: Dict[str, Any]):
        self.settings = settings
        raw = str(settings.get("min_severity", "info")).lower()
        try:
            self.min_severity = Severity(raw)
        except ValueError:
            logger.warning("Unknown min_severity %r on %s channel; using 'info'", raw, self.type_name)
            self.min_severity = Severity.INFO

    def accepts(self, alert: Alert) -> bool:
        return _SEVERITY_ORDER[alert.severity] >= _SEVERITY_ORDER[self.min_severity]

    def send(self, alert: Alert) -> None:
        raise NotImplementedError

    def _require(self, *keys: str) -> List[str]:
        """Fetch required settings, supporting a `<key>_env` indirection for secrets."""
        values = []
        for key in keys:
            value = self.settings.get(key)
            env_var = self.settings.get(f"{key}_env")
            if not value and env_var:
                value = os.environ.get(env_var)
            if not value:
                raise ValueError(f"{self.type_name} channel is missing required setting '{key}'")
            values.append(value)
        return values


class LogChannel(Channel):
    """Writes to the log. Always on as a last resort so nothing is silently lost."""

    type_name = "log"

    def send(self, alert: Alert) -> None:
        level = {
            Severity.CRITICAL: logging.ERROR,
            Severity.WARNING: logging.WARNING,
            Severity.INFO: logging.INFO,
        }[alert.severity]
        logger.log(level, "ALERT %s — %s", alert.title, alert.body)


class NtfyChannel(Channel):
    """Push via ntfy.sh. No account or API key; the topic name is the secret."""

    type_name = "ntfy"

    _PRIORITY = {Severity.CRITICAL: "urgent", Severity.WARNING: "high", Severity.INFO: "default"}
    _TAGS = {Severity.CRITICAL: "rotating_light", Severity.WARNING: "warning", Severity.INFO: "white_check_mark"}

    def send(self, alert: Alert) -> None:
        (topic,) = self._require("topic")
        server = self.settings.get("server", "https://ntfy.sh").rstrip("/")
        response = requests.post(
            f"{server}/{topic}",
            data=alert.body.encode("utf-8"),
            headers={
                "Title": alert.title,
                "Priority": self._PRIORITY[alert.severity],
                "Tags": self._TAGS[alert.severity],
            },
            timeout=15,
        )
        response.raise_for_status()


class TwilioChannel(Channel):
    """SMS via Twilio."""

    type_name = "twilio"

    def send(self, alert: Alert) -> None:
        sid, token, from_number, to_number = self._require(
            "account_sid", "auth_token", "from_number", "to_number"
        )
        response = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": from_number, "To": to_number, "Body": f"{alert.title}\n{alert.body}"},
            auth=(sid, token),
            timeout=20,
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Twilio returned {response.status_code}: {response.text[:200]}")


class MacNotificationChannel(Channel):
    """Banner on this Mac. Secondary only — useless when you're away from it."""

    type_name = "macos"

    def send(self, alert: Alert) -> None:
        # Quotes would break out of the AppleScript string literal.
        title = alert.title.replace('"', "'")
        body = alert.body.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            check=True,
            capture_output=True,
            timeout=15,
        )


_REGISTRY = {
    cls.type_name: cls
    for cls in (LogChannel, NtfyChannel, TwilioChannel, MacNotificationChannel)
}


class Notifier:
    """Fans an alert out to every enabled channel that accepts its severity."""

    def __init__(self, channel_settings: List[Dict[str, Any]]):
        self.channels: List[Channel] = []
        for settings in channel_settings:
            if not settings.get("enabled", True):
                continue
            type_name = settings.get("type")
            channel_cls = _REGISTRY.get(type_name)
            if channel_cls is None:
                logger.warning(
                    "Unknown alert channel type %r (known: %s)", type_name, ", ".join(sorted(_REGISTRY))
                )
                continue
            self.channels.append(channel_cls(settings))

        if not self.channels:
            logger.warning("No alert channels enabled; falling back to the log only")
            self.channels.append(LogChannel({}))

    def send(self, alert: Optional[Alert]) -> None:
        if alert is None:
            return
        for channel in self.channels:
            if not channel.accepts(alert):
                continue
            try:
                channel.send(alert)
                logger.debug("Sent alert via %s", channel.type_name)
            except Exception as exc:  # noqa: BLE001 - a bad channel must not stop the others
                logger.error("Alert channel %s failed: %s", channel.type_name, exc)

    def describe(self) -> List[str]:
        return [f"{c.type_name} (>= {c.min_severity.value})" for c in self.channels]
