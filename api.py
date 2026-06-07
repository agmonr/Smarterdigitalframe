import logging; logging.basicConfig(level=logging.INFO, force=True); logging.getLogger("werkzeug").setLevel(logging.INFO)
import os
import json
import re
import configparser
import subprocess
import signal
import shutil
import cv2
import io
import threading
import time
import requests
import numpy as np
from flask import Flask, jsonify, request, send_from_directory, render_template, Response
from flask_cors import CORS
from croniter import croniter
from datetime import datetime
import downloader
import common

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)
CORS(app)

# Use shared objects from common.py
PROJECT_ROOT = common.PROJECT_ROOT
CONFIG_FILE = common.CONFIG_FILE
STATE_FILE = common.STATE_FILE
REMOVE_DIR = common.REMOVE_DIR

get_config = common.get_config
set_screen_state = common.set_screen_state
get_hardware_screen_state = common.get_hardware_screen_state

# Motion Detection State
motion_data = {
    "last_frame": None,
    "last_movement_time": time.time(),
    "screen_state": "on"
}
camera_lock = threading.Lock()

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
    last_sync_time = 0
    while True:
        try:
            # 1. Determine currently playing album
            current_album_id = None
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, 'r') as f:
                        state = json.load(f)
                        current_img = state.get('current_image', '')
                        if 'google_photos/' in current_img:
                            current_album_id = current_img.split('google_photos/')[1].split('/')[0]
                except:
                    pass

            # 2. Find next album in queue
            albums = downloader.get_albums()
            if albums:
                next_album = None
                if current_album_id:
                    # Find index of current album
                    for i, album in enumerate(albums):
                        if album['id'] == current_album_id or os.path.basename(album['path']) == current_album_id:
                            next_album = albums[(i + 1) % len(albums)]
                            break
                
                if not next_album:
                    next_album = albums[0]

                # 3. Sync the "next" album
                # We sync it if it's been a while since the last global sync, 
                # OR if we want to ensure the next one is always ready.
                # Let's sync one album every cycle to keep it "pipelined".
                downloader.download_album(next_album['id'], next_album['url'], next_album['path'])
            
            # 4. Wait before next check. Pipelining means we stay ahead of playback.
            # If a group is 20 images and interval is 10s, that's 200s per group.
            # Checking every 60s is plenty.
            time.sleep(60)
        except Exception as e:
            print(f'Auto-sync error: {e}')
            time.sleep(60)

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
    try:
        history = common.get_history()
        return jsonify(history)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/remove', methods=['POST'])
