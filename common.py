import os
import configparser
import json
import subprocess
import time
import logging
import sqlite3
import signal

def notify_display_process():
    """Send SIGUSR1 to the display process to wake it up."""
    state = get_state()
    pid = state.get('pid')
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
            return True
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"Error notifying display process {pid}: {e}")
    return False

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_ROOT, 'config.ini')
CONFIG_EXAMPLE = os.path.join(PROJECT_ROOT, 'config.ini.example')

# Use /dev/shm (RAM) for transient state files to minimize SD card writes
SHM_ROOT = '/dev/shm/smarterdigitalframe'
if not os.path.exists(SHM_ROOT) and os.path.exists('/dev/shm'):
    try: os.makedirs(SHM_ROOT, exist_ok=True)
    except: SHM_ROOT = PROJECT_ROOT # Fallback to disk if SHM is not writable
else:
    if not os.path.exists('/dev/shm'): SHM_ROOT = PROJECT_ROOT

STATE_FILE = os.path.join(SHM_ROOT, 'state.json')
SYNC_STATUS_FILE = os.path.join(SHM_ROOT, 'sync_status.json')
ALBUM_STATUS_FILE = os.path.join(SHM_ROOT, 'album_status.json')
MANUAL_ON_FILE = os.path.join(SHM_ROOT, 'manual_on.tmp')
MANUAL_OFF_FILE = os.path.join(SHM_ROOT, 'manual_off.tmp')
NEXT_IMAGE_FILE = os.path.join(SHM_ROOT, 'next_image.tmp')
PREV_IMAGE_FILE = os.path.join(SHM_ROOT, 'prev_image.tmp')
NEXT_GROUP_FILE = os.path.join(SHM_ROOT, 'next_group.tmp')
SHOW_IMAGE_FILE = os.path.join(SHM_ROOT, 'show_image.tmp')

HISTORY_FILE = os.path.join(PROJECT_ROOT, 'history.json')
DB_FILE = os.path.join(PROJECT_ROOT, 'history.db')
REMOVE_DIR = os.path.join(PROJECT_ROOT, 'removed/')
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs/')

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

if not os.path.exists(REMOVE_DIR):
    os.makedirs(REMOVE_DIR)

# Cache for config to minimize reads
_config_cache = None
_config_mtime = 0
_config_last_check = 0

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # Optimizations for SD Cards: Minimize writes and use RAM for temporary data
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL') # Better balance for SD cards than OFF
    conn.execute('PRAGMA cache_size = -10000') # ~10MB cache (increased)
    conn.execute('PRAGMA temp_store = MEMORY')
    conn.execute('PRAGMA mmap_size = 30000000') # Use memory mapping for faster reads
    conn.execute('PRAGMA page_size = 4096') # Standard SD card page size
    return conn

# Flag to ensure DB is initialized only once per process
_db_initialized = False

def init_db():
    """Initialize SQLite database and migrate existing history.json if it exists."""
    global _db_initialized
    if _db_initialized:
        return
    
    # Always check if table exists, even if DB file exists
    conn = get_db_connection()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_history_name ON history(name)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history(timestamp)')
        conn.commit()
        
        # Migration logic
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, 'r') as f:
                    history = json.load(f)
                if isinstance(history, list):
                    for entry in history:
                        if isinstance(entry, dict):
                            conn.execute(
                                'INSERT INTO history (name, path, timestamp) VALUES (?, ?, ?)',
                                (entry.get('name'), entry.get('path'), entry.get('timestamp'))
                            )
                    conn.commit()
                # Rename history.json after successful migration
                os.rename(HISTORY_FILE, HISTORY_FILE + '.bak')
            except Exception as e:
                print(f"Error migrating history: {e}")
        
        # Cleanup old entries on init
        try:
            config = get_config()
            retention_days = config.getint('DEFAULT', 'history_retention_days', fallback=30)
            conn.execute(
                "DELETE FROM history WHERE datetime(timestamp) < datetime('now', '-' || ? || ' days')",
                (retention_days,)
            )
            conn.commit()
        except:
            pass
        
        _db_initialized = True
    finally:
        conn.close()

