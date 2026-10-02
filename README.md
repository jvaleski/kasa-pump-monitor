# Plunge Pump Monitor

Watches a cold plunge circulation pump through a TP-Link Kasa KP125M smart plug
and alerts when it stops, cavitates, strains, or goes unreachable.

The pump runs continuously, so its power draw *is* the health signal. It's a
very steady load — on the reference install, 36 calibration samples landed
within **0.24W of each other** (80.47–80.71W) — so real deviations stand out
clearly against the noise.

Nothing here is plunge-specific beyond the naming. It suits any always-on
appliance with a stable draw: a well pump, a sump pump, an aquarium filter, a
fridge compressor.

Development happens on macOS; the service runs on a Linux box (Raspberry Pi or
similar) on the same LAN as the plug.

## How it decides something is wrong

| Draw | State | Meaning |
| --- | --- | --- |
| `<= 5W` | `stopped` | Dead, tripped, or unplugged. Water is not circulating. |
| `5W .. running_min` | `cavitating` | Spinning against air — lost prime or low water. |
| `running_min .. running_max` | `running` | Healthy. |
| `> running_max` | `straining` | Restriction — clogged filter, failing bearings. |
| relay off | `plug_off` | The plug itself was switched off. |
| no response | `unreachable` | Network problem, or the power is out. |

`running_min`/`running_max` are the calibrated draw ±`running_tolerance_pct`.
On the reference install that's 80.58W ±10%, so **72.5–88.6W**. Your numbers
come from `calibrate`.

Three things keep alerts trustworthy:

- **Debounce.** A state change must repeat for `confirm_samples` consecutive
  polls (default 3, so 3 minutes) before it alerts. One noisy reading or a brief
  network blip stays quiet.
- **Voltage normalization.** See below — mains voltage moves the pump's draw by
  several percent through the day, and that's not a fault.
- **Drift detection.** Every hour the monitor compares the 24-hour median draw
  against the calibrated baseline. A filter that clogs slowly can creep upward
  for weeks while every individual reading still sits inside the band; comparing
  medians catches that. Fires at `drift_alert_pct` (default 4%).

Once a problem is confirmed you get one alert, then a reminder every
`cooldown_seconds` (default 30 min) until it clears, then a recovery notice.

## Line voltage matters more than you'd think

Two readings taken twelve hours apart:

| | Evening | Morning |
| --- | --- | --- |
| Power | 80.58 W | 83.03 W (+3.0%) |
| Line voltage | 113.16 V | 122.08 V (+7.9%) |

The pump didn't change; the grid did. A 3% swing from voltage alone would eat
half of a 6% alert band, and would trip the 4% drift alarm on a daily cycle.

Two mitigations are in place. The band starts at a conservative ±10%, and the
monitor can model the effect out entirely as `P = C * V ** k`, comparing every
reading against what it *should* draw at the measured voltage. Fit `k` from real
data once you have a day or so of history:

```bash
./pump_monitor.py fit-voltage           # show the fit, change nothing
./pump_monitor.py fit-voltage --apply   # save it
```

It reports how much of the scatter the model removes, and refuses to fit when
voltage was too flat over the sampled period to say anything meaningful. With
normalization on you can safely tighten `running_tolerance_pct` toward 5%, which
buys back real sensitivity. Until then it stays off (`voltage_exponent: 0`).

## Setup

```bash
pip3 install -r requirements.txt
cp pump_config.example.json pump_config.json   # then fill in device + alerts
```

With the pump running normally, learn its signature:

```bash
./pump_monitor.py calibrate            # 10 min by default
```

This refuses to write a baseline that doesn't look like a steadily running pump
— if the draw is at standby, or swings more than the tolerance (meaning the pump
cycled mid-calibration), it errors instead of saving a bogus number.

Calibration describes the *pump*, not the machine watching it, so you can
calibrate on your Mac and copy `pump_config.json` to the Pi.

## Deploying to Linux

```bash
rsync -a --exclude='legacy/' --exclude='__pycache__/' --exclude='.venv/' \
      ./ pi@YOUR-PI:~/kasa/                    # includes your calibrated config
ssh pi@YOUR-PI
cd ~/kasa && ./deploy/install.sh
```

`install.sh` creates a virtualenv unconditionally — Raspberry Pi OS Bookworm and
later mark the system Python as externally managed (PEP 668) so pip refuses to
install into it, and on older releases a venv is still the right call because it
pins `python-kasa` independently of anything else on the box. It then installs
dependencies, `chmod 600`s the config, generates
`/etc/systemd/system/plunge-monitor.service` with the real paths baked in, then
enables and starts it. It refuses to install if the config has no calibration,
rather than leaving you with a service that fails on every start.

### Python version decides whether you get voltage

This catches people out, so it's worth stating plainly.

`python-kasa` 0.8+ requires **Python 3.11**. Raspberry Pi OS **Bullseye ships
3.9**, so on Bullseye pip resolves to the last compatible release, 0.7.7.
`requirements.txt` encodes this with environment markers, so each host picks
what it can actually run:

```
python-kasa>=0.10.0; python_version >= "3.11"
python-kasa==0.7.7;  python_version <  "3.11"
```

The catch: **on 0.7.7 the KP125M reports `current_consumption` but leaves
`voltage` and `current` as `None`.** The attributes exist; they're just never
populated. That means:

