import logging; logging.basicConfig(level=logging.INFO, force=True); logging.getLogger("werkzeug").setLevel(logging.INFO)
import os
import json
import configparser
import subprocess
import signal
import shutil
import cv2
import io
import threading
import time
import numpy as np
from flask import Flask, jsonify, request, send_from_directory, render_template, Response
from flask_cors import CORS
from croniter import croniter
from datetime import datetime
import downloader

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)
CORS(app)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_ROOT, 'config.ini')
STATE_FILE = os.path.join(PROJECT_ROOT, 'state.json')
HISTORY_FILE = os.path.join(PROJECT_ROOT, 'history.json')
REMOVE_DIR = os.path.join(PROJECT_ROOT, 'removed/')

def get_config():
    config = configparser.ConfigParser(interpolation=None)
    # Load defaults from example file first
    example_config = os.path.join(PROJECT_ROOT, 'config.ini.example')
    if os.path.exists(example_config):
        config.read(example_config)
    # Overlay with actual config
    config.read(CONFIG_FILE)
    return config

# Motion Detection State
motion_data = {
    "last_frame": None,
    "last_movement_time": time.time(),
    "screen_state": "on"
}
camera_lock = threading.Lock()

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
        with open("/sys/class/graphics/fb0/blank", "w") as f:
            f.write(val_b)
    except:
        pass
    try:
        # Try setterm blanking
        val_s = "force" if not on else "poke"
        subprocess.run(['setterm', '--blank', val_s], check=False, capture_output=True)
    except:
        pass

def get_hardware_screen_state():
    """Read actual screen state from hardware sources."""
    # 1. Try DRM DPMS state (most reliable on modern Pi OS with KMS)
    try:
        # Look for connected HDMI ports
        drm_path = "/sys/class/drm/"
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
    except:
        pass

    # 3. Try vcgencmd (Legacy/Firmware fallback)
    try:
        result = subprocess.run(['vcgencmd', 'display_power', '-1'], capture_output=True, text=True)
        if 'display_power=0' in result.stdout:
            return 'off'
    except:
        pass
    
    return 'on'

def motion_detection_thread():
    while True:
        try:
            config = get_config()
            weak_machine = config.getboolean('DEFAULT', 'weak_machine', fallback=False)
            
            # Check both MOTION and CAMERA enabled flags
            if not config.has_section('MOTION') or not config.getboolean('MOTION', 'enabled', fallback=False) or \
               not config.getboolean('CAMERA', 'enabled', fallback=True):
                time.sleep(30 if weak_machine else 5)
                continue

            # Sync internal state with actual hardware state
            current_state = get_hardware_screen_state()
            motion_data["screen_state"] = current_state

            sensitivity = config.getint('MOTION', 'sensitivity', fallback=50)
            auto_sens = config.getboolean('MOTION', 'auto_sensitivity', fallback=False)
            
            effective_sensitivity = sensitivity
            if auto_sens and motion_data["last_frame"] is not None:
                light = get_avg_light(motion_data["last_frame"])
                if light < 50:
                    effective_sensitivity = min(100, sensitivity + 30)
                elif light > 200:
                    effective_sensitivity = max(1, sensitivity - 20)
            
            timeout = config.getint('MOTION', 'timeout', fallback=300)
            
            # Map sensitivity (1-100) to threshold (100-1)
            # Higher sensitivity means lower threshold/smaller changes detected
            threshold = max(1, 100 - effective_sensitivity)

            # Shutter (exposure time in us) and Gain settings for low light
            shutter = config.getint('MOTION', 'shutter', fallback=0)
            gain = config.getint('MOTION', 'gain', fallback=0)
            
            # Capture a small image for motion detection to save CPU
            cmd = ['rpicam-still', '-n', '-o', '-', '-t', '200', '--width', '320', '--height', '240', '--hflip', '--vflip']
            if shutter > 0:
                cmd.extend(['--shutter', str(shutter)])
            if gain > 0:
                cmd.extend(['--gain', str(gain)])
            
            if shutil.which('rpicam-still') is None and shutil.which('libcamera-still'):
                cmd[0] = 'libcamera-still'
            
            with camera_lock:
                result = subprocess.run(cmd, capture_output=True)
            
            if result.returncode == 0:
                nparr = np.frombuffer(result.stdout, np.uint8)
                frame = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
                if frame is not None:
                    frame = cv2.GaussianBlur(frame, (21, 21), 0)
                    
                    if motion_data["last_frame"] is not None:
                        frame_delta = cv2.absdiff(motion_data["last_frame"], frame)
                        thresh = cv2.threshold(frame_delta, threshold, 255, cv2.THRESH_BINARY)[1]
                        thresh = cv2.dilate(thresh, None, iterations=2)
                        
                        # Calculate percentage of changed pixels
                        changed_pixels = np.sum(thresh) / 255
                        change_percent = (changed_pixels / (frame.shape[0] * frame.shape[1])) * 100
                        
                        # If more than 0.5% of pixels changed, count as movement
                        if change_percent > 0.5:
                            motion_data["last_movement_time"] = time.time()
                            if current_state == "off":
                                set_screen_state(True)
                                motion_data["screen_state"] = "on"
                                restart_display_service()
                    
                    motion_data["last_frame"] = frame
            
            # Check for timeout
            if time.time() - motion_data["last_movement_time"] > timeout:
                if current_state == "on":
                    set_screen_state(False)
                    motion_data["screen_state"] = "off"
            
            # Sleep between checks
            time.sleep(30 if weak_machine else 2)
        except Exception as e:
            time.sleep(10)

