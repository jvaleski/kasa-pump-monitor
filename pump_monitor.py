#!/usr/bin/env python3
"""Cold plunge pump monitor.

Watches the circulation pump's power draw through a Kasa KP125M smart plug and
alerts when it stops, cavitates, strains, or goes unreachable.

    ./pump_monitor.py calibrate     learn the pump's normal draw (run this first)
    ./pump_monitor.py watch         poll continuously (what systemd runs)
    ./pump_monitor.py once          single poll, for cron
    ./pump_monitor.py status        current state and recent history
    ./pump_monitor.py fit-voltage   model out line-voltage swing (after a day of data)
    ./pump_monitor.py test-alert    verify the notification channels
"""

import argparse
import asyncio
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from plunge.config import Config, ConfigError
from plunge.device import DeviceError
from plunge.monitor import Monitor

DEFAULT_CONFIG = Path(__file__).parent / "pump_config.json"


def setup_logging(config: Config, verbose: bool) -> None:
    storage = config.storage
    handlers = [
        # Rotating, not plain: one line per poll would otherwise grow without
        # bound on a box that runs for years.
        RotatingFileHandler(
            config.resolve(storage.log_file),
            maxBytes=int(storage.log_max_mb * 1024 * 1024),
            backupCount=storage.log_backups,
        ),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # python-kasa and urllib3 are chatty at DEBUG and drowned the old log file
    # in Twilio request dumps.
    for noisy in ("kasa", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def require_calibration(config: Config) -> None:
    if not config.pump.is_calibrated:
        raise ConfigError(
            "The pump has not been calibrated yet.\n"
            "Start the pump, let it settle, then run: ./pump_monitor.py calibrate"
        )


async def run(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    setup_logging(config, args.verbose)
    monitor = Monitor(config)

    if args.command == "calibrate":
        result = await monitor.calibrate(args.duration * 60, args.interval)
        failed = f" ({result['failures']} reads failed)" if result["failures"] else ""
        reference = result["reference_volts"]
        lines = [
            f"\nCalibrated from {result['samples']} samples{failed}:",
            f"  normal draw : {result['median']:.2f}W "
            f"(observed {result['min']:.2f}–{result['max']:.2f}W, stdev {result['stdev']:.3f}W)",
            f"  alert band  : {result['running_min']:.2f}–{result['running_max']:.2f}W",
            f"  observed noise uses {result['headroom_ratio'] * 100:.0f}% of the alert band",
        ]
        if reference:
            lines.append(f"  reference   : {reference:.1f}V line voltage")
        if not result["normalizing"]:
            lines.append(
                "\nVoltage normalization is off. Mains voltage moves the pump's draw by a few\n"
                "percent through the day, which eats alert headroom. After a day of history,\n"
                "run './pump_monitor.py fit-voltage' to model it out and tighten the band."
            )
        lines.append(f"\nWritten to {config.path}. Start monitoring with: ./pump_monitor.py watch")
        print("\n".join(lines))
        return 0

    if args.command == "fit-voltage":
        result = monitor.fit_voltage(apply=args.apply)
        low, high = result["voltage_range"]
        print(
            f"\nFitted from {result['samples']} readings spanning {low:.1f}–{high:.1f}V "
            f"({result['voltage_spread_pct']:.1f}% spread):\n"
            f"  model     : P = C * V ** {result['exponent']:.3f}\n"
            f"  reference : {result['reference_volts']:.1f}V\n"
            f"  scatter   : {result['scatter_before_pct']:.2f}% -> "
            f"{result['scatter_after_pct']:.2f}% "
            f"({result['improvement_pct']:.0f}% less variation)\n"
        )
        if result["applied"]:
            print(f"Applied and written to {config.path}.")
        else:
            print("Nothing was written. Re-run with --apply to save this model.")
        return 0

    if args.command == "status":
        report = await monitor.status()
        print(json.dumps(report, indent=2, default=str))
        return 0 if "device_error" not in report else 1

    if args.command == "test-alert":
        await monitor.test_alert()
        print(f"Test alert dispatched to: {', '.join(monitor.notifier.describe())}")
        return 0

    require_calibration(config)

    if args.command == "once":
        # Cron re-enters the process every poll, so the loop's periodic prune
        # never fires. Prune here or history grows without bound.
        monitor.prune()
        reading = await monitor.poll_once()
        await monitor.client.close()
        return 0 if reading is not None else 1

    if args.command == "watch":
        try:
            await monitor.watch()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        return 0

    raise ValueError(f"unhandled command {args.command!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-c", "--config", default=str(DEFAULT_CONFIG), help="path to the config file"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("watch", help="poll continuously until stopped")
    sub.add_parser("once", help="take a single reading and exit")
    sub.add_parser("status", help="show current state as JSON")
    sub.add_parser("test-alert", help="send a test alert through every channel")

    calibrate = sub.add_parser("calibrate", help="learn the pump's normal power draw")
    calibrate.add_argument(
        "-d", "--duration", type=float, default=10, help="minutes to sample (default: 10)"
    )
    calibrate.add_argument(
        "-i", "--interval", type=float, default=10, help="seconds between samples (default: 10)"
    )

    fit = sub.add_parser(
        "fit-voltage",
        help="model out line-voltage effects using collected history",
    )
    fit.add_argument(
        "--apply", action="store_true", help="write the fitted model to the config"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except (ConfigError, DeviceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        hint = getattr(exc, "hint", None)
        if hint:
            print(f"\n{hint}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
