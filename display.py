import os
import time
import random
import signal
import sys
import configparser
import logging
import json
import tempfile
import subprocess
import urllib.request
from datetime import datetime
from croniter import croniter
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageOps
import math
import threading
import gc
import common

# Setup Logging using common library
logger = common.setup_logger(__name__)

# Wake event for responsive sleep
wake_event = threading.Event()

get_config = common.get_config

# Configuration loading
def load_config_values():
    global IMAGE_DIR, INTERVAL, GROUP_SIZE, FB_DEV, COLOR_ORDER, LOG_LEVEL_STR, LOG_FILE
    global SHOW_TIME, TIME_FORMAT, TIME_FONT_SIZE, TIME_LOCATION, TIME_COLOR, TIME_BORDER_COLOR, TIME_BORDER_SIZE, NEG_TIME, TIME_ALPHA
    global SHOW_HOURLY, SHOW_PERIODIC, SHOW_SCHEDULED, CLOCK_SCHEDULE_1, CLOCK_SCHEDULE_2, SCREEN_OFF_HOUR, SCREEN_ON_HOUR, SCREEN_OFF_HOUR_2, SCREEN_ON_HOUR_2, SELECTED_FOLDERS, WEAK_MACHINE

    config = common.get_config()
    PROJECT_ROOT = common.PROJECT_ROOT
    
    IMAGE_DIR = common.get_image_dir()
    SELECTED_FOLDERS = config.get('DEFAULT', 'selected_folders', fallback='all')
    
    # Handle 'auto' for weak_machine which getboolean would reject
    wm_val = config.get('DEFAULT', 'weak_machine', fallback='false').lower()
    if wm_val == 'auto':
        # In 'auto' mode, display.py will use its own check for user activity or motion
        # For simplicity in this process, we'll check if any recent interaction happened via the API
        # But as a fallback/default for 'auto', we'll treat it as False (high perf) 
        # unless we want to implement the same 5-min logic here.
        # For now, let's just make sure it doesn't CRASH.
        WEAK_MACHINE = False 
    else:
        WEAK_MACHINE = config.getboolean('DEFAULT', 'weak_machine', fallback=False)

    INTERVAL = config.getint('DEFAULT', 'interval', fallback=10)
    if WEAK_MACHINE:
        INTERVAL = max(INTERVAL, 30)
    
    GROUP_SIZE = config.getint('DEFAULT', 'groupsize', fallback=10)
    FB_DEV = config.get('DEFAULT', 'framebufferdevice', fallback='/dev/fb0')
    COLOR_ORDER = config.get('DEFAULT', 'colororder', fallback='BGR').upper()
    LOG_LEVEL_STR = config.get('DEFAULT', 'loglevel', fallback='INFO').upper()
    LOG_FILE = config.get('DEFAULT', 'logfile', fallback='logs/digitalframe.log')
    
    # Apply log level
    numeric_level = getattr(logging, LOG_LEVEL_STR, logging.INFO)
    logger.setLevel(numeric_level)
    
    SHOW_TIME = config.getboolean('DEFAULT', 'showtime', fallback=True)
    SHOW_PERIODIC = config.getboolean('DEFAULT', 'showperiodicclock', fallback=False)
    SHOW_SCHEDULED = config.getboolean('DEFAULT', 'showscheduledclock', fallback=False)
    CLOCK_SCHEDULE_1 = config.get('DEFAULT', 'clockschedule1', fallback='0 * * * *')
    CLOCK_SCHEDULE_2 = config.get('DEFAULT', 'clockschedule2', fallback='30 * * * *')
    TIME_FORMAT = config.get('DEFAULT', 'timeformat', raw=True, fallback='%H:%M')
    TIME_FONT_SIZE = config.getint('DEFAULT', 'timefontsize', fallback=48)
    TIME_LOCATION = config.get('DEFAULT', 'timelocation', fallback='top-left').lower()
    TIME_COLOR = config.get('DEFAULT', 'timecolor', fallback='yellow')
    TIME_BORDER_COLOR = config.get('DEFAULT', 'timebordercolor', fallback='black')
    TIME_BORDER_SIZE = config.getint('DEFAULT', 'timebordersize', fallback=2)
    NEG_TIME = config.getboolean('DEFAULT', 'negativetime', fallback=False)
    TIME_ALPHA = config.getint('DEFAULT', 'timealpha', fallback=255)

    SHOW_HOURLY = config.getboolean('DEFAULT', 'showhourlytime', fallback=True)

    SCREEN_OFF_HOUR = config.getint('DEFAULT', 'screenoffhour', fallback=22)
    SCREEN_ON_HOUR = config.getint('DEFAULT', 'screenonhour', fallback=7)
    SCREEN_OFF_HOUR_2 = config.getint('DEFAULT', 'screenoffhour2', fallback=0)
    SCREEN_ON_HOUR_2 = config.getint('DEFAULT', 'screenonhour2', fallback=0)
    
    return os.path.getmtime(common.CONFIG_FILE) if os.path.exists(common.CONFIG_FILE) else 0