def remove_image():
    data = request.json
    filename = data.get('filename')
    path = data.get('path') # Optional: relative path from imagedir
    
    if not filename and not path:
        return jsonify({"error": "No filename or path provided"}), 400
    
    config = get_config()
    image_dir = common.get_image_dir()
    remove_dir = config.get('DEFAULT', 'removedir', fallback=REMOVE_DIR)
    
    src = None
    if path:
        # If path is provided, try direct join first
        potential_src = os.path.join(image_dir, path)
        if os.path.exists(potential_src):
            src = potential_src
    
    # Fallback to searching if path didn't work or wasn't provided
    if not src:
        for root, dirs, files in os.walk(image_dir):
            if filename in files:
                src = os.path.join(root, filename)
                break
            
    if src and os.path.exists(src):
        try:
            # Determine relative path for preserving structure in removed/
            rel_path = os.path.relpath(src, image_dir)
            
            # Check if this is the currently displayed image
            is_current = False
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, 'r') as f:
                        state = json.load(f)
                        current_image = state.get('current_image', '')
                        # Match against both filename and relative path
                        if filename == os.path.basename(current_image) or rel_path == current_image:
                            is_current = True
                except:
                    pass

            # Remove from history
            common.delete_from_history(filename)

            # Move the file preserving subfolder structure
            dst = os.path.join(remove_dir, rel_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            
            # If destination exists, add timestamp to avoid collision
            if os.path.exists(dst):
                base, ext = os.path.splitext(dst)
                dst = f"{base}_{int(time.time())}{ext}"
                
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
    image_dir = common.get_image_dir()
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
    restart_display_service()
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
        image_dir = common.get_image_dir()
        
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
    restart_display_service() # Restart to apply config
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

@app.route('/api/next-group', methods=['POST'])
def next_group():
    with open("next_group.tmp", "w") as f: f.write("next")
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
    image_dir = common.get_image_dir()
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

@app.route('/api/sync/status', methods=['GET'])
def get_sync_status():
    sync_status_path = common.SYNC_STATUS_FILE
    if os.path.exists(sync_status_path):
        try:
            with open(sync_status_path, 'r') as f:
                status = json.load(f)
            
            # Add "Next Up" info based on current playback
            current_album_id = None
            if os.path.exists(STATE_FILE):
                try:
                    with open(STATE_FILE, 'r') as f:
                        state = json.load(f)
                        current_img = state.get('current_image', '')
                        if 'google_photos/' in current_img:
                            current_album_id = current_img.split('google_photos/')[1].split('/')[0]
                except:
                    pass

            albums = downloader.get_albums()
            next_album_obj = None
            if albums:
                if current_album_id:
                    for i, album in enumerate(albums):
                        if album['id'] == current_album_id or os.path.basename(album['path']) == current_album_id:
                            next_album_obj = albums[(i + 1) % len(albums)]
                            break
                if not next_album_obj:
                    next_album_obj = albums[0]

                status["next_album"] = next_album_obj["id"]
                
                # Try to find a thumbnail for next album
                next_path = os.path.join(PROJECT_ROOT, "images", next_album_obj["path"])
                if os.path.exists(next_path):
                    files = sorted([f for f in os.listdir(next_path) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
                    if files:
                        status["next_thumbnail"] = f"/api/image/{next_album_obj['path']}/{files[0]}"

            return jsonify(status)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"operation": "Idle", "message": "No sync activity recorded."})

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
    status_data = downloader.get_album_status()
    
    config = get_config()
    image_dir = common.get_image_dir()

    for album in albums:
        stat = status_data.get(album['id'], "Idle")
        if isinstance(stat, dict):
            album['status'] = stat.get('status', 'Idle')
            album['last_sync'] = stat.get('last_sync')
            album['file_count'] = stat.get('file_count')
            album['size_mb'] = stat.get('size_mb')
        else:
            album['status'] = stat
            album['file_count'] = None
            album['size_mb'] = None

        # If stats are missing, try to calculate them on the fly
        if album['file_count'] is None:
            album_path = os.path.join(image_dir, album['path'])
            if os.path.exists(album_path):
                try:
                    files = [f for f in os.listdir(album_path) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
                    album['file_count'] = len(files)
                    album['size_mb'] = round(sum(os.path.getsize(os.path.join(album_path, f)) for f in files) / (2**20), 2)
                except:
                    album['file_count'] = 0
                    album['size_mb'] = 0
            else:
                album['file_count'] = 0
                album['size_mb'] = 0
                
    return jsonify(albums)

@app.route('/api/albums/sync', methods=['POST'])
def trigger_sync_api():
    threading.Thread(target=downloader.sync_all, args=(True,), daemon=True).start()
    return jsonify({"status": "success", "message": "Global sync initiated"})

@app.route('/api/albums', methods=['POST'])
def add_album_api():
    data = request.json
    album_id = data.get('id', '').strip()
    url = data.get('url', '').strip()
    
    if not album_id or not url:
        return jsonify({"error": "Both Album Name (ID) and URL are required."}), 400
        
    # Strictly allow only English alphanumeric characters, dots, underscores, and hyphens.
    # No spaces, no non-English characters.
    if not re.match(r'^[a-zA-Z0-9._-]+$', album_id):
        return jsonify({"error": f"Invalid Album ID '{album_id}'. It must only contain English letters, numbers, dots, underscores, or hyphens (no spaces or special characters)."}), 400

    # Validate URL
    try:
        # Use GET instead of HEAD for some shorteners that might block HEAD
        response = requests.get(url, timeout=10, allow_redirects=True, stream=True)
        if response.status_code >= 400:
            return jsonify({"error": f"URL unreachable: Received HTTP {response.status_code}. Please ensure the album is shared publicly and the link is correct."}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to validate URL: {str(e)}"}), 400

    path = os.path.join('google_photos', album_id)
    albums = downloader.get_albums()
    for album in albums:
        if album['id'] == album_id:
            return jsonify({"error": f"An album with ID '{album_id}' already exists."}), 400
    
    try:
        albums.append({"id": album_id, "url": url, "path": path})
        with open(downloader.ALBUMS_FILE, 'w') as f:
            json.dump(albums, f)
        
        # Auto-add to selected_folders if not 'all'
        config = get_config()
        selected = config.get('DEFAULT', 'selected_folders', fallback='all')
        if selected != 'all':
            current_selected = [s.strip() for s in selected.split(',') if s.strip()]
            if path not in current_selected:
                current_selected.append(path)
                config.set('DEFAULT', 'selected_folders', ",".join(current_selected))
                with open(CONFIG_FILE, 'w') as f:
                    config.write(f)
                restart_display_service() # Restart to apply folder change
    except Exception as e:
        return jsonify({"error": f"Internal system error while saving configuration: {str(e)}"}), 500
        
    return jsonify({"status": "success"})

@app.route('/api/albums/<album_id>', methods=['DELETE'])
def delete_album_api(album_id):
    albums = downloader.get_albums()
    album_to_delete = next((a for a in albums if a['id'] == album_id), None)
    
    if album_to_delete:
        # 1. Physically delete the images from the filesystem
        config = get_config()
        image_dir = common.get_image_dir()
        full_path = os.path.join(image_dir, album_to_delete['path'])
        
        if os.path.exists(full_path):
            try:
                shutil.rmtree(full_path)
                # Also try to remove the parent 'google_photos' if it's empty
                parent = os.path.dirname(full_path)
                if os.path.exists(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except Exception as e:
                logger.error(f"Error deleting album files for {album_id}: {e}")

    # 2. Remove from albums.json
    new_albums = [a for a in albums if a['id'] != album_id]
    with open(downloader.ALBUMS_FILE, 'w') as f:
        json.dump(new_albums, f)
    
    # Remove from selected_folders if present
    if album_to_delete:
        config = get_config()
        selected = config.get('DEFAULT', 'selected_folders', fallback='all')
        if selected != 'all':
            path = album_to_delete['path']
            current_selected = [s.strip() for s in selected.split(',') if s.strip()]
            if path in current_selected:
                current_selected.remove(path)
                # If no folders left, default to 'all' or keep it empty as per project convention.
                # Project seems to use 'all' as fallback.
                new_value = ",".join(current_selected) if current_selected else 'all'
                config.set('DEFAULT', 'selected_folders', new_value)
                with open(CONFIG_FILE, 'w') as f:
                    config.write(f)
                restart_display_service()
        
    # 3. Clean up status
    status = downloader.get_album_status()
    if album_id in status:
        del status[album_id]
        with open(downloader.STATUS_FILE, 'w') as f:
            json.dump(status, f)

    return jsonify({"status": "success"})

def sync_album_selection_on_startup():
    """Ensure all Google Photos albums from albums.json are in the selected_folders config."""
    try:
        albums = downloader.get_albums()
        if not albums:
            return
            
        config = get_config()
        selected = config.get('DEFAULT', 'selected_folders', fallback='all')
        
        # If set to 'all', they are already included
        if selected == 'all':
            return
            
        current_selected = [s.strip() for s in selected.split(',') if s.strip()]
        changed = False
        
        for album in albums:
            path = album['path']
            if path not in current_selected:
                current_selected.append(path)
                changed = True
        
        if changed:
            config.set('DEFAULT', 'selected_folders', ",".join(current_selected))
            with open(CONFIG_FILE, 'w') as f:
                config.write(f)
            logger.info("Startup: Synchronized Google Photos albums with folder selection.")
    except Exception as e:
        logger.error(f"Error during startup album sync: {e}")

if __name__ == '__main__':
    sync_album_selection_on_startup()
    threading.Thread(target=motion_detection_thread, daemon=True).start()
    threading.Thread(target=camera_scheduler_thread, daemon=True).start()
    threading.Thread(target=google_photos_sync_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=5001)