# Camera Scheduling Thread
def camera_scheduler_thread():
    while True:
        try:
            config = get_config()
            if not config.getboolean('CAMERA', 'enabled', fallback=True):
                time.sleep(60)
                continue
                
            cron_expr = config.get('CAMERA', 'schedule', fallback='')
            if not cron_expr or not croniter.is_valid(cron_expr):
                time.sleep(60)
                continue
            
            # Check if it's time for a capture
            iter = croniter(cron_expr, datetime.now())
            next_run = iter.get_next(datetime)
            
            # If the next run is within the next 60 seconds, wait for it
            diff = (next_run - datetime.now()).total_seconds()
            if diff < 60:
                time.sleep(max(0, diff))
                # Perform capture
                capture_image()
                time.sleep(60) # Prevent multiple triggers in same minute
            else:
                time.sleep(60)
        except Exception as e:
            time.sleep(60)

def capture_image():
    with camera_lock:
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"capture_{timestamp}.jpg"
        
        config = get_config()
        image_dir = config.get('CAMERA', 'imagedir_captures', fallback=os.path.join(PROJECT_ROOT, 'captures/'))
        os.makedirs(image_dir, exist_ok=True)
        filepath = os.path.join(image_dir, filename)
        
        # Use rpicam-still to capture image to file
        cmd = ['rpicam-still', '-n', '-o', filepath, '-t', '2000', '--width', '1296', '--height', '972', '--hflip', '--vflip', '--tuning-file', '/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json']
        if shutil.which('rpicam-still') is None and shutil.which('libcamera-still'):
            cmd[0] = 'libcamera-still'
            
        try:
            subprocess.run(cmd, check=True)
        except Exception as e:
            pass

# Google Photos Sync Thread
def google_photos_sync_thread():
    print('Google Photos sync thread started')
    while True:
        try:
            downloader.sync_all()
        except Exception as e:
            print(f'Auto-sync error: {e}')
        # Sync every hour
        time.sleep(3600)

def get_avg_light(frame):
    return np.mean(frame)