last_config_mtime = load_config_values()

# Cache for state updates to prevent excessive disk writes
_last_state_data = None
_last_state_time = 0

# Use shared objects from common.py
PROJECT_ROOT = common.PROJECT_ROOT
CONFIG_FILE = common.CONFIG_FILE
STATE_FILE = common.STATE_FILE
MANUAL_ON_FILE = common.MANUAL_ON_FILE
MANUAL_OFF_FILE = common.MANUAL_OFF_FILE
NEXT_IMAGE_FILE = common.NEXT_IMAGE_FILE
PREV_IMAGE_FILE = common.PREV_IMAGE_FILE
NEXT_GROUP_FILE = common.NEXT_GROUP_FILE
SHOW_IMAGE_FILE = common.SHOW_IMAGE_FILE

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WIDTH = 0
HEIGHT = 0
BPP = 0
STRIDE = 0

def set_screen_state(on):
    """Unified screen control using common library and notification."""
    common.set_screen_state(on)
    # Notify API of state change
    notify_api_screen_state(on)

def notify_api_screen_state(on):
    """Tell the API the screen was turned on/off so the dashboard stays in sync."""
    state = "on" if on else "off"
    try:
        data = json.dumps({"state": state}).encode('utf-8')
        req = urllib.request.Request("http://localhost:5001/api/internal/screen_state", data=data, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=2) as f:
            pass
    except:
        pass

def update_state(type="image", img_path=None):
    global _last_state_data, _last_state_time
    try:
        # Determine current image display name (relative path if possible)
        current_img = "Idle"
        if img_path:
            try:
                # Use relative path from IMAGE_DIR so API can find it exactly
                current_img = os.path.relpath(img_path, IMAGE_DIR)
            except:
                current_img = os.path.basename(img_path)
        elif type == "clock":
            current_img = "Clock"

        state_data = {
            "type": type,
            "current_image": current_img,
            "full_path": img_path if img_path else "",
            "pid": os.getpid()
        }
        
        # Throttle writes: Only write if state changed OR if 10 seconds passed (heartbeat)
        current_time = time.time()
        if state_data == _last_state_data and (current_time - _last_state_time) < 10:
            return

        state = state_data.copy()
        state["last_update"] = datetime.now().isoformat()
        
        # Writing directly to SHM (RAM) is already fast, no need for mkstemp
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
        
        _last_state_data = state_data
        _last_state_time = current_time
    except Exception as e:
        logger.error(f"Error updating state file: {e}")

def update_history(img_path, save=True):
    if not save:
        return
    try:
        now = datetime.now()
        rel_path = os.path.basename(img_path)
        try:
            rel_path = os.path.relpath(img_path, IMAGE_DIR)
        except:
            pass
            
        common.add_to_history(os.path.basename(img_path), rel_path, now.isoformat())
    except Exception as e:
        logger.error(f"Error updating history: {e}")

def set_cursor(visible):
    """Hide/Show cursor using ANSI and system-level commands."""
    state = "on" if visible else "off"
    try:
        # 1. ANSI escape sequence
        sys.stdout.write("\033[?25h" if visible else "\033[?25l")
        sys.stdout.flush()
        
        # 2. setterm command for the TTY
        subprocess.run(['setterm', '--cursor', state], check=False, capture_output=True)
        
        # 3. Disable framebuffer cursor blinking if possible
        blink_path = "/sys/class/graphics/fbcon/cursor_blink"
        if os.path.exists(blink_path):
            with open(blink_path, "w") as f:
                f.write("1" if visible else "0")
    except:
        pass

