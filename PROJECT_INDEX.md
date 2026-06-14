# Project Index: Smarter Digital Frame
- **Purpose**: Manage a digital photo frame, syncing images from Google Photos albums and local folders.

## Key Files & Roles
- `api.py`: Main Flask application, handles API endpoints for photos, folders, albums, and system control, including presence detection and Bluetooth pairing.
- `manager.py`: Process manager/orchestrator for the application components.
- `downloader.py`: Logic for scraping/downloading images from Google Photos shared albums.
- `display.py`: Manages slideshow logic, clock overlay, and dual-range screen power scheduling.
- `proximity.py`: Manages BLE proximity detection for presence sensing, including distance calculation and smart tag filtering.
- `network_server.py`: Manages network-related functionality.
- `terminal_server.py`: Provides a remote terminal interface.
- `wifi_setup.py`: Handles Wi-Fi configuration.
- `common.py`: Shared utilities, config loading, DB connection, logging, screen schedule logic, and presence state helpers.
- `templates/`: HTML templates for the web interface (including `ble_proximity.html` for dedicated proximity management).
- `albums.json`: Configured Google Photos albums.
- `config.ini`: User settings (including dual screen schedules and distance thresholds).

## Major Functions & Entry Points
- **api.py**: Handles API endpoints, including photo management, sync triggers, configuration updates, and proximity/pairing control.
- **manager.py**: Orchestrates the main application flow.
- **downloader.py**: Core functionality for Google Photos image scraping and synchronization.
- **display.py**: Handles the slideshow, clock overlay, and dual-range screen power management.
- **proximity.py**: Implements the BLE proximity scanner, distance-based presence logic, smart tag filtering, and pairing mode.
- **network_server.py**: API endpoints for network status and Wi-Fi configuration.
- **common.py**: Shared utilities for configuration, logging, hardware access, and centralized schedule/presence logic.
