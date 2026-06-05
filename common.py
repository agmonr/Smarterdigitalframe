import os
import configparser
import json
import subprocess
import time
import logging

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_ROOT, 'config.ini')
CONFIG_EXAMPLE = os.path.join(PROJECT_ROOT, 'config.ini.example')
STATE_FILE = os.path.join(PROJECT_ROOT, 'state.json')
HISTORY_FILE = os.path.join(PROJECT_ROOT, 'history.json')
REMOVE_DIR = os.path.join(PROJECT_ROOT, 'removed/')
LOG_DIR = os.path.join(PROJECT_ROOT, 'logs/')

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

if not os.path.exists(REMOVE_DIR):
    os.makedirs(REMOVE_DIR)

def get_config():
    config = configparser.ConfigParser(interpolation=None)
    # Load defaults from example file first
    if os.path.exists(CONFIG_EXAMPLE):
        config.read(CONFIG_EXAMPLE)
    # Overlay with actual config
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
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

def get_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    return []

def save_history(history):
    try:
        # Limit history size to 1000 entries
        if len(history) > 1000:
            history = history[-1000:]
        with open(HISTORY_FILE, 'w') as f:
            json.dump(history, f)
    except Exception as e:
        print(f"Error saving history: {e}")

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
