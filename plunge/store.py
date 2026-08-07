"""Persistence for readings and detector state.

Readings go to a JSONL file, appended one line at a time. The old monitor
re-serialized an entire JSON array on every poll, which meant the whole history
was rewritten once a minute and a crash mid-write could truncate all of it.
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .detect import MonitorState
from .device import Reading

logger = logging.getLogger(__name__)


def append_reading(path: Path, reading: Reading) -> None:
    """Append one reading. Cheap enough to call every poll."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(json.dumps(reading.to_dict()) + "\n")


def load_history(path: Path, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Read history, skipping any lines that got corrupted by a partial write."""
    if not path.exists():
        return []

    entries: List[Dict[str, Any]] = []
    with open(path) as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("Skipping malformed history line %d in %s", line_number, path)
                continue
            if since is not None:
                stamp = entry.get("t")
                if not stamp:
                    continue
                try:
                    if datetime.fromisoformat(stamp) < since:
                        continue
                except ValueError:
                    continue
            entries.append(entry)
    return entries


def prune_history(path: Path, retain_days: float) -> int:
    """Drop readings older than `retain_days`. Returns how many were removed."""
    if not path.exists() or retain_days <= 0:
        return 0

    cutoff = datetime.now() - timedelta(days=retain_days)
    kept = load_history(path, since=cutoff)
    total = sum(1 for line in open(path) if line.strip())
    removed = total - len(kept)
    if removed <= 0:
        return 0

    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as handle:
        for entry in kept:
            handle.write(json.dumps(entry) + "\n")
    os.replace(tmp, path)
    logger.info("Pruned %d readings older than %.0f days from %s", removed, retain_days, path.name)
    return removed


def load_state(path: Path) -> MonitorState:
    """Load detector state, starting fresh if it is missing or unreadable."""
    if not path.exists():
        return MonitorState()
    try:
        with open(path) as handle:
            return MonitorState.from_dict(json.load(handle))
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning("Could not read state from %s (%s); starting fresh", path, exc)
        return MonitorState()


def save_state(path: Path, state: MonitorState) -> None:
    """Write state atomically so a crash can't leave a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w") as handle:
        json.dump(state.to_dict(), handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