@app.route('/api/motion', methods=['GET'])
def get_motion_config():
    config = get_config()
    if not config.has_section('MOTION'):
        return jsonify({"enabled": False, "sensitivity": 50, "auto_sensitivity": False, "timeout": 300})

    current_state = get_hardware_screen_state()
    motion_data["screen_state"] = current_state
    
    sensitivity = config.getint('MOTION', 'sensitivity', fallback=50)
    auto_sens = config.getboolean('MOTION', 'auto_sensitivity', fallback=False)
    
    effective_sensitivity = sensitivity
    if auto_sens and motion_data["last_frame"] is not None:
        light = get_avg_light(motion_data["last_frame"])
        if light < 50:
            effective_sensitivity = min(100, sensitivity + 30)
        elif light > 200:
            effective_sensitivity = max(1, sensitivity - 20)

    return jsonify({
        "enabled": config.getboolean('MOTION', 'enabled', fallback=False),
        "auto_sensitivity": auto_sens,
        "sensitivity": sensitivity,
        "effective_sensitivity": effective_sensitivity,
        "timeout": config.getint('MOTION', 'timeout', fallback=300),
        "shutter": config.getint('MOTION', 'shutter', fallback=0),
        "gain": config.getint('MOTION', 'gain', fallback=0),
        "last_movement_seconds_ago": int(time.time() - motion_data["last_movement_time"]),
        "screen_state": current_state
    })

@app.route('/api/motion', methods=['POST'])
def update_motion_config():
    data = request.json
    config = get_config()
    if not config.has_section('MOTION'):
        config.add_section('MOTION')
    
    if 'enabled' in data:
        config.set('MOTION', 'enabled', str(data['enabled']))
    if 'auto_sensitivity' in data:
        config.set('MOTION', 'auto_sensitivity', str(data['auto_sensitivity']))
    if 'sensitivity' in data:
        config.set('MOTION', 'sensitivity', str(data['sensitivity']))
    if 'timeout' in data:
        config.set('MOTION', 'timeout', str(data['timeout']))
    if 'shutter' in data:
        config.set('MOTION', 'shutter', str(data['shutter']))
    if 'gain' in data:
        config.set('MOTION', 'gain', str(data['gain']))
        
    with open(CONFIG_FILE, 'w') as f:
        config.write(f)
    
    return jsonify({"status": "success", "message": "Motion configuration updated"})

@app.route('/api/state', methods=['GET'])
def get_state():
    if not os.path.exists(STATE_FILE):
        default_state = {
            "type": "idle",
            "current_image": "Idle",
            "full_path": "",
            "last_update": datetime.now().isoformat(),
            "pid": 0
        }
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump(default_state, f)
        except Exception as e:
            return jsonify({"error": f"Failed to create state file: {e}"}), 500

    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
            return jsonify(state)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": "State file not found"}), 404

@app.route('/api/history', methods=['GET'])
def get_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
            return jsonify(history[::-1]) # Newest first
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify([])

@app.route('/api/remove', methods=['POST'])
def remove_image():
    data = request.json
    filename = data.get('filename')
    if not filename:
        return jsonify({"error": "No filename provided"}), 400
    
    config = get_config()
    image_dir = config.get('DEFAULT', 'imagedir', fallback=os.path.join(PROJECT_ROOT, 'images/'))
    remove_dir = config.get('DEFAULT', 'removedir', fallback=REMOVE_DIR)
    
    src = None
    for root, dirs, files in os.walk(image_dir):
        if filename in files:
            src = os.path.join(root, filename)
            break
            
    if src and os.path.exists(src):
        try:
            # Check if this is the currently displayed image
            is_current = False
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, 'r') as f:
                        state = json.load(f)
                        current_image = state.get('current_image', '')
                        if filename == os.path.basename(current_image) or filename == current_image:
                            is_current = True
                except:
                    pass

            # Remove from history.json
            if os.path.exists(HISTORY_FILE):
                try:
                    with open(HISTORY_FILE, 'r') as f:
                        history = json.load(f)
                    
                    # Remove all entries with this filename
                    new_history = [item for item in history if item.get('name') != filename]
                    
                    if len(new_history) != len(history):
                        with open(HISTORY_FILE, 'w') as f:
                            json.dump(new_history, f)
                except Exception as e:
                    logging.error(f"Error updating history after removal: {e}")

            # Move the file
            os.makedirs(remove_dir, exist_ok=True)
            dst = os.path.join(remove_dir, filename)
            shutil.move(src, dst)

            # If it was the current image, trigger next
            if is_current:
                with open("next_image.tmp", "w") as f:
                    f.write("next")

            return jsonify({"status": "success", "message": f"Moved {filename} to {remove_dir}"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"error": f"File {filename} not found in {image_dir}"}), 404