def clear_console():
    """Clear console and reset position."""
    try:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
        subprocess.run(['clear'], check=False)
    except:
        pass

def load_fb_info():
    global WIDTH, HEIGHT, BPP, STRIDE
    try:
        fb_path = "/sys/class/graphics/fb0/"
        with open(os.path.join(fb_path, "virtual_size"), "r") as f:
            WIDTH, HEIGHT = map(int, f.read().strip().split(','))
        with open(os.path.join(fb_path, "bits_per_pixel"), "r") as f:
            BPP = int(f.read().strip())
        with open(os.path.join(fb_path, "stride"), "r") as f:
            STRIDE = int(f.read().strip())
        logger.info(f"Framebuffer Info: {WIDTH}x{HEIGHT}, {BPP}bpp, stride {STRIDE}")
    except Exception as e:
        logger.error(f"Error reading framebuffer info: {e}")
        sys.exit(1)

def is_ntp_synchronized():
    try:
        # Check if NTP service is active and synchronized
        # timedatectl output format varies slightly, look for 'System clock synchronized: yes'
        result = subprocess.run(['timedatectl', 'show', '--property=NTPSynchronized', '--value'], capture_output=True, text=True)
        return result.stdout.strip() == 'yes'
    except Exception as e:
        logger.error(f"Error checking NTP status: {e}")
        return False

def draw_styled_text(draw, text, font, position, text_color, border_color, border_size, alpha=255):
    x, y = position
    
    # Convert colors to RGBA if they are not already
    def to_rgba(color, a):
        if isinstance(color, str):
            # If it's a string, we need to convert it to a tuple first
            from PIL import ImageColor
            rgb = ImageColor.getrgb(color)
            return (*rgb, a)
        elif isinstance(color, (list, tuple)):
            return (*color[:3], a)
        return (color, color, color, a)

    txt_rgba = to_rgba(text_color, alpha)
    brd_rgba = to_rgba(border_color, alpha)

    if border_size > 0:
        for dx in range(-border_size, border_size + 1):
            for dy in range(-border_size, border_size + 1):
                if dx != 0 or dy != 0:
                    draw.text((x + dx, y + dy), text, font=font, fill=brd_rgba)
    draw.text((x, y), text, font=font, fill=txt_rgba)

