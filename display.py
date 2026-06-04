import os
import time
import random
import signal
import sys
import configparser
import logging
logging.disable(logging.INFO)
logging.getLogger().setLevel(logging.ERROR)
import json
import tempfile
import subprocess
import urllib.request
from datetime import datetime
from croniter import croniter
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageOps
import math

# Setup Logging
log_level = logging.ERROR
logging.basicConfig(force=True, 
    filename='logs/digitalframe.log',
    level=log_level,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def get_config():
    config = configparser.ConfigParser()
    if os.path.exists('config.ini.example'):
        config.read('config.ini.example')
    config.read('config.ini')
    return config

# Configuration loading
def load_config_values():
    global IMAGE_DIR, INTERVAL, GROUP_SIZE, FB_DEV, COLOR_ORDER, LOG_LEVEL_STR, LOG_FILE
    global SHOW_TIME, TIME_FORMAT, TIME_FONT_SIZE, TIME_LOCATION, TIME_COLOR, TIME_BORDER_COLOR, TIME_BORDER_SIZE, NEG_TIME, TIME_ALPHA
    global SHOW_HOURLY, SHOW_PERIODIC, SHOW_SCHEDULED, CLOCK_SCHEDULE_1, CLOCK_SCHEDULE_2, SCREEN_OFF_HOUR, SCREEN_ON_HOUR, SELECTED_FOLDERS, WEAK_MACHINE

    config = configparser.ConfigParser()
    # Load defaults from example file first
    if os.path.exists('config.ini.example'):
        config.read('config.ini.example')
    
    files_read = config.read('config.ini')
    if not files_read and not os.path.exists('config.ini.example'):
        logger.error("Could not read config.ini or config.ini.example!")
        return 0

    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
    IMAGE_DIR = config.get('DEFAULT', 'imagedir', fallback=os.path.join(PROJECT_ROOT, 'images/'))
    SELECTED_FOLDERS = config.get('DEFAULT', 'selected_folders', fallback='all')
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
    
    # Also update the basic config if possible, or just update the logger level
    for handler in logging.root.handlers[:]:
        handler.setLevel(numeric_level)

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
    
    return os.path.getmtime('config.ini')

last_config_mtime = load_config_values()

STATE_FILE = "state.json"
HISTORY_FILE = "history.json"
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WIDTH = 0
HEIGHT = 0
BPP = 0
STRIDE = 0

def set_screen_state(on):
    """Unified screen control using both vcgencmd and blanking."""
    val_v = "1" if on else "0"
    val_b = "0" if on else "1"
    try:
        subprocess.run(['vcgencmd', 'display_power', val_v], check=False, capture_output=True)
    except:
        pass
    try:
        with open("/sys/class/graphics/fb0/blank", "w") as f:
            f.write(val_b)
    except:
        pass
    try:
        val_s = "force" if not on else "poke"
        subprocess.run(['setterm', '--blank', val_s], check=False, capture_output=True)
    except:
        pass
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

        state = {
            "type": type,
            "current_image": current_img,
            "full_path": img_path if img_path else "",
            "last_update": datetime.now().isoformat(),
            "pid": os.getpid()
        }
        fd, temp_path = tempfile.mkstemp(dir=".", prefix="state_tmp_")
        with os.fdopen(fd, 'w') as f:
            json.dump(state, f)
        os.replace(temp_path, STATE_FILE)
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
            
        entry = {
            "name": os.path.basename(img_path), 
            "path": rel_path,
            "timestamp": now.isoformat()
        }
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                try: history = json.load(f)
                except: history = []
        history.append(entry)
        if len(history) > 3600:
            history = history[-3600:]
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f)
    except Exception as e:
        logger.error(f"Error updating history file: {e}")

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
        img = Image.open(img_path)
        img = ImageOps.exif_transpose(img)
        img_width, img_height = img.size
        scale = min(WIDTH / img_width, HEIGHT / img_height)
        new_width, new_height = int(img_width * scale), int(img_height * scale)
        img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        bg = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        bg.paste(img, ((WIDTH - new_width) // 2, (HEIGHT - new_height) // 2))
        if SHOW_TIME:
            draw_time(bg, TIME_FORMAT, TIME_FONT_SIZE)
        write_to_fb(fb, bg)
        update_state("image", img_path)
        update_history(img_path, save=save)
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
    valid_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.bmp')
    imgs = []
    
    # Sources: Google Photos synced albums + Configured local pictures folder
    sources = [IMAGE_DIR]
    
    selected = []
    if SELECTED_FOLDERS != 'all':
        selected = [s.strip() for s in SELECTED_FOLDERS.split(',')]
    
    for source in sources:
        if not os.path.exists(source): continue
        
        for root, dirs, files in os.walk(source):
            # Get relative path from source to determine if this folder is selected
            rel_path = os.path.relpath(root, source)
            
            if SELECTED_FOLDERS != 'all':
                # Check if rel_path or any of its parents are in selected
                is_selected = False
                if rel_path == '.':
                    # Images in the root directory are only shown if 'all' is selected
                    # or if we explicitly allow them. For now, let's say they aren't 
                    # included unless 'all' is chosen, to stay consistent with folder selection.
                    pass
                else:
                    for s in selected:
                        if rel_path == s or rel_path.startswith(s + os.sep):
                            is_selected = True
                            break
                
                if not is_selected:
                    continue

            for f in files:
                if f.lower().endswith(valid_extensions):
                    imgs.append(os.path.join(root, f))
    return imgs

def is_hour_in_range(hour, start, end):
    if start == end: return False
    # Standard range (e.g., 22 to 07)
    if start < end:
        return start <= hour < end
    else:
        # Wrapped range (e.g., 22 to 07 where start > end)
        return hour >= start or hour < end

def get_next_image_index(images, idx, images_shown_in_group):
    if not images:
        return 0, 0
    
    images_shown_in_group += 1
    # If we've shown enough images in this group, pick a new random starting point
    if images_shown_in_group >= GROUP_SIZE:
        # Try up to 5 times to find an image not shown in the last 24 hours
        recent_history = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r") as f:
                    full_history = json.load(f)
                cutoff = datetime.now().timestamp() - 86400
                recent_history = [e["name"] for e in full_history if datetime.fromisoformat(e["timestamp"]).timestamp() > cutoff]
            except: pass

        for _ in range(5):
            new_idx = random.randint(0, len(images) - 1)
            if os.path.basename(images[new_idx]) not in recent_history:
                return new_idx, 0
        return random.randint(0, len(images) - 1), 0
    else:
        # Otherwise, just cycle to the next image
        return (idx + 1) % len(images), images_shown_in_group

def main():
    logger.info("Starting Digital Frame service")
    # Initial state should respect schedule
    now = datetime.now()
    in_off_hours = is_hour_in_range(now.hour, SCREEN_OFF_HOUR, SCREEN_ON_HOUR)
    if in_off_hours:
        logger.info(f"Startup: Currently in OFF hours ({now.hour}), starting with screen OFF")
        set_screen_state(False)
        was_blanked = True
    else:
        logger.info(f"Startup: Currently in ON hours ({now.hour}), starting with screen ON")
        set_screen_state(True)
        was_blanked = False

    load_fb_info()
    with open(FB_DEV, "wb") as fb:
        bg = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
        write_to_fb(fb, bg)
    images = get_images()
    if not images: return

    def signal_handler(sig, frame):
        set_cursor(True)
        set_screen_state(True)
        if os.path.exists(STATE_FILE): os.remove(STATE_FILE)
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    set_cursor(False)
    clear_console()

    images.sort()
    idx = random.randint(0, len(images) - 1)
    images_shown_in_group = 0
    last_hour = now.hour
    last_minute = -1
    last_display_time = 0
    last_rotation_time = 0
    config_mtime = last_config_mtime
    dir_mtime = os.path.getmtime(IMAGE_DIR)
    was_periodic = False
    manual_prev = False

    with open(FB_DEV, "wb") as fb:
        while True:
            # Check for config or directory changes
            try:
                # Check for config changes
                current_config_mtime = os.path.getmtime('config.ini')
                if current_config_mtime > config_mtime:
                    old_image_dir = IMAGE_DIR
                    old_selected = SELECTED_FOLDERS
                    config_mtime = load_config_values()
                    last_display_time = 0 # Force refresh on ANY config change
                    last_rotation_time = 0
                    if IMAGE_DIR != old_image_dir or SELECTED_FOLDERS != old_selected:
                        images = get_images()
                        images.sort(); idx = 0; images_shown_in_group = 0
                
                # Check for new images
                current_dir_mtime = os.path.getmtime(IMAGE_DIR)
                if current_dir_mtime > dir_mtime:
                    dir_mtime = current_dir_mtime
                    images = get_images()
                    images.sort()
                    logger.info("New images detected, refreshed list.")

            except: pass

            now = datetime.now()
            
            # 1. Schedule check
            config = get_config()
            schedule_enabled = config.getboolean('SCHEDULE', 'enabled', fallback=False)
            in_off_hours = is_hour_in_range(now.hour, SCREEN_OFF_HOUR, SCREEN_ON_HOUR)
            
            # Manual override check
            manual_on = os.path.exists("manual_on.tmp")

            if schedule_enabled and in_off_hours and not manual_on:
                if not was_blanked:
                    logger.info(f"Schedule: Entering OFF hours ({now.hour})")
                    set_screen_state(False); was_blanked = True
                time.sleep(10); continue
            else:
                if was_blanked:
                    logger.info(f"Schedule: Entering ON hours ({now.hour}) or Manual Override or Schedule Disabled")
                    set_screen_state(True); was_blanked = False
                    last_display_time = 0 # Force refresh immediately
                
                # If we are in off hours but it's a manual override, don't sleep for 10s
                # just continue to the rest of the loop logic.
                pass

            # Check for navigation commands (Move here for better responsiveness)
            if os.path.exists("show_image.tmp"):
                try:
                    with open("show_image.tmp", "r") as f:
                        show_path = f.read().strip()
                    os.remove("show_image.tmp")
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
            
            if os.path.exists("next_image.tmp"):
                os.remove("next_image.tmp")
                manual_prev = False
                if images:
                    idx = (idx + 1) % len(images)
                    display_image(fb, images[idx], save=True)
                    last_display_time = time.time()
                    last_rotation_time = time.time()
            elif os.path.exists("prev_image.tmp"):
                os.remove("prev_image.tmp")
                manual_prev = True
                if images:
                    # Try to use history to find the previous image
                    navigated = False
                    try:
                        if os.path.exists(HISTORY_FILE):
                            with open(HISTORY_FILE, "r") as f:
                                history = json.load(f)
                            current_filename = os.path.basename(images[idx])
                            found_idx = -1
                            for i in range(len(history) - 1, -1, -1):
                                if history[i]["name"] == current_filename:
                                    found_idx = i
                                    break
                            if found_idx > 0:
                                prev_filename = history[found_idx - 1]["name"]
                                for i in range(len(images)):
                                    if os.path.basename(images[i]) == prev_filename:
                                        idx = i
                                        navigated = True
                                        break
                    except Exception as e:
                        logger.error(f"Error navigating via history: {e}")
                    
                    if not navigated:
                        idx = (idx - 1) % len(images)
                    
                    display_image(fb, images[idx], save=False)
                    last_display_time = time.time()
                    last_rotation_time = time.time()

            # 2. Hardware state sync (only if not already blanked by schedule)
            try:
                res = subprocess.run(['vcgencmd', 'display_power'], capture_output=True, text=True)
                is_on = 'display_power=1' in res.stdout
                if is_on and was_blanked:
                    # This handles if something else turned the screen ON
                    logger.info("Hardware: Screen turned ON externally - forcing refresh")
                    was_blanked = False
                    last_display_time = 0
                elif not is_on and not was_blanked:
                    # This handles if something else turned the screen OFF
                    was_blanked = True
            except: pass

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
                if (is_periodic or is_scheduled) and time.time() - last_rotation_time >= INTERVAL:
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
                            current_image_obj = Image.open(images[idx])
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
                        display_hourly_clock(fb, current_image_obj, images[idx] if images and idx < len(images) else None)
                        time.sleep(0.05)
                    last_display_time = time.time() # Update display time
                    last_rotation_time = time.time() # Reset rotation time after hourly clock too
                    continue

                if is_periodic or is_scheduled:
                    was_periodic = True
                    display_hourly_clock(fb, current_image_obj, images[idx] if images and idx < len(images) else None)
                    time.sleep(0.05) # ~20 FPS for smooth anti-burn-in movement
                    continue # Stay in clock mode

            # Logic for when to refresh display
            should_refresh = False
            if was_periodic:
                should_refresh = True
                was_periodic = False
            elif time.time() - last_rotation_time >= INTERVAL:
                # Time for NEXT image
                should_refresh = True
                if images:
                    if manual_prev:
                        # User was navigating backwards, now move "back to top" (random image)
                        idx = random.randint(0, len(images) - 1)
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
                display_image(fb, images[idx], save=True)
                last_display_time = time.time()
                last_minute = now.minute
            
            sleep_time = 30 if WEAK_MACHINE else 1
            time.sleep(sleep_time)

if __name__ == "__main__":
    main()
