# CLAUDE.md

Guidance for Claude Code (and humans) working in this repo.

## What this is

A monitor for an always-on appliance on a TP-Link Kasa KP125M smart plug. It
polls power draw, classifies it into a health state, debounces, and alerts.
Built for a cold plunge circulation pump; nothing is plunge-specific beyond the
naming.

**The load-bearing assumption is that the appliance runs 24/7.** That is what
makes a threshold-plus-debounce design correct. Do not add duty-cycle or
schedule logic without revisiting the whole detector — an appliance that cycles
would need a fundamentally different approach.

## Quick start

```bash
pip3 install -r requirements.txt
cp pump_config.example.json pump_config.json
```

Then edit `pump_config.json` — see **What you must fill in** below.

```bash
./pump_monitor.py status        # confirm the plug is reachable
./pump_monitor.py calibrate     # with the appliance running normally, ~10 min
./pump_monitor.py test-alert    # confirm notifications actually arrive
./pump_monitor.py watch         # run it
```

Deploy to Linux with `./deploy/install.sh` (systemd). See **Deploying**.

## What you must fill in

`pump_config.json` is gitignored because it holds secrets. Copy the example and
populate:

| Field | Required | Notes |
| --- | --- | --- |
| `device.ip` | **yes** | Plug's LAN IP. Give it a DHCP reservation — the config doesn't rediscover. |
| `device.username` | **yes** | Your TP-Link/Kasa **account email**, not a device name. |
| `device.password` | **yes** | Your TP-Link account password. Newer Kasa firmware requires it even for local access. |
| `pump.running_watts` | written for you | Leave `null`; `calibrate` fills it. |
| `pump.voltage_reference` | written for you | Same — `calibrate` records the voltage it saw. |
| `alerts.channels[].topic` | if using ntfy | **The topic name is the only secret.** Pick something unguessable, e.g. `pump-$(openssl rand -hex 5)`. Anyone who knows it can read your alerts and send you fake ones. |
| `alerts.channels[].api_key` | if the channel needs one | Or use the env indirection below. |

**Any secret can live in the environment instead.** Replace the key with
`<key>_env` naming a variable:

```json
"password_env": "KASA_PASSWORD"
```

Inline wins if both are present. The example config ships with
`"password_env": "KASA_PASSWORD"` rather than an inline password, deliberately.

**Never commit `pump_config.json`.** It is in `.gitignore`; keep it there.
`install.sh` chmod 600s it on the target.

## Python version decides whether you get voltage

Read this before debugging why `fit-voltage` won't run.

`python-kasa` 0.8+ needs **Python 3.11**. Raspberry Pi OS Bullseye ships 3.9,
so pip falls back to 0.7.7 there — `requirements.txt` handles the split with
environment markers.

**On 0.7.7 the KP125M never populates `voltage` or `current`** (the attributes
exist and return `None`). Consequences:

- power alerting works fully;
- `PumpConfig.normalize()` no-ops, since it needs volts;
- `fit-voltage` has nothing to fit and refuses;
- so `running_tolerance_pct` must stay conservative (±10%).

Voltage normalization requires Python 3.11+. Diagnose with:

```bash
.venv/bin/python3 -c "import kasa; print(kasa.__version__)"
grep -c '"v"' pump_history.jsonl     # 0 = no voltage being recorded
```

## Architecture

Dependency flow is a clean DAG — read it bottom-up, each layer stands alone:

```
config.py   ← no internal deps; load, validate, resolve secrets, voltage model
device.py   ← config;                 Kasa I/O → Reading
detect.py   ← config, device;         Reading → PumpState → Alert   (the brain)
store.py    ← detect, device;         JSONL history + atomic state
notify.py   ← detect;                 Alert → channels
monitor.py  ← all of the above;       poll loop, calibrate, fit-voltage, status
pump_monitor.py                       CLI, argparse, logging setup
```

`detect.py` is the interesting part and is fully unit-testable without a plug
or a network.

### One poll, end to end (`monitor.py:poll_once`)

1. `client.read()` → `Reading`. On failure feeds `UNREACHABLE` to the detector
   rather than raising — **the loop must outlive any single poll**.
2. `store.append_reading()` — one JSONL line.
3. `detector.classify()` → `PumpState` from the band.
4. `detector.observe()` → maybe an `Alert` (after debounce).
5. `notifier.send()`.
6. `store.save_state()` — atomic.