def draw_time(image, format_str, font_size, location=None, color=None, border_color=None, border_size=None, alpha=None):
    if not is_ntp_synchronized():
        return # Time is not synced, skip drawing

    try:
        loc = location or TIME_LOCATION
        txt_col = color or TIME_COLOR
        brd_col = border_color or TIME_BORDER_COLOR
        brd_sz = border_size if border_size is not None else TIME_BORDER_SIZE
        alpha_val = alpha if alpha is not None else TIME_ALPHA
        
        font = ImageFont.truetype(FONT_PATH, font_size)
        text = datetime.now().strftime(format_str)
        
        # Create a drawing context for the main image (must be RGB or RGBA)
        if alpha_val < 255:
            # For transparency, we draw on a separate layer and then composite
            txt_layer = Image.new('RGBA', image.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(txt_layer)
        else:
            draw = ImageDraw.Draw(image)
            
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        padding = 40
        
        # Base position
        if loc == "center": x, y = (WIDTH - tw) // 2, (HEIGHT - th) // 2
        elif loc == "top-left": x, y = padding, padding
        elif loc == "top-right": x, y = WIDTH - tw - padding, padding
        elif loc == "bottom-left": x, y = padding, HEIGHT - th - padding
        else: x, y = WIDTH - tw - padding, HEIGHT - th - padding

        # Smooth anti-burn-in movement using harmonic motion
        t = time.time()
        Rx, Ry = 20, 20 # Offset radius
        omega = 0.2    # Frequency
        dx = int(Rx * math.cos(omega * t))
        dy = int(Ry * math.sin(omega * t))
        x += dx
        y += dy

        if NEG_TIME:
            mask = Image.new('L', image.size, 0)
            mask_draw = ImageDraw.Draw(mask)
            draw_styled_text(mask_draw, text, font, (x, y), 255, 255, brd_sz)
            inverted = ImageChops.invert(image.convert('RGB'))
            image.paste(inverted, (0, 0), mask)
        else:
            if alpha_val < 255:
                draw_styled_text(draw, text, font, (x, y), txt_col, brd_col, brd_sz, alpha=alpha_val)
                image.paste(txt_layer, (0, 0), txt_layer)
            else:
                draw_styled_text(draw, text, font, (x, y), txt_col, brd_col, brd_sz)
    except Exception as e:
        logger.error(f"Error drawing time: {e}")

def write_to_fb(fb, bg):
    data = np.array(bg)
    if BPP == 32:
        fb_data = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
        if COLOR_ORDER == 'BGR':
            fb_data[:, :, 0] = data[:, :, 2]
            fb_data[:, :, 1] = data[:, :, 1]
            fb_data[:, :, 2] = data[:, :, 0]
        else:
            fb_data[:, :, 0:3] = data
        fb_data[:, :, 3] = 255
        fb.seek(0); fb.write(fb_data.tobytes())
    elif BPP == 24:
        fb_data = data[:, :, ::-1] if COLOR_ORDER == 'BGR' else data
        fb.seek(0); fb.write(fb_data.tobytes())
    elif BPP == 16:
        if COLOR_ORDER == 'BGR':
            b, g, r = data[:, :, 0].astype(np.uint16), data[:, :, 1].astype(np.uint16), data[:, :, 2].astype(np.uint16)
        else:
            r, g, b = data[:, :, 0].astype(np.uint16), data[:, :, 1].astype(np.uint16), data[:, :, 2].astype(np.uint16)
        fb_data = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        fb.seek(0); fb.write(fb_data.tobytes())
    else:
        fb.seek(0); fb.write(data.tobytes())
    fb.flush()

def display_image(fb, img_path, save=True):
    try:
        # Load and handle orientation with minimum I/O
        with Image.open(img_path) as img:
            img = ImageOps.exif_transpose(img)
            img_width, img_height = img.size
            
            # Use faster scaling if images are significantly larger than screen
            scale = min(WIDTH / img_width, HEIGHT / img_height)
            new_width, new_height = int(img_width * scale), int(img_height * scale)
            
            # Memory-efficient scaling: if we're downscaling a lot (>2x), do a fast pre-scale
            if scale < 0.5:
                # Fast pre-scale to 2x the target size
                img = img.resize((new_width * 2, new_height * 2), Image.Resampling.NEAREST)
            
            # Final high-quality resize
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            bg = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
            bg.paste(img, ((WIDTH - new_width) // 2, (HEIGHT - new_height) // 2))
            
            # Drawing time directly on the resized image is slightly more memory efficient
            if SHOW_TIME:
                draw_time(bg, TIME_FORMAT, TIME_FONT_SIZE)
                
            write_to_fb(fb, bg)
            update_state("image", img_path)
            update_history(img_path, save=save)
            
            # Explicit cleanup for large image objects
            del img
            del bg
            gc.collect()
    except Exception as e:
        logger.error(f"Error displaying {img_path}: {e}")
def display_hourly_clock(fb, current_image=None, img_path=None):
    if not is_ntp_synchronized():
        return # Skip clock overlay if time is not synced
        
    try:
        if current_image:
            bg = current_image.copy()
        else:
            bg = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))

        # Use global standard properties
        draw_time(bg, TIME_FORMAT, TIME_FONT_SIZE, location=TIME_LOCATION, color=TIME_COLOR, border_color=TIME_BORDER_COLOR, border_size=TIME_BORDER_SIZE)
        write_to_fb(fb, bg)
        update_state("clock", img_path)
    except Exception as e:
        logger.error(f"Error displaying hourly clock: {e}")


def get_images():
    return common.get_images()

def get_random_image_index(images):
    if not images:
        return 0

    # Get relative paths of recently shown images (from history)
    recent_paths = common.get_recent_paths(days=None)
    
    indices = list(range(len(images)))
    random.shuffle(indices)

    # Try up to 5 times to find a starting point where the WHOLE group is fresh
    for i in range(min(len(indices), 5)):
        idx = indices[i]
        try:
            is_group_fresh = True
            # Check the next GROUP_SIZE images starting from idx
            for offset in range(GROUP_SIZE):
                check_idx = (idx + offset) % len(images)
                if images[check_idx] in recent_paths:
                    is_group_fresh = False
                    break
            
            if is_group_fresh:
                return idx
        except:
            pass

    # If 5 failed tries, fall back to the first random one
    return indices[0] if indices else 0
def get_next_image_index(images, idx, images_shown_in_group):
    if not images:
        return 0, 0
    
    images_shown_in_group += 1
    # If we've shown enough images in this group, pick a new random starting point
    if images_shown_in_group >= GROUP_SIZE:
        new_idx = get_random_image_index(images)
        return new_idx, 0
    else:
        # Otherwise, just cycle to the next image
        return (idx + 1) % len(images), images_shown_in_group

def main():
    logger.info("Starting Digital Frame service")
    
    # Startup: Force screen ON and clean up any manual overrides
    logger.info("Startup: Forcing screen ON")
    set_screen_state(True)
    was_blanked = False
    if os.path.exists(MANUAL_OFF_FILE):
        os.remove(MANUAL_OFF_FILE)
    if os.path.exists(MANUAL_ON_FILE):
        os.remove(MANUAL_ON_FILE)

    load_fb_info()
    with open(FB_DEV, "wb") as fb:
        bg = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        write_to_fb(fb, bg)
    images = get_images()
    if not images: 
        logger.warning("No images found at startup")

    def signal_handler(sig, frame):
        set_cursor(True)
        set_screen_state(True)
        if os.path.exists(STATE_FILE): os.remove(STATE_FILE)
        sys.exit(0)
    
    def wake_handler(sig, frame):
        logger.info("Wake signal received")
        wake_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGUSR1, wake_handler)
    set_cursor(False)
    clear_console()

    images.sort()
    idx = get_random_image_index(images)
    images_shown_in_group = 0
    now = datetime.now()
    last_hour = now.hour
    last_minute = -1
    last_display_time = 0
    last_rotation_time = 0
    config_mtime = last_config_mtime
    dir_mtime = os.path.getmtime(IMAGE_DIR) if os.path.exists(IMAGE_DIR) else 0
    was_periodic = False
    manual_prev = False

    with open(FB_DEV, "wb") as fb:
        while True:
            # Throttle config checks
            now_time = time.time()
            if now_time - config_mtime > 30:
                try:
                    current_config_mtime = os.path.getmtime('config.ini')
                    if current_config_mtime > config_mtime:
                        old_image_dir = IMAGE_DIR
                        old_selected = SELECTED_FOLDERS
                        config_mtime = load_config_values()
                        last_display_time = 0 # Force refresh on ANY config change
                        last_rotation_time = 0
                        if IMAGE_DIR != old_image_dir or SELECTED_FOLDERS != old_selected:
                            images = get_images()
                            images.sort()
                            idx = get_random_image_index(images)
                            images_shown_in_group = 0
                    else:
                        # Update our last-checked timestamp even if mtime didn't change
                        config_mtime = now_time 
                except:
                    pass
                
            # Check for new images less frequently
            if now_time - dir_mtime > 300: # Every 5 mins
                try:
                    if os.path.exists(IMAGE_DIR):
                        current_dir_mtime = os.path.getmtime(IMAGE_DIR)
                        if current_dir_mtime > dir_mtime:
                            dir_mtime = current_dir_mtime
                            old_len = len(images)
                            images = get_images()
                            images.sort()
                            logger.info(f"New images detected, refreshed list. Count: {len(images)}")
                            if len(images) != old_len:
                                idx = get_random_image_index(images)
                                images_shown_in_group = 0
                except:
                    pass

            # 1. Determine desired screen state
            # Use cached values from load_config_values() instead of get_config()
            is_manually_off = os.path.exists(MANUAL_OFF_FILE)
            is_manually_on = os.path.exists(MANUAL_ON_FILE)
            
            # Priority 1: Manual OFF
            if is_manually_off:
                should_be_on = False
            # Priority 2: Schedule OFF (Stronger than sensors)
            elif common.is_scheduled_off():
                should_be_on = False
            # Priority 3: Manual ON (Override schedule/sensors)
            elif is_manually_on:
                should_be_on = True
            # Priority 4: Presence Detection (ON Hours only)
            elif common.is_presence_enabled():
                # If presence detection is on, display.py should NOT force state.
                # It lets api.py control the power based on activity.
                current_hw_state = common.get_hardware_screen_state()
                should_be_on = (current_hw_state == 'on')
            else:
                # Default to ON if no schedule/sensors/manual override
                should_be_on = True

            # 2. Update screen state if it doesn't match requirement
            if should_be_on and was_blanked:
                logger.info("Screen logic: Turning ON")
                set_screen_state(True)
                was_blanked = False
                last_display_time = 0 # Force refresh
            elif not should_be_on and not was_blanked:
                logger.info("Screen logic: Turning OFF")
                set_screen_state(False)
                was_blanked = True

            # 3. Hardware state sync (only if not already blanked by our logic)
            # This handles if something else turned the screen ON/OFF
            try:
                current_hw_state = common.get_hardware_screen_state()
                hw_on = (current_hw_state == 'on')
                if hw_on and was_blanked:
                    logger.info("Hardware: Screen turned ON externally - forcing refresh")
                    was_blanked = False
                    last_display_time = 0
                elif current_hw_state == 'off' and not was_blanked:
                    # If hardware turned off, respect it but don't force it back on unless needed
                    was_blanked = True
            except: pass

            if not should_be_on:
                # If we are supposed to be off, sleep longer and skip refresh logic
                wake_event.wait(5)
                wake_event.clear()
                continue

            # Check for navigation commands (Move here for better responsiveness)
            if os.path.exists(SHOW_IMAGE_FILE):
                try:
                    with open(SHOW_IMAGE_FILE, "r") as f:
                        show_path = f.read().strip()
                    os.remove(SHOW_IMAGE_FILE)
                    logger.info(f"Manual 'Show' requested for: {show_path}")
                    if show_path and os.path.exists(show_path):
                        display_image(fb, show_path, save=True)
                        last_display_time = time.time()
                        last_rotation_time = time.time()
                        idx = -1 # Next image will be images[0]
                    else:
                        logger.error(f"Manual 'Show' failed: Path not found {show_path}")
                except Exception as e:
                    logger.error(f"Error handling show_image.tmp: {e}")
            
            if os.path.exists(NEXT_GROUP_FILE):
                os.remove(NEXT_GROUP_FILE)
                manual_prev = False
                if images:
                    idx = get_random_image_index(images)
                    images_shown_in_group = 0
                    display_image(fb, os.path.join(IMAGE_DIR, images[idx]), save=True)
                    last_display_time = time.time()
                    last_rotation_time = time.time()
            elif os.path.exists(NEXT_IMAGE_FILE):
                os.remove(NEXT_IMAGE_FILE)
                manual_prev = False
                if images:
                    idx = (idx + 1) % len(images)
                    display_image(fb, os.path.join(IMAGE_DIR, images[idx]), save=True)
                    last_display_time = time.time()
                    last_rotation_time = time.time()
            elif os.path.exists(PREV_IMAGE_FILE):
                os.remove(PREV_IMAGE_FILE)
                manual_prev = True
                if images:
                    # Try to use history to find the previous image
                    navigated = False
                    try:
                        history = common.get_history(limit=50)
                        current_path = images[idx]
                        found_idx = -1
                        for i in range(len(history)):
                            if history[i]["path"] == current_path:
                                found_idx = i
                                break
                        if found_idx != -1 and found_idx + 1 < len(history):
                            prev_path = history[found_idx + 1]["path"]
                            for i in range(len(images)):
                                if images[i] == prev_path:
                                    idx = i
                                    navigated = True
                                    break
                    except Exception as e:
                        logger.error(f"Error navigating via history: {e}")
                    
                    if not navigated:
                        idx = (idx - 1) % len(images)
                    
                    display_image(fb, os.path.join(IMAGE_DIR, images[idx]), save=False)
                    last_display_time = time.time()
                    last_rotation_time = time.time()

            now = datetime.now()
            # Hourly/Periodic/Scheduled clock check
            is_periodic = SHOW_PERIODIC and ((0 <= now.minute < 3) or (30 <= now.minute < 33))
            
            is_scheduled = False
            if SHOW_SCHEDULED:
                for sch in [CLOCK_SCHEDULE_1, CLOCK_SCHEDULE_2]:
                    if sch and croniter.is_valid(sch):
                        it = croniter(sch, now)
                        prev = it.get_prev(datetime)
                        if (now - prev).total_seconds() < 180: # 3 minutes window
                            is_scheduled = True; break
            
            if not was_blanked and ((SHOW_HOURLY and now.hour != last_hour) or is_periodic or is_scheduled):
                # Check if it's time to rotate image during periodic clock
                if time.time() - last_rotation_time >= INTERVAL:
                    idx, images_shown_in_group = get_next_image_index(images, idx, images_shown_in_group)
                    last_rotation_time = time.time()
                    last_display_time = time.time()
                    current_image_obj = None # Force reload

                # Ensure we have the current background image ready for the clock overlay
                if now.minute != last_minute or 'current_image_obj' not in locals() or current_image_obj is None:
                    last_minute = now.minute
                    current_image_obj = None
                    if images and idx < len(images):
                        try:
                            current_image_obj = Image.open(os.path.join(IMAGE_DIR, images[idx]))
                            current_image_obj = ImageOps.exif_transpose(current_image_obj)
                            img_width, img_height = current_image_obj.size
                            scale = min(WIDTH / img_width, HEIGHT / img_height)
                            new_width, new_height = int(img_width * scale), int(img_height * scale)
                            current_image_obj = current_image_obj.resize((new_width, new_height), Image.Resampling.LANCZOS)
                            bg_img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
                            bg_img.paste(current_image_obj, ((WIDTH - new_width) // 2, (HEIGHT - new_height) // 2))
                            current_image_obj = bg_img
                        except Exception as e:
                            logger.error(f"Error preparing background for clock: {e}")
                            current_image_obj = None

                if now.hour != last_hour:
                    last_hour = now.hour
                    # Hourly clock: show for 10 seconds with smooth animation
                    logger.info(f"Displaying hourly clock at {now.hour}:00")
                    start_time = time.time()
                    while time.time() - start_time < 10:
                        display_hourly_clock(fb, current_image_obj, os.path.join(IMAGE_DIR, images[idx]) if images and idx < len(images) else None)
                        time.sleep(0.05)
                    last_display_time = 0 # Force a refresh after hourly clock
                    continue

                if is_periodic or is_scheduled:
                    was_periodic = True
                    display_hourly_clock(fb, current_image_obj, os.path.join(IMAGE_DIR, images[idx]) if images and idx < len(images) else None)
                    # Don't use 'continue' here, allow the rest of the loop to run
                    # so that 'should_refresh' logic can trigger image rotation.
                    # We use a small sleep to maintain responsiveness for animations
                    # but avoid 100% CPU.
                    wake_event.wait(0.05)
                    wake_event.clear()
                    # We don't continue, so the logic below for should_refresh will be evaluated.

            # Logic for when to refresh display
            should_refresh = False
            if was_blanked:
                # If screen is OFF, we don't refresh anything
                should_refresh = False
            elif was_periodic and not (is_periodic or is_scheduled):
                should_refresh = True
                was_periodic = False
            elif time.time() - last_rotation_time >= INTERVAL:
                # Time for NEXT image
                should_refresh = True
                if images:
                    if manual_prev:
                        # User was navigating backwards, now move "back to top" (random image)
                        idx = get_random_image_index(images)
                        manual_prev = False
                        images_shown_in_group = 0
                    else:
                        idx, images_shown_in_group = get_next_image_index(images, idx, images_shown_in_group)
                else:
                    idx = 0
                last_rotation_time = time.time()
            elif last_display_time == 0:
                # Forced refresh (config change or screen ON)
                should_refresh = True
                last_rotation_time = time.time()
            elif SHOW_TIME and now.minute != last_minute:
                # Just refresh SAME image for clock update
                should_refresh = True

            if should_refresh and images:
                display_image(fb, os.path.join(IMAGE_DIR, images[idx]), save=True)
                last_display_time = time.time()
                last_minute = now.minute
            
            sleep_time = 30 if WEAK_MACHINE else 1
            wake_event.wait(sleep_time)
            wake_event.clear()

if __name__ == "__main__":
    main()