@app.route('/api/folders', methods=['GET'])
def get_folders():
    config = get_config()
    image_dir = config.get('DEFAULT', 'imagedir', fallback=os.path.join(PROJECT_ROOT, 'images/'))
    selected = config.get('DEFAULT', 'selected_folders', fallback='all')
    
    folders = []
    if os.path.exists(image_dir):
        # Walk the directory tree to find all subfolders
        for root, dirs, files in os.walk(image_dir):
            for d in dirs:
                # Calculate the relative path from the image_dir
                full_path = os.path.join(root, d)
                rel_path = os.path.relpath(full_path, image_dir)
                folders.append(rel_path)
    
    return jsonify({
        "available_folders": sorted(folders),
        "selected_folders": selected
    })

@app.route('/api/folders', methods=['POST'])
def save_folders():
    data = request.json
    selected = data.get('selected', 'all')
    
    config = get_config()
    config.set('DEFAULT', 'selected_folders', selected)
    
    with open(CONFIG_FILE, 'w') as f:
        config.write(f)
        
    restart_frame()
    return jsonify({"status": "success"})

@app.route('/api/show', methods=['POST'])
def show_image():
    data = request.json
    image_path = data.get('path')
    
    # If path is missing or invalid, try to treat 'path' as a filename and search for it
    if not image_path or not os.path.exists(image_path):
        filename = os.path.basename(image_path) if image_path else data.get('filename')
        if not filename:
            return jsonify({"error": "No image identifier provided"}), 400
            
        # Search for filename in IMAGE_DIR
        config = get_config()
        image_dir = config.get('DEFAULT', 'imagedir', fallback=os.path.join(PROJECT_ROOT, 'images/'))
        
        found_path = None
        for root, dirs, files in os.walk(image_dir):
            if filename in files:
                found_path = os.path.join(root, filename)
                break
        
        if found_path:
            image_path = found_path
        else:
            return jsonify({"error": f"Image {filename} not found in {image_dir}"}), 404

    # Write to a trigger file that display.py monitors
    with open("show_image.tmp", "w") as f:
        f.write(image_path)

    return jsonify({"status": "success"})

@app.route('/api/config', methods=['GET'])
def get_config_api():
    config = get_config()
    config_dict = {'DEFAULT': dict(config.items('DEFAULT'))}
    for section in config.sections():
        config_dict[section] = dict(config.items(section))
    return jsonify(config_dict)

@app.route('/api/config', methods=['POST'])
def update_config():
    data = request.json
    config = get_config()
    for section, section_data in data.items():
        if section == 'DEFAULT':
            for key, value in section_data.items():
                config.set('DEFAULT', key, str(value))
        else:
            if not config.has_section(section):
                config.add_section(section)
            for key, value in section_data.items():
                config.set(section, key, str(value))

    with open(CONFIG_FILE, 'w') as f:
        config.write(f)
    restart_frame() # Restart to apply config
    return jsonify({"status": "success", "message": "Configuration updated"})

def _restart_frame_process():
    try:
        time.sleep(0.5) # Give the API a moment to respond
        subprocess.run(['sudo', 'systemctl', 'restart', 'frame'], check=True)
    except Exception as e:
        pass

@app.route('/api/restart', methods=['POST'])
def restart_frame():
    restart_thread = threading.Thread(target=_restart_frame_process)
    restart_thread.daemon = True 
    restart_thread.start()
    return jsonify({"status": "success", "message": "Frame service restart initiated."})

@app.route('/api/next', methods=['POST'])
def next_image():
    with open("next_image.tmp", "w") as f: f.write("next")
    return jsonify({"status": "success"})

@app.route('/api/prev', methods=['POST'])
def prev_image():
    with open("prev_image.tmp", "w") as f: f.write("prev")
    return jsonify({"status": "success"})