- power-based alerting works completely — stopped, cavitating, straining,
  unreachable all behave normally;
- `normalize()` silently no-ops on every reading, because it needs volts;
- `fit-voltage` has nothing to fit and will refuse.

So on Bullseye, voltage normalization is not deferred — it's **unavailable**,
and `running_tolerance_pct` should stay at a conservative ±10%. If you want the
tighter band, you need Python 3.11+: Bookworm ships it, or `pyenv install
3.11` leaves your OS alone (expect a long compile on older Pi hardware).

Check before you wonder why `fit-voltage` never works:

```bash
.venv/bin/python3 -c "import kasa; print(kasa.__version__)"
grep -c '"v"' pump_history.jsonl      # 0 means no voltage is being recorded
```

```bash
systemctl status plunge-monitor
journalctl -u plunge-monitor -f
./deploy/install.sh --uninstall
```

The service runs `watch` as one long-lived process rather than re-invoking
`once` on a timer. That keeps a single connection open instead of rediscovering
the plug every minute, and keeps the debounce counters warm.

## Commands

```bash
./pump_monitor.py watch        # poll continuously (what systemd runs)
./pump_monitor.py once         # single poll, for cron
./pump_monitor.py status       # current state + 24h stats, as JSON
./pump_monitor.py calibrate    # relearn the normal draw
./pump_monitor.py fit-voltage  # model out line-voltage swing
./pump_monitor.py test-alert   # verify every configured channel
```

## Alerts

Channels are a list under `alerts.channels`; each has a `type`, an `enabled`
flag, and an optional `min_severity` (`info` / `warning` / `critical`).

The reference install runs **log** and **ntfy** (push), both at
`min_severity: info`.

ntfy needs no account, no API key, and no domain — the topic name *is* the
secret, so keep it out of screenshots and public repos. Subscribe with the ntfy
app (iOS/Android) or a browser at `ntfy.sh/<topic>`. The public server holds
messages for about 12 hours; `pump_history.jsonl` and the log are the durable
record.

`info` is deliberate, not lazy. STRAINING and UNREACHABLE are only WARNING, and
recovery notices are INFO — a `critical` filter would silently drop the early
clog signal and never tell you the pump came back. Alerts fire on state
transitions, not per reading, so this does not spam.

There is no email channel. Resend was tried and removed: its shared
`onboarding@resend.dev` sender is rejected on an account that has its own
verified domain, and — the part that makes it dangerous — the API returns
**HTTP 200 with an email id**, then fails asynchronously at delivery. Nothing
in-process can tell. Any email channel added here should verify delivery out of
band rather than trusting a 2xx.

Also built in, disabled by default:

- **twilio** — SMS. Set `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` and the numbers.
- **macos** — local banner, for testing on the Mac. No-op on Linux.

A channel that fails is logged and skipped, so one dead notifier can't take down
the monitor or block the others. Verify the whole path with `test-alert`.

Any secret can move to the environment: replace the key with `<key>_env` naming
an environment variable, e.g. `"password_env": "KASA_PASSWORD"`.

## If the plug stops responding

A firmware update once moved this plug to TP-Link's **TPAP** encryption scheme,
which python-kasa cannot speak ([issue #1590](https://github.com/python-kasa/python-kasa/issues/1590),
open since Oct 2025). Symptom:

```
UnsupportedDeviceError: Unsupported device ... encrypt_type='TPAP'
```

Fix: in the Kasa app, open the plug's settings and toggle **Third-Party
Compatibility** (sometimes "Local Access") off and back on. The monitor prints
this hint automatically when it sees that error.

## Layout

```
pump_monitor.py       CLI entry point
plunge/config.py      config loading, validation, voltage model, secrets
plunge/device.py      plug connection and power reads
plunge/detect.py      state machine, debounce, drift  <- the interesting part
plunge/notify.py      pluggable alert channels
plunge/store.py       JSONL history + persisted state
plunge/monitor.py     poll loop, calibration, voltage fitting
deploy/install.sh     systemd installer
tests/                python3 -m unittest discover -s tests
```

Files written at runtime: `pump_history.jsonl` (readings, pruned to
`retain_days`), `pump_state.json` (detector state), `pump_monitor.log`.

## Notes on the rewrite

The previous `power_monitor.py` monitored the whole plunge unit, chiller
included, which is why its history shows both ~80W and ~1519W readings. With
only the pump on this plug now, its design didn't fit:

- It compared against a rolling 24-hour **self-calculated average**, so a pump
  failing slowly would drag its own baseline down and normalize the failure. The
  baseline is now explicit and calibrated, and only changes when you say so.
- Its `power_threshold` check ran `current < threshold` under the subject line
  "Threshold Exceeded", and fired on every poll with no debounce.
- `consecutive_failures` lived on the instance while the script ran once per
  cron invocation, so it reset to 0 every minute and the 3-failure email path
  could never fire. State is now persisted to disk.
- It rewrote the entire history JSON array on every poll; history is now an
  append-only JSONL file.
- It ignored line voltage entirely, despite the plug reporting it.

If you're replacing a cron-driven predecessor, remember to clear its crontab
entry (`crontab -e`) when you enable the service. Two monitors on one plug
means duplicate alerts and two divergent histories.