def get_history(limit=1000):
    """Retrieve history from SQLite, newest first."""
    init_db()
    try:
        conn = get_db_connection()
        cursor = conn.execute('SELECT name, path, timestamp FROM history ORDER BY id DESC LIMIT ?', (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"Error getting history: {e}")
        return []

def add_to_history(name, path, timestamp):
    """Add a new entry to history and maintain limits."""
    init_db()
    try:
        config = get_config()
        retention_days = config.getint('DEFAULT', 'history_retention_days', fallback=30)
        
        conn = get_db_connection()
        conn.execute(
            'INSERT INTO history (name, path, timestamp) VALUES (?, ?, ?)',
            (name, path, timestamp)
        )
        
        # 1. Cleanup by days
        conn.execute(
            "DELETE FROM history WHERE datetime(timestamp) < datetime('now', '-' || ? || ' days')",
            (retention_days,)
        )
        
        # 2. Safety cap: Keep only the latest 10000 entries regardless of days
        conn.execute('''
            DELETE FROM history WHERE id NOT IN (
                SELECT id FROM history ORDER BY id DESC LIMIT 10000
            )
        ''')
        
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error adding to history: {e}")

def delete_from_history(name):
    """Delete all entries with a specific filename from history."""
    init_db()
    try:
        conn = get_db_connection()
        conn.execute('DELETE FROM history WHERE name = ?', (name,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error deleting from history: {e}")

def get_recent_paths(days=1):
    """Retrieve only relative paths of images shown in the last N days (SQL-native)."""
    init_db()
    try:
        conn = get_db_connection()
        # Use SQLite's native datetime filtering for maximum performance
        if days is None:
            cursor = conn.execute("SELECT DISTINCT path FROM history")
        else:
            cursor = conn.execute(
                "SELECT DISTINCT path FROM history WHERE datetime(timestamp) > datetime('now', '-' || ? || ' days')",
                (days,)
            )
        paths = [row['path'] for row in cursor.fetchall()]
        conn.close()
        return set(paths) # Use set for O(1) lookup performance
    except Exception as e:
        print(f"Error getting recent paths: {e}")
        return set()

def get_config():
    global _config_cache, _config_mtime, _config_last_check
    
    now = time.time()
    # Only check file system if it's been more than 30 seconds since last check
    if _config_cache is not None and (now - _config_last_check < 30):
        return _config_cache

    _config_last_check = now
    
    # Check if config file was modified since last load
    current_mtime = 0
    if os.path.exists(CONFIG_FILE):
        current_mtime = os.path.getmtime(CONFIG_FILE)
    
    if _config_cache is not None and current_mtime <= _config_mtime:
        return _config_cache

    config = configparser.ConfigParser(interpolation=None)
    # Load defaults from example file first
    if os.path.exists(CONFIG_EXAMPLE):
        config.read(CONFIG_EXAMPLE)
    # Overlay with actual config
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
    
    _config_cache = config
    _config_mtime = current_mtime
    return config

def setup_logger(name, filename='digitalframe.log', level=None):
    if level is None:
        config = get_config()
        level_str = config.get('DEFAULT', 'loglevel', fallback='INFO').upper()
        level = getattr(logging, level_str, logging.INFO)
    
    log_path = os.path.join(LOG_DIR, filename)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Avoid duplicate handlers
    if not logger.handlers:
        handler = logging.FileHandler(log_path)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        
        # Also add console handler
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)
        
    return logger

def set_screen_state(on):
    """Unified screen control using DRM, vcgencmd, blanking, and setterm."""
    val_v = "1" if on else "0"
    val_b = "0" if on else "1"
    
    # Try DRM DPMS (most reliable on modern Pi OS with KMS)
    try:
        drm_path = "/sys/class/drm/"
        if os.path.exists(drm_path):
            for card in os.listdir(drm_path):
                if "HDMI-A" in card:
                    dpms_file = os.path.join(drm_path, card, "dpms")
                    if os.path.exists(dpms_file):
                        with open(dpms_file, "w") as f:
                            f.write("On" if on else "Off")
    except:
        pass

    try:
        # Try vcgencmd (HDMI legacy)
        subprocess.run(['vcgencmd', 'display_power', val_v], check=False, capture_output=True)
    except:
        pass
    try:
        # Try framebuffer blanking
        if os.path.exists("/sys/class/graphics/fb0/blank"):
            with open("/sys/class/graphics/fb0/blank", "w") as f:
                f.write(val_b)
    except:
        pass
    try:
        # Try setterm blanking
        val_s = "force" if not on else "poke"
        subprocess.run(['setterm', '--blank', val_s], check=False, capture_output=True, env=os.environ)
    except:
        pass

def get_hardware_screen_state():
    """Read actual screen state from hardware sources."""
    # 1. Try DRM DPMS state (most reliable on modern Pi OS with KMS)
    try:
        # Look for connected HDMI ports
        drm_path = "/sys/class/drm/"
        if os.path.exists(drm_path):
            for card in os.listdir(drm_path):
                if "HDMI-A" in card:
                    status_file = os.path.join(drm_path, card, "status")
                    if os.path.exists(status_file):
                        with open(status_file, "r") as f:
                            if f.read().strip() != "connected":
                                continue
                    
                    dpms_file = os.path.join(drm_path, card, "dpms")
                    if os.path.exists(dpms_file):
                        with open(dpms_file, "r") as f:
                            val = f.read().strip().lower()
                            if val == "off":
                                return 'off'
                            elif val == "on":
                                return 'on'
    except:
        pass

    # 2. Try framebuffer blanking fallback
    try:
        if os.path.exists("/sys/class/graphics/fb0/blank"):
            with open("/sys/class/graphics/fb0/blank", "r") as f:
                val = f.read().strip()
                if val != "0" and val != "": 
                    return 'off'
                return 'on'
    except:
        pass
    
    return 'unknown'

def get_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Error saving state: {e}")

def get_image_dir():
    config = get_config()
    return config.get('DEFAULT', 'imagedir', fallback=os.path.join(PROJECT_ROOT, 'images/'))

# Cache for image list
_image_cache = None
_image_cache_time = 0

def get_images(image_dir=None):
    global _image_cache, _image_cache_time
    if image_dir is None:
        image_dir = get_image_dir()
    
    now = time.time()
    # Cache image list for 60 seconds (increased from 30)
    if _image_cache is not None and (now - _image_cache_time < 60):
        return _image_cache

    valid_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp')
    if not os.path.exists(image_dir):
        return []
        
    images = []
    
    def _fast_scan(path):
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_file():
                        if entry.name.lower().endswith(valid_extensions):
                            rel_path = os.path.relpath(entry.path, image_dir)
                            images.append(rel_path)
                    elif entry.is_dir():
                        if entry.name != 'removed':
                            _fast_scan(entry.path)
        except Exception:
            pass

    _fast_scan(image_dir)
    
    _image_cache = images
    _image_cache_time = now
    return images

def is_time_in_range(now_h, now_m, start_h, start_m, end_h, end_m):
    now_total = now_h * 60 + now_m
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    
    if start_total == end_total: return False
    
    if start_total < end_total:
        return start_total <= now_total < end_total
    else:
        # Wrapped range (e.g., 22:30 to 07:15)
        return now_total >= start_total or now_total < end_total

def is_scheduled_off():
    """Checks if the screen should be OFF based on the primary and secondary schedules."""
    from datetime import datetime
    config = get_config()
    schedule_enabled = config.getboolean('SCHEDULE', 'enabled', fallback=False)
    if not schedule_enabled:
        return False
        
    now = datetime.now()
    now_h, now_m = now.hour, now.minute
    
    # Primary Schedule
    off_h1 = config.getint('DEFAULT', 'screenoffhour', fallback=22)
    off_m1 = config.getint('DEFAULT', 'screenoffmin', fallback=0)
    on_h1 = config.getint('DEFAULT', 'screenonhour', fallback=7)
    on_m1 = config.getint('DEFAULT', 'screenonmin', fallback=0)
    if is_time_in_range(now_h, now_m, off_h1, off_m1, on_h1, on_m1):
        return True
        
    # Secondary Schedule
    off_h2 = config.getint('DEFAULT', 'screenoffhour2', fallback=0)
    off_m2 = config.getint('DEFAULT', 'screenoffmin2', fallback=0)
    on_h2 = config.getint('DEFAULT', 'screenonhour2', fallback=0)
    on_m2 = config.getint('DEFAULT', 'screenonmin2', fallback=0)
    
    if (off_h2 != on_h2 or off_m2 != on_m2) and is_time_in_range(now_h, now_m, off_h2, off_m2, on_h2, on_m2):
        return True
        
    return False

def is_camera_scheduled_on():
    """Checks if the camera should be ON based on its own primary and secondary schedules."""
    from datetime import datetime
    config = get_config()
    
    now = datetime.now()
    now_h, now_m = now.hour, now.minute
    
    # Primary Schedule
    on_h1 = config.getint('CAMERA', 'on_hour', fallback=0)
    on_m1 = config.getint('CAMERA', 'on_min', fallback=0)
    off_h1 = config.getint('CAMERA', 'off_hour', fallback=0)
    off_m1 = config.getint('CAMERA', 'off_min', fallback=0)
    
    if (on_h1 != off_h1 or on_m1 != off_m1) and is_time_in_range(now_h, now_m, on_h1, on_m1, off_h1, off_m1):
        return True
        
    # Secondary Schedule
    on_h2 = config.getint('CAMERA', 'on_hour2', fallback=0)
    on_m2 = config.getint('CAMERA', 'on_min2', fallback=0)
    off_h2 = config.getint('CAMERA', 'off_hour2', fallback=0)
    off_m2 = config.getint('CAMERA', 'off_min2', fallback=0)
    
    if (on_h2 != off_h2 or on_m2 != off_m2) and is_time_in_range(now_h, now_m, on_h2, on_m2, off_h2, off_m2):
        return True
        
    # If both are 0-0, it's always on (special case for camera)
    if on_h1 == off_h1 and on_m1 == off_m1 and on_h2 == off_h2 and on_m2 == off_m2:
        return True

    return False

def is_presence_enabled():
    """Returns True if either Motion or Proximity detection is enabled."""
    config = get_config()
    motion_enabled = config.getboolean('MOTION', 'enabled', fallback=False)
    proximity_enabled = config.getboolean('PROXIMITY', 'enabled', fallback=False)
    return motion_enabled or proximity_enabled
