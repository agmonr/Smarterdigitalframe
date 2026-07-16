# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

DigitalFrame turns a Raspberry Pi + display into a smart photo frame: a slideshow with a clock overlay, Google Photos album sync, local folder support, motion-triggered camera capture, BLE proximity-based wake/sleep, and a web dashboard for remote control — all tuned to be gentle on SD card I/O and to run headless on low-power Pi hardware (Pi Zero 2 and up).

See `PROJECT_INDEX.md` for a file-by-file map of the codebase (key files, major functions, entry points) — read it first when navigating.

## Running it

There is no build step; this is a plain Python/Flask project driven by systemd in production.

```bash
./install.sh              # sets up venv (via uv), system deps, nginx, and the frame.service systemd unit
./run_frame.sh             # foreground run of all 5 processes, output silenced
./run_frame_debug.sh       # same, but logs each process to logs/<name>.log instead of /dev/null
```

`install.sh` can also bootstrap directly into an empty directory: `curl -fsSL .../install.sh | sudo bash` clones the repo first, then installs. It installs `rfkill` and unblocks Wi-Fi/Bluetooth as part of setup, since BLE proximity and Wi-Fi config depend on both being unblocked.

In production the whole stack runs as one systemd unit, `frame.service` (`WorkingDirectory` = repo root, runs as root because `display.py` writes to `/dev/fb0` and controls HDMI power). Deploying a fix to a live frame means: get the fix onto disk in the service's working directory (the repo has a 24h auto-update thread in `api.py`, or `git pull` manually), then `sudo systemctl restart frame.service` — code changes to files like `display.py` only take effect on restart, since the process holds old code in memory.

```bash
sudo systemctl restart frame.service
systemctl status frame.service --no-pager
journalctl -u frame.service -f
```

There is no test suite in this repo (`pytest`/`pytest-flask` are in `requirements.txt` but no test files exist yet) and no linter is configured — verify changes by exercising the running services (see below), not by running a checker.

## Architecture: five cooperating processes, no shared memory

`run_frame.sh` / `frame.service` launches five independent Python processes that coordinate only through the filesystem — there is no RPC or shared Python state between them:

| Process | Port | Role |
|---|---|---|
| `display.py` | — (writes `/dev/fb0` directly) | The slideshow loop: image rotation, clock overlay, framebuffer writes, screen on/off |
| `api.py` | 5001 | The "brain": REST API for photos/folders/albums/sync, camera & motion capture, proximity/BLE pairing, screen/system state, remote terminal exec |
| `manager.py` | 5002 | Serves the web UI page shells (`templates/*.html`); no business logic, all data comes from `api.py` |
| `network_server.py` | 5003 | Network status and Wi-Fi configuration endpoints |
| `terminal_server.py` | 5004 (socket.io) | Standalone remote terminal (separate from `api.py`'s `/api/terminal/run`) |

nginx (`digitalframe.nginx`) fronts all of them on port 80 and routes by path (`/api/` → 5001, `/network` + `/api/network/` → 5003, `/socket.io/` → 5004, everything else → 5002).

**Cross-process coordination happens via `common.py` and `/dev/shm`.** `common.py` defines shared paths under `SHM_ROOT` (`/dev/shm/smarterdigitalframe`, falling back to disk if SHM isn't writable): `STATE_FILE` (what's currently on screen), `MANUAL_ON_FILE`/`MANUAL_OFF_FILE` (manual screen override flags), and command files `NEXT_IMAGE_FILE`/`PREV_IMAGE_FILE`/`NEXT_GROUP_FILE`/`SHOW_IMAGE_FILE` — `api.py` writes these as tmp-file "signals" when the dashboard requests navigation, and `display.py`'s main loop polls for their existence, consumes them, and deletes them. This indirection exists specifically to keep the render loop decoupled from the request-handling process. All transient/frequently-written state deliberately lives in RAM (`/dev/shm`), not on the SD card — a stated design goal to minimize SD wear (see README's "MicroSD Card & I/O Optimization").

`common.py` also centralizes: config loading/caching (`get_config`, re-reads `config.ini` only when its mtime changes), the SQLite history DB (`history.db`, WAL mode + mmap + `synchronous=OFF` for Pi-friendly performance), screen state get/set (`set_screen_state`, `get_hardware_screen_state` via `vcgencmd`), and the schedule/presence decision logic (`is_scheduled_off`, `is_camera_scheduled_on`, `is_presence_enabled`) that both `api.py` and `display.py` consult so screen-power and camera-schedule rules stay in one place instead of being duplicated per process.

## `display.py`'s main loop

This is the most stateful piece of the codebase and worth understanding before touching it. It's a single `while True` loop (in `main()`) that, each iteration:
1. Throttles config/directory re-checks (every 30s / 5min respectively) and resets caches like `idx` when the image set or selected folders change.
2. Determines desired screen on/off state from a strict priority order: manual OFF > schedule OFF > manual ON > presence detection > default ON.
3. Polls the `common.py` command files (`SHOW_IMAGE_FILE`, `NEXT_GROUP_FILE`, `NEXT_IMAGE_FILE`, `PREV_IMAGE_FILE`) for dashboard-initiated navigation and updates `idx` accordingly.
4. Handles hourly/periodic/scheduled clock overlays (`SHOW_HOURLY`, `SHOW_PERIODIC`, `SHOW_SCHEDULED` + croniter-based schedules), which render a *cached* background (`current_image_obj`) with the time drawn on top so it doesn't have to reload/rescale the image every tick.
5. Falls through to `should_refresh` logic that decides whether to call `display_image()` (rotation interval elapsed, forced refresh after a config/screen change, or just a per-minute clock redraw).

The important invariant: anywhere `idx` changes, any cached rendering keyed off the old `idx` (like the clock overlay's `current_image_obj`) must be invalidated too (tracked via `last_clock_idx`) — otherwise a stale cached frame gets redrawn over a freshly displayed image on the next tick, which looks like the frame "reverting" after a navigation action. This was a real bug; watch for the same class of staleness when adding new cached state to the loop.

The clock overlay also does small time-based animation (harmonic-motion offset in `draw_time`) specifically to avoid OLED/IPS burn-in from a static clock position — that jitter is intentional, not a bug.

## Config

`config.ini` (gitignored; generated from `config.ini.example` by `install.sh`) drives nearly all runtime behavior via `configparser` sections: `[DEFAULT]` (image dir, interval, group size, clock display/format/position, screen schedule, `weak_machine` perf mode), `[MOTION]`, `[SCHEDULE]`, `[CAMERA]`, `[NETWORK]`, `[PROXIMITY]`. `weak_machine` also accepts `auto`, handled specially since `configparser.getboolean` would reject it.