Every 60 polls: drift check. Every 1440: prune history. Deliberately off the
every-minute path.

### Design decisions that look odd but aren't

- **Detector state is persisted.** The predecessor tracked consecutive failures
  on an instance attribute while running once per cron invocation, so the
  counter reset every minute and the alert threshold was mathematically
  unreachable. See the comment at `detect.py:88`.
- **History is JSONL, not a JSON array.** The predecessor rewrote the whole
  array every poll; a crash mid-write truncated everything. Appending is O(1)
  and one corrupt line is skipped, not fatal.
- **Drift compares medians over 24h**, because a slowly clogging filter can sit
  inside the instantaneous band for weeks.
- **`once` prunes explicitly.** Under cron the process re-enters every poll, so
  the loop's periodic prune never fires and history would grow forever.
- **`watch` sends an INFO startup ping.** A dead monitor and a healthy quiet one
  look identical from outside; this distinguishes them.
- **Alert severity is deliberate.** `STRAINING` and `UNREACHABLE` are only
  WARNING and recovery is INFO, so a `critical`-only channel silently drops the
  early clog signal. Default channels to `info`.

## Testing

```bash
python3 -m pytest tests/ -q          # or: python3 -m unittest discover -s tests
```

No device or network needed — `Monitor.__init__` does no I/O beyond reading
state off disk, so a real `Monitor` can be built against a temp config.

**If you change behaviour, mutation-test your tests.** Copy the repo to /tmp,
break the thing on purpose, and confirm a test fails. This caught two real gaps:
tests called `announce_startup()` and `prune()` directly, so deleting the calls
from `watch()` broke nothing. `TestWatchStartupWiring` and
`TestOnceCommandWiring` exist because of that.

## Deploying

```bash
rsync -a --exclude='legacy/' --exclude='__pycache__/' --exclude='.venv/' \
      ./ user@host:~/kasa/
ssh user@host 'cd ~/kasa && ./deploy/install.sh'
```

`install.sh` is idempotent. It builds a venv (PEP 668 makes system pip refuse),
installs deps, chmod 600s the config, **refuses to install if the config has no
calibration**, writes `/etc/systemd/system/plunge-monitor.service` with real
paths baked in, then enables and starts it.

```bash
systemctl status plunge-monitor
journalctl -u plunge-monitor -f
./deploy/install.sh --uninstall
```

Prerequisites on the target: Python 3.9+, `python3-venv` (`sudo apt install
python3-venv` — Bullseye often lacks it), systemd, and **network reachability
to the plug**. That last one is the most common failure; verify with a ping
before installing.

Calibration describes the *appliance*, not the host, so calibrating on a laptop
and copying `pump_config.json` to the target is valid.

**Retire any predecessor.** Two monitors on one plug means duplicate alerts and
two divergent histories. Check `crontab -l` on the target.

## Gotchas

- **Disk is bounded, but only because it's configured to be.** Logs rotate via
  `storage.log_max_mb` × `log_backups` (default 5MB × 3). History prunes at
  `retain_days` (30). Don't swap `RotatingFileHandler` back to `FileHandler`.
- **The plug can vanish after a firmware update.** TP-Link's TPAP encryption
  isn't supported by python-kasa. Fix: toggle *Third-Party Compatibility* off
  and on in the Kasa app. The monitor prints this hint automatically.
- **Don't trust an HTTP 2xx as proof a notification arrived.** An email channel
  was removed after it returned `200` with a message id and then failed
  asynchronously at delivery, invisibly. Verify out of band — for ntfy, poll the
  topic back:
  ```bash
  curl -s "https://ntfy.sh/<topic>/json?poll=1&since=10m"
  ```
- **`calibrate` refuses bad input by design.** If it errors about swing, the
  appliance likely cycled mid-calibration; the resulting median would be a
  fiction halfway between on and off.
- **Centrifugal pumps may invert the expected signal.** A restricted *intake*
  usually lowers draw rather than raising it, so a clog can classify as
  `cavitating` rather than `straining`. Both alert, so coverage is fine, but the
  descriptions in `detect.py` could point at the wrong cause.

## Conventions

- Standard library only, beyond `python-kasa` and `requests`.
- Comments explain *why*, especially where the code looks wrong but isn't.
  Match that density; don't add narration of what the code plainly does.
- Errors carry an actionable `hint` where one exists (see `DeviceError`).
- A failing alert channel is logged and skipped — losing a notifier must never
  take down the monitor.
