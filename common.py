import os
import configparser
import json
import subprocess
import time
import logging
import sqlite3

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

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    # Optimizations for SD Cards: Minimize writes and use RAM for temporary data
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = OFF') # Maximum performance, slight risk on power loss
    conn.execute('PRAGMA cache_size = -5000') # ~5MB cache
    conn.execute('PRAGMA temp_store = MEMORY')
    conn.execute('PRAGMA mmap_size = 30000000') # Use memory mapping for faster reads
    return conn

def init_db():
    """Initialize SQLite database and migrate existing history.json if it exists."""
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
    global _config_cache, _config_mtime
    
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
    """Unified screen control using both vcgencmd and blanking."""
    val_v = "1" if on else "0"
    val_b = "0" if on else "1"
    try:
        # Try vcgencmd (HDMI)
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

def get_images(image_dir=None):
    if image_dir is None:
        config = get_config()
        image_dir = config.get('DEFAULT', 'imagedir', fallback=os.path.join(PROJECT_ROOT, 'images/'))
    
    valid_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp')
    if not os.path.exists(image_dir):
        return []
        
    images = []
    for root, dirs, files in os.walk(image_dir):
        # Skip removed directory if it's inside image_dir
        if 'removed' in root:
            continue
        for f in files:
            if f.lower().endswith(valid_extensions):
                # If we are in a subdirectory, include it in the name relative to image_dir
                rel_path = os.path.relpath(os.path.join(root, f), image_dir)
                images.append(rel_path)
    return images
