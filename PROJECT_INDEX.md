# Project Index: Smarter Digital Frame
- **Purpose**: Manage a digital photo frame, syncing images from Google Photos albums and local folders.

## Key Files & Roles
- `api.py`: Main Flask application, handles API endpoints for photos, folders, albums, and system control.
- `manager.py`: Process manager/orchestrator for the application components.
- `downloader.py`: Logic for scraping/downloading images from Google Photos shared albums, including throttling and storage management.
- `display.py`: Manages slideshow logic, drawing images/clocks to the framebuffer.
- `proximity.py`: Manages BLE proximity detection for presence sensing.
- `network_server.py`: Manages network-related functionality.
- `terminal_server.py`: Provides a remote terminal interface.
- `wifi_setup.py`: Handles Wi-Fi configuration.
- `common.py`: Shared utilities, config loading, DB connection, logging, and hardware control.
- `templates/`: HTML templates for the web interface.
- `albums.json`: Configured Google Photos albums (contains metadata about enabled/disabled state).
- `config.ini`: User settings (includes selected folders).

## Major Functions & Entry Points
- **api.py**: Handles API endpoints, including photo management, sync triggers, and configuration updates.
- **manager.py**: Orchestrates the main application flow.
- **downloader.py**: Core functionality for Google Photos image scraping and synchronization.
- **display.py**: Handles the slideshow, clock overlay, and screen power scheduling.
- **proximity.py**: Implements the BLE proximity scanner and device registry.
- **network_server.py**: API endpoints for network status and Wi-Fi configuration.
- **common.py**: Shared utilities for configuration, logging, and hardware access.
