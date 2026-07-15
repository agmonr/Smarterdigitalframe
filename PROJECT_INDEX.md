# Project Index: Smarter Digital Frame
- **Purpose**: Manage a digital photo frame — syncs images from Google Photos albums and local folders, runs a slideshow with clock overlay, and adds presence-aware behavior via BLE proximity and a motion-triggered Pi camera (photo burst/video capture, live view).

## Key Files & Roles
- `api.py`: Main Flask app (port 5000-ish, served via nginx) — API endpoints for photos, folders, albums, sync, camera/motion capture, live video/camera feed, screen power, proximity/BLE pairing, terminal exec, and system status.
- `manager.py`: Small Flask app that serves the web UI page shells (dashboard, history, camera, google-photos, live-video, folders, settings, proximity, system, terminal) by rendering `templates/*.html`; the actual data comes from `api.py`.
- `downloader.py`: Logic for scraping/downloading images from Google Photos shared albums.
- `display.py`: Slideshow loop — image selection/rotation, clock overlay (including smooth anti-burn-in animation), framebuffer writes, and screen state coordination with `api.py`.
- `proximity.py`: BLE proximity detection for presence sensing — distance calculation, smart tag filtering, pairing mode.
- `network_server.py`: Network status and Wi-Fi configuration endpoints.
- `terminal_server.py`: Standalone remote terminal interface (separate from `api.py`'s `/api/terminal/run`).
- `wifi_setup.py`: Wi-Fi/access-point configuration helper.
- `common.py`: Shared utilities — config loading, DB connection/history helpers, logging setup, screen on/off + camera schedule logic (`is_scheduled_off`, `is_camera_scheduled_on`), presence-enabled check.
- `logger_setup.py`: Logging configuration helper.
- `templates/`: HTML templates for the web UI — `dashboard.html`, `history.html`, `camera.html`, `google_photos.html`, `live_video.html`, `folders.html`, `settings.html`, `settings_camera.html`, `ble_proximity.html`, `status.html`, `terminal.html`, `image_gallery.html`, `full_screen.html`, `nav.html`, `index.html`, `darkmode.css`.
- `static/`: Static assets (`darkmode.css`, `loading.png`, `502.html`).
- `captures/`: Motion/scheduled camera output, organized into per-date subfolders.
- `albums.json`: Configured Google Photos albums.
- `config.ini` / `config.ini.example`: User settings — `[DEFAULT]` (slideshow, clock, screen schedule, weak_machine), `[MOTION]` (PIR/motion sensitivity, timeout), `[SCHEDULE]` (screen schedule on/off), `[CAMERA]` (capture on/off schedule, video resolution, capture-on-motion mode/burst/duration, retention), `[NETWORK]` (AP name/password), `[PROXIMITY]` (BLE distance thresholds, ignore list).
- `history.db`: SQLite DB of shown-image history (used for anti-repeat/history views).
- `digitalframe.nginx`: nginx site config fronting the Flask apps.
- `install.sh`, `run_frame.sh`, `run_frame_debug.sh`: Setup and process-launch scripts.

## Major Functions & Entry Points
- **api.py**: Core API surface, notably —
  - Photos/folders/albums: `/api/image/<file>`, `/api/download/<file>`, `/api/folders`, `/api/show`, `/api/next`, `/api/next-group`, `/api/prev`, `/api/albums`, `/api/albums/sync`, `/api/albums/update`, `/api/remove`, `/api/history`.
  - Camera & motion capture: `camera_scheduler_thread()` (background loop driving motion-triggered burst/video capture per `[CAMERA]`/`[MOTION]` config), `/api/motion` (GET/POST), `/api/captures` + `/api/captures/<file>` (browse/serve captured stills/video), `/api/capture_video`, `/api/camera_feed`, `/api/video_feed` (MJPEG live view).
  - Proximity/pairing: `/api/proximity/pairing-mode`, `/api/proximity/toggle-ignore`, `_enable_pairing_mode_task()`.
  - System/screen: `/api/screen`, `/api/internal/screen_state`, `/api/state`, `/api/system/status`, `/api/sync/status`, `/api/restart`, `/api/config`.
  - Remote terminal: `/api/terminal/run`.
  - Presence tracking via shared `presence_data` dict (last motion/interaction/proximity timestamps, screen state) feeding the effective "weak machine" and screen-schedule decisions in `common.py`.
- **manager.py**: Routes each web UI page (`/`, `/history`, `/camera`, `/google-photos`, `/live-video`, `/folders`, `/settings/general`, `/settings/proximity`, `/settings/camera`, `/system`, `/terminal`) to its template; no business logic.
- **downloader.py**: Google Photos shared-album scraping and image sync.
- **display.py**: `main()` slideshow loop — image rotation/grouping (`get_next_image_index`, `get_random_image_index`), clock rendering (`draw_time`, `display_hourly_clock`), framebuffer output (`write_to_fb`), and screen-state notification back to `api.py`.
- **proximity.py**: BLE scanner, distance-based presence logic, smart tag filtering, pairing mode support.
- **network_server.py**: API endpoints for network status and Wi-Fi configuration.
- **common.py**: Config/DB access, history helpers (`get_history`, `add_to_history`, `delete_from_history`), screen state (`set_screen_state`, `get_hardware_screen_state`), and the centralized schedule/presence logic (`is_scheduled_off`, `is_camera_scheduled_on`, `is_presence_enabled`) shared by `api.py` and `display.py`.