@app.route('/current')
def fullscreen_view():
    return render_template('full_screen.html')

@app.route('/api/image/<path:filename>')
def serve_image(filename):
    config = get_config()
    image_dir = config.get('DEFAULT', 'imagedir', fallback=os.path.join(PROJECT_ROOT, 'images/'))
    remove_dir = config.get('DEFAULT', 'removedir', fallback=REMOVE_DIR)
    
    # Try direct path first (if it's a relative path from imagedir)
    full_path = os.path.join(image_dir, filename)
    if os.path.exists(full_path) and os.path.isfile(full_path):
        return send_from_directory(os.path.dirname(full_path), os.path.basename(full_path))
        
    # Fallback to searching (for backward compatibility or if only filename is provided)
    paths = [image_dir, remove_dir]
    for p in paths:
        for root, dirs, files in os.walk(p):
            if filename in files:
                return send_from_directory(root, filename)
    return f"File {filename} not found", 404

def _restart_frame_task():
    """Wait briefly and restart the frame service."""
    time.sleep(1)
    try:
        subprocess.run(['sudo', 'systemctl', 'restart', 'frame'], check=False)
    except Exception as e:
        pass

def restart_display_service():
    """Helper to initiate a restart of the frame service."""
    threading.Thread(target=_restart_frame_task, daemon=True).start()

@app.route('/api/screen', methods=['GET', 'POST'])
def screen_control():
    if request.method == 'POST':
        data = request.json
        state = data.get('state')
        try:
            on = (state == 'on')
            if on:
                # Create manual override file
                with open("manual_on.tmp", "w") as f: f.write("1")
            else:
                # Remove manual override file if it exists
                if os.path.exists("manual_on.tmp"):
                    os.remove("manual_on.tmp")
            
            set_screen_state(on)
            motion_data["screen_state"] = state
            if on:
                motion_data["last_movement_time"] = time.time()
                restart_display_service()
            return jsonify({"status": "success", "state": state})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        state = get_hardware_screen_state()
        motion_data["screen_state"] = state
        return jsonify({"state": state})

@app.route('/api/internal/screen_state', methods=['POST'])
def sync_screen_state():
    data = request.json
    state = data.get('state')
    if state in ['on', 'off']:
        motion_data["screen_state"] = state
        if state == 'on':
            motion_data["last_movement_time"] = time.time()
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 400

@app.route('/api/video_feed')
def video_feed():
    config = get_config()
    if not config.getboolean('CAMERA', 'enabled', fallback=True):
        return "Camera is disabled", 404
        
    res = config.get('CAMERA', 'video_resolution', fallback='640x480').split('x')
    width, height = res[0], res[1]

    def generate():
        # Using rpicam-vid for a continuous stream is much more efficient than rpicam-still in a loop
        cmd = ['rpicam-vid', '-t', '0', '--codec', 'mjpeg', '--inline', '--width', width, '--height', height, '--hflip', '--vflip', '--tuning-file', '/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json', '-o', '-', '-n']
        if shutil.which('rpicam-vid') is None and shutil.which('libcamera-vid'): cmd[0] = 'libcamera-vid'
        
        # We hold the lock for the entire duration of the stream to avoid contention
        with camera_lock:
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
            try:
                buffer = b""
                while True:
                    chunk = process.stdout.read(8192)
                    if not chunk: break
                    buffer += chunk
                    
                    # Split buffer into individual JPEG frames using start (FF D8) and end (FF D9) markers
                    while True:
                        start = buffer.find(b'\xff\xd8')
                        end = buffer.find(b'\xff\xd9')
                        if start != -1 and end != -1 and start < end:
                            jpg = buffer[start:end+2]
                            buffer = buffer[end+2:]
                            yield (b'--frame\r\n'
                                   b'Content-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n')
                        else:
                            # If we have a lot of data but no full frame, something is wrong, clear it
                            if len(buffer) > 1000000: buffer = b""
                            break
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/camera_feed')
def camera_feed():
    config = get_config()
    if not config.getboolean('CAMERA', 'enabled', fallback=True):
        return "Camera is disabled", 404
        
    shutter = config.getint('MOTION', 'shutter', fallback=0)
    gain = config.getint('MOTION', 'gain', fallback=0)

    cmd = ['rpicam-still', '-n', '-o', '-', '-t', '2000', '--width', '1296', '--height', '972', '--hflip', '--vflip', '--tuning-file', '/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json']
    
    if shutter > 0:
        cmd.extend(['--shutter', str(shutter)])
    if gain > 0:
        cmd.extend(['--gain', str(gain)])

    if shutil.which('rpicam-still') is None and shutil.which('libcamera-still'):
        cmd[0] = 'libcamera-still'
        
    try:
        with camera_lock:
            result = subprocess.run(cmd, capture_output=True)
        if result.returncode == 0:
            return Response(result.stdout, mimetype='image/jpeg')
        else:
            return f"Capture failed: {result.stderr.decode()}", 500
    except Exception as e:
        return f"Error: {str(e)}", 500

