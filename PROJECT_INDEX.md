# Project Index: Smarter Digital Frame
- **Purpose**: Manage a digital photo frame, syncing images from Google Photos albums and local folders.

## Key Files & Roles
- `api.py`: Main Flask application, handles API endpoints for photos, folders, albums, and system control.
- `downloader.py`: Logic for scraping/downloading images from Google Photos shared albums, including throttling and storage management.
- `display.py`: Manages slideshow logic, drawing images/clocks to the framebuffer.
- `common.py`: Shared utilities, config loading, DB connection, logging, and hardware control.
- `templates/`: HTML templates for the web interface.
- `albums.json`: Configured Google Photos albums (contains metadata about enabled/disabled state).
- `config.ini`: User settings (includes selected folders).

## Major Functions & Entry Points
- **api.py**:
    - `get_folders()`: API endpoint to list local folders (filters out `google_photos`).
    - `add_album_api()`: API endpoint to add new Google Photos albums.
    - `get_albums_api()`: API endpoint to list configured Google Photos albums.
    - `google_photos_sync_thread()`: Background thread running the auto-sync pipelining logic.
- **downloader.py**:
    - `download_album(album_id, url, output_dir, force_fast)`: Core function to scrape/download images.
    - `sync_all(force_fast)`: Triggers sync for all configured albums.
- **display.py**:
    - `get_images()`: Collects list of all images available for the slideshow.
- **common.py**:
    - `get_config()`: Loads and caches application configuration.
    - `get_image_dir()`: Resolves the base image directory.
