#!/usr/bin/env python3
"""Plunge thermostat logger (Inkbird ITC-308-WIFI), phase 1: read-only.

    ./thermo.py log       poll continuously and append to thermo_history.jsonl (what systemd runs)
    ./thermo.py once      a single poll
    ./thermo.py status    read the controller now, plus a 24h summary, as JSON

Nothing in this phase writes to the controller. See plunge/inkbird.py for why.
"""

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from plunge.config import ConfigError
from plunge.inkbird import InkbirdError
from plunge.thermo_config import ThermoConfig
from plunge.thermo_logger import ThermoLogger, recent_summary

DEFAULT_CONFIG = Path(__file__).parent / "thermo_config.json"


def setup_logging(config: ThermoConfig, verbose: bool) -> None:
    storage = config.storage
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            RotatingFileHandler(
                config.resolve(storage.log_file),
                maxBytes=int(storage.log_max_mb * 1024 * 1024),
                backupCount=storage.log_backups,
            ),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    for noisy in ("urllib3", "tinytuya"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["log", "once", "status"])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        config = ThermoConfig.load(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(config, args.verbose)
    thermo = ThermoLogger(config)

    if args.command == "log":
        try:
            thermo.watch()
        except KeyboardInterrupt:
            return 0
        return 0

    if args.command == "once":
        thermo.prune()  # under cron the loop's periodic prune never runs
        return 0 if thermo.poll_once() is not None else 1

    try:
        reading = thermo.client.read()
    except InkbirdError as exc:
        print(json.dumps({"error": str(exc), "hint": exc.hint}, indent=2))
        return 1
    reading.outdoor_f = thermo.outdoor.current()
    now = reading.to_dict()
    now["output_name"] = reading.output_name
    print(json.dumps({"now": now, "last_24h": recent_summary(config)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