@app.route('/terminal')
def terminal_page():
    return render_template('terminal.html')

@app.route('/api/terminal/run', methods=['POST'])
def run_command():
    data = request.json
    command = data.get('command')
    if not command:
        return jsonify({"error": "No command provided"}), 400
    
    try:
        # Run command with a timeout to prevent hanging
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
        return jsonify({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Command timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

import psutil
import shutil

@app.route('/api/system/status', methods=['GET'])
def get_system_status():
    # CPU Temp (Reads from standard Linux path)
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp = float(f.read().strip()) / 1000
    except:
        temp = None
    
    # Disk Usage for all partitions
    disk_partitions = []
    for part in psutil.disk_partitions():
        if part.fstype:  # Only include partitions with a file system
            try:
                usage = psutil.disk_usage(part.mountpoint)
                disk_partitions.append({
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "total": usage.total // (2**30),
                    "used": usage.used // (2**30),
                    "free": usage.free // (2**30),
                    "percent": usage.percent
                })
            except:
                continue
    
    # Memory Usage
    mem = psutil.virtual_memory()
    
    return jsonify({
        "cpu_temp": temp,
        "disk": disk_partitions,
        "memory": {
            "total": mem.total // (2**20),
            "used": mem.used // (2**20),
            "free": mem.available // (2**20),
            "percent": mem.percent
        }
    })

@app.route('/system')
def system_status_page():
    return render_template('status.html')

@app.route('/api/albums', methods=['GET'])
def get_albums_api():
    albums = downloader.get_albums()
    status = downloader.get_album_status()
    for album in albums:
        album['status'] = status.get(album['id'], "Idle")
    return jsonify(albums)

@app.route('/api/albums', methods=['POST'])
def add_album_api():
    data = request.json
    album_id = data.get('id')
    url = data.get('url')
    
    if not all([album_id, url]):
        return jsonify({"error": "Missing id or url"}), 400
        
    safe_id = "".join([c for c in album_id if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
    safe_id = safe_id.replace(' ', '_')
    
    if not safe_id:
        return jsonify({"error": "Invalid album ID"}), 400

    path = os.path.join('google_photos', safe_id)
    albums = downloader.get_albums()
    for album in albums:
        if album['id'] == album_id:
            return jsonify({"error": "Album with this ID already exists"}), 400
            
    albums.append({"id": album_id, "url": url, "path": path})
    with open(downloader.ALBUMS_FILE, 'w') as f:
        json.dump(albums, f)
        
    return jsonify({"status": "success"})

@app.route('/api/albums/<album_id>', methods=['DELETE'])
def delete_album_api(album_id):
    albums = downloader.get_albums()
    new_albums = [a for a in albums if a['id'] != album_id]
    with open(downloader.ALBUMS_FILE, 'w') as f:
        json.dump(new_albums, f)
    return jsonify({"status": "success"})

if __name__ == '__main__':
    threading.Thread(target=motion_detection_thread, daemon=True).start()
    threading.Thread(target=camera_scheduler_thread, daemon=True).start()
    threading.Thread(target=google_photos_sync_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=5001)
