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
import psutil
import numpy as np
from flask import Flask, jsonify, request, send_from_directory, render_template, Response
from flask_cors import CORS
from croniter import croniter
from datetime import datetime
import downloader
import common
import proximity

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)
CORS(app)

# Use shared objects from common.py
PROJECT_ROOT = common.PROJECT_ROOT
CONFIG_FILE = common.CONFIG_FILE
STATE_FILE = common.STATE_FILE
REMOVE_DIR = common.REMOVE_DIR
MANUAL_ON_FILE = common.MANUAL_ON_FILE
MANUAL_OFF_FILE = common.MANUAL_OFF_FILE
NEXT_IMAGE_FILE = common.NEXT_IMAGE_FILE
PREV_IMAGE_FILE = common.PREV_IMAGE_FILE
NEXT_GROUP_FILE = common.NEXT_GROUP_FILE
SHOW_IMAGE_FILE = common.SHOW_IMAGE_FILE

get_config = common.get_config
set_screen_state = common.set_screen_state
get_hardware_screen_state = common.get_hardware_screen_state

# Motion and Proximity State
presence_data = {
    "last_frame": None,
    "last_presence_time": time.time(),
    "last_proximity_time": 0,
    "screen_state": "on",
    "ble_devices_in_range": 0
}
camera_lock = threading.Lock()
proximity_scanner = None

_pairing_mode_active = False
_pairing_mode_end_time = 0

def _enable_pairing_mode_task(duration=60):
    global _pairing_mode_active, _pairing_mode_end_time
    try:
        logging.info(f"Enabling Bluetooth Pairing Mode for {duration}s...")
        # Ensure bluetooth is powered and discoverable
        subprocess.run(['sudo', 'bluetoothctl', 'power', 'on'], check=False)
        subprocess.run(['sudo', 'bluetoothctl', 'discoverable', 'on'], check=False)
        subprocess.run(['sudo', 'bluetoothctl', 'pairable', 'on'], check=False)
        
        _pairing_mode_active = True
        _pairing_mode_end_time = time.time() + duration
        
        time.sleep(duration)
        
        logging.info("Disabling Bluetooth Pairing Mode (timeout)...")
        subprocess.run(['sudo', 'bluetoothctl', 'discoverable', 'off'], check=False)
        subprocess.run(['sudo', 'bluetoothctl', 'pairable', 'off'], check=False)
        _pairing_mode_active = False
    except Exception as e:
        logging.error(f"Error in Bluetooth pairing mode task: {e}")
        _pairing_mode_active = False

@app.route('/api/proximity/pairing-mode', methods=['POST'])
def enable_pairing_mode_api():
    duration = request.json.get('duration', 60)
    threading.Thread(target=_enable_pairing_mode_task, args=(duration,), daemon=True).start()
    return jsonify({"status": "success", "message": f"Pairing mode enabled for {duration} seconds."})

def on_proximity_detected():
    now = time.time()
    # Cool-down: Only trigger every 5 seconds to avoid flooding
    if now - presence_data["last_proximity_time"] < 5:
        return
    presence_data["last_proximity_time"] = now
    
    presence_data["last_presence_time"] = now
    # Get actual hardware state to ensure we are in sync
    current_hw_state = get_hardware_screen_state()
    
    if current_hw_state == "off":
        # Check manual off override
        if os.path.exists(MANUAL_OFF_FILE):
            logging.info("Proximity detected, but screen is manually set to OFF.")
            return

        # Check schedule - OFF hours are stronger than anything
        if common.is_scheduled_off():
            logging.info("Proximity detected, but screen is currently scheduled to be OFF.")
            return

        logging.info("Proximity detected! Turning screen ON.")
        set_screen_state(True)
        presence_data["screen_state"] = "on"
        common.notify_display_process()
    else:
        # Already ON, just sync state
        presence_data["screen_state"] = "on"

def presence_detection_thread():
    while True:
        try:
            config = get_config()
            weak_machine = config.getboolean('DEFAULT', 'weak_machine', fallback=False)
            
            motion_enabled = config.getboolean('MOTION', 'enabled', fallback=False)
            camera_enabled = config.getboolean('CAMERA', 'enabled', fallback=True)
            proximity_enabled = config.getboolean('PROXIMITY', 'enabled', fallback=False)

            # Sync internal state with actual hardware state at each check
            current_hw_state = get_hardware_screen_state()
            presence_data["screen_state"] = current_hw_state

            if not (motion_enabled and camera_enabled) and not proximity_enabled:
                time.sleep(30 if weak_machine else 5)
                continue
            
            # Update BLE device count if scanner is active
            if proximity_scanner:
                presence_data["ble_devices_in_range"] = proximity_scanner.devices_in_range

            if motion_enabled and camera_enabled:
                # Optimized motion check: Only if not manually OFF and not scheduled OFF
                if os.path.exists(MANUAL_OFF_FILE) or common.is_scheduled_off():
                    time.sleep(10 if weak_machine else 2)
                    continue

                sensitivity = config.getint('MOTION', 'sensitivity', fallback=50)
                auto_sens = config.getboolean('MOTION', 'auto_sensitivity', fallback=False)
                
                effective_sensitivity = sensitivity
                if auto_sens and presence_data["last_frame"] is not None:
                    light = get_avg_light(presence_data["last_frame"])
                    if light < 50:
                        effective_sensitivity = min(100, sensitivity + 30)
                    elif light > 200:
                        effective_sensitivity = max(1, sensitivity - 20)
                
                # Map sensitivity (1-100) to threshold (100-1)
                threshold = max(1, 100 - effective_sensitivity)

                shutter = config.getint('MOTION', 'shutter', fallback=0)
                gain = config.getint('MOTION', 'gain', fallback=0)
                
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
                        
                        if presence_data["last_frame"] is not None:
                            frame_delta = cv2.absdiff(presence_data["last_frame"], frame)
                            thresh = cv2.threshold(frame_delta, threshold, 255, cv2.THRESH_BINARY)[1]
                            thresh = cv2.dilate(thresh, None, iterations=2)
                            
                            changed_pixels = np.sum(thresh) / 255
                            change_percent = (changed_pixels / (frame.shape[0] * frame.shape[1])) * 100
                            
                            if change_percent > 0.5:
                                presence_data["last_presence_time"] = time.time()
                                if current_hw_state == "off":
                                    # Check manual off override and schedule
                                    if not os.path.exists(MANUAL_OFF_FILE) and not common.is_scheduled_off():
                                        set_screen_state(True)
                                        presence_data["screen_state"] = "on"
                                        common.notify_display_process()
                                    else:
                                        logging.debug("Motion detected, but screen is manually OFF or scheduled OFF.")
                        
                        presence_data["last_frame"] = frame
            
            # Check for timeout
            motion_timeout = config.getint('MOTION', 'timeout', fallback=300)
            proximity_timeout = config.getint('PROXIMITY', 'timeout', fallback=300)
            
            effective_timeout = 300
            if motion_enabled and proximity_enabled:
                effective_timeout = max(motion_timeout, proximity_timeout)
            elif motion_enabled:
                effective_timeout = motion_timeout
            elif proximity_enabled:
                effective_timeout = proximity_timeout

            if time.time() - presence_data["last_presence_time"] > effective_timeout:
                if current_hw_state == "on":
                    # Check manual override
                    if not os.path.exists(MANUAL_ON_FILE):
                        logging.info(f"Presence timeout ({effective_timeout}s). Turning screen OFF.")
                        set_screen_state(False)
                        presence_data["screen_state"] = "off"
            
            time.sleep(30 if weak_machine else 2)
        except Exception as e:
            logging.error(f"Error in presence_detection_thread: {e}")
            time.sleep(10)

# Camera Scheduling Thread
def camera_scheduler_thread():
    last_trigger_minute = -1
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
            
            now = datetime.now()
            # Check if current time matches cron expression
            if croniter.match(cron_expr, now):
                # Ensure we only trigger once per minute
                if now.minute != last_trigger_minute:
                    last_trigger_minute = now.minute
                    
                    # Reload config to get freshest settings
                    config = get_config()
                    capture_on_motion = config.getboolean('CAMERA', 'capture_on_motion', fallback=False)
                    
                    should_capture = True
                    if capture_on_motion:
                        # Check if motion occurred in last 60 seconds
                        time_since_motion = time.time() - presence_data["last_presence_time"]
                        if time_since_motion > 60:
                            should_capture = False
                            logging.info(f"Scheduled capture skipped: No motion detected in last 60 seconds (last was {time_since_motion:.1f}s ago).")
                        else:
                            logging.info(f"Motion detected ({time_since_motion:.1f}s ago). Triggering scheduled capture.")
                            
                    if should_capture:
                        capture_mode = config.get('CAMERA', 'capture_mode', fallback='image')
                        if capture_mode == 'video':
                            duration = config.getint('CAMERA', 'capture_video_duration', fallback=10)
                            capture_video_segment(duration)
                        else:
                            burst_count = config.getint('CAMERA', 'capture_burst_count', fallback=20)
                            capture_image(burst_count=burst_count)
            
            # Check every 10 seconds for better responsiveness
            time.sleep(10)
        except Exception as e:
            logging.error(f"Error in camera_scheduler_thread: {e}")
            time.sleep(60)

def capture_image(burst_count=1):
    with camera_lock:
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        config = get_config()
        image_dir = config.get('CAMERA', 'imagedir_captures', fallback=os.path.join(PROJECT_ROOT, 'captures/'))
        os.makedirs(image_dir, exist_ok=True)
        
        still_cmd = 'rpicam-still'
        if shutil.which('rpicam-still') is None and shutil.which('libcamera-still'):
            still_cmd = 'libcamera-still'
            
        if burst_count > 1:
            # Use filename pattern for burst
            filepath_pattern = os.path.join(image_dir, f"capture_{timestamp}_%02d.jpg")
            # For maximum speed, use --timelapse 0 and small resolution
            # This allows the hardware to stream frames to disk as fast as possible.
            # Timeout needs to be long enough to finish all frames.
            timeout_ms = max(5000, burst_count * 500) 
            cmd = [still_cmd, '-n', '--immediate', '--frames', str(burst_count), '--timelapse', '0', '-o', filepath_pattern, '-t', str(timeout_ms), '--width', '640', '--height', '480', '--hflip', '--vflip', '--denoise', 'cdn_off', '--nopreview', '--tuning-file', '/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json']
        else:
            filename = f"capture_{timestamp}.jpg"
            filepath = os.path.join(image_dir, filename)
            cmd = [still_cmd, '-n', '-o', filepath, '-t', '2000', '--width', '1296', '--height', '972', '--hflip', '--vflip', '--tuning-file', '/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json']
            
        try:
            subprocess.run(cmd, check=True)
        except Exception as e:
            logging.error(f"Error capturing image(s): {e}")

def capture_video_segment(duration_seconds=10):
    config = get_config()
    capture_dir = config.get('CAMERA', 'imagedir_captures', fallback='captures/')
    if not os.path.isabs(capture_dir):
        capture_dir = os.path.join(PROJECT_ROOT, capture_dir)
        
    if not os.path.exists(capture_dir):
        os.makedirs(capture_dir)
    
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_file = os.path.join(capture_dir, f"video_{timestamp}.mp4")
    
    # Get current resolution from config
    res = config.get('CAMERA', 'video_resolution', fallback='640x480').split('x')
    width, height = res[0], res[1]
    
    vid_cmd = 'rpicam-vid'
    if shutil.which('rpicam-vid') is None and shutil.which('libcamera-vid'):
        vid_cmd = 'libcamera-vid'
        
    duration_ms = str(duration_seconds * 1000)
    cmd = [vid_cmd, '-t', duration_ms, '--codec', 'libav', '--libav-format', 'mp4', '--width', width, '--height', height, '--bitrate', '5000000', '--hflip', '--vflip', '--tuning-file', '/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json', '-o', output_file, '-n']
    
    def record():
        logging.info(f"Starting scheduled video capture: {output_file} for {duration_seconds}s")
        with camera_lock:
            try:
                # Use a very generous timeout (double the duration) to ensure file is finalized
                subprocess.run(cmd, check=True, timeout=(duration_seconds * 2) + 10)
                logging.info(f"Scheduled video capture complete: {output_file}")
            except Exception as e:
                logging.error(f"Scheduled video capture failed: {e}")

    threading.Thread(target=record).start()

# Google Photos Sync Thread
def google_photos_sync_thread():
    print('Google Photos sync thread started (Hourly Sync)')
    while True:
        try:
            print(f'Starting hourly full sync at {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
            # Perform a full sync of all albums. 
            # force_fast=False ensures bandwidth limits are enforced (4MB/s cap).
            downloader.sync_all(force_fast=False)
            
            print(f'Hourly sync complete. Sleeping for 1 hour...')
            time.sleep(3600)
        except Exception as e:
            print(f'Auto-sync error: {e}')
            time.sleep(600) # Wait 10 minutes before retrying on error

def get_avg_light(frame):
    return np.mean(frame)

@app.route('/api/motion', methods=['GET'])
def get_motion_config():
    config = get_config()
    if not config.has_section('MOTION'):
        return jsonify({"enabled": False, "sensitivity": 50, "auto_sensitivity": False, "timeout": 300})

    current_state = get_hardware_screen_state()
    presence_data["screen_state"] = current_state
    
    sensitivity = config.getint('MOTION', 'sensitivity', fallback=50)
    auto_sens = config.getboolean('MOTION', 'auto_sensitivity', fallback=False)
    
    effective_sensitivity = sensitivity
    if auto_sens and presence_data["last_frame"] is not None:
        light = get_avg_light(presence_data["last_frame"])
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
        "last_movement_seconds_ago": int(time.time() - presence_data["last_presence_time"]),
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
                with open(NEXT_IMAGE_FILE, "w") as f:
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
    all_folders = []
    if os.path.exists(image_dir):
        def _scan_folders(p):
            try:
                with os.scandir(p) as it:
                    for entry in it:
                        if entry.is_dir(follow_symlinks=False):
                            rel_path = os.path.relpath(entry.path, image_dir)
                            all_folders.append(rel_path)
                            
                            # Strictly filter out google_photos folders
                            path_parts = rel_path.split(os.sep)
                            if 'google_photos' not in path_parts and 'google-photos' not in path_parts:
                                folders.append(rel_path)
                            
                            _scan_folders(entry.path)
            except:
                pass
        _scan_folders(image_dir)
    
    return jsonify({
        "available_folders": sorted(folders),
        "all_folders": sorted(all_folders),
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
    with open(SHOW_IMAGE_FILE, "w") as f:
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

    # Dynamically apply proximity scanner state
    if 'PROXIMITY' in data and proximity_scanner:
        if config.getboolean('PROXIMITY', 'enabled', fallback=False):
            proximity_scanner.start()
        else:
            proximity_scanner.stop()

    common.notify_display_process() # Notify to apply config without restart
    return jsonify({"status": "success", "message": "Configuration updated"})

def _restart_frame_process():
    try:
        time.sleep(0.5) # Give the API a moment to respond
        subprocess.run(['sudo', 'systemctl', 'restart', 'frame'], check=True)
    except Exception as e:
        pass

@app.route('/api/proximity/toggle-ignore', methods=['POST'])
def toggle_proximity_ignore():
    data = request.json
    address = data.get('address', '').strip().upper()
    if not address:
        return jsonify({"error": "No address provided"}), 400
    
    config = get_config()
    current_ignored = config.get('PROXIMITY', 'ignored_addresses', fallback='').split(',')
    current_ignored = [a.strip().upper() for a in current_ignored if a.strip()]
    
    if address in current_ignored:
        current_ignored.remove(address)
        action = "removed"
    else:
        current_ignored.append(address)
        action = "added"
    
    config.set('PROXIMITY', 'ignored_addresses', ','.join(current_ignored))
    with open(CONFIG_FILE, 'w') as f:
        config.write(f)
    
    # Notify changes
    common.notify_display_process()
    return jsonify({"status": "success", "action": action, "address": address})

@app.route('/api/restart', methods=['POST'])
def restart_frame():
    restart_thread = threading.Thread(target=_restart_frame_process)
    restart_thread.daemon = True 
    restart_thread.start()
    return jsonify({"status": "success", "message": "Frame service restart initiated."})

@app.route('/api/next', methods=['POST'])
def next_image():
    with open(NEXT_IMAGE_FILE, "w") as f: f.write("next")
    return jsonify({"status": "success"})

@app.route('/api/next-group', methods=['POST'])
def next_group():
    with open(NEXT_GROUP_FILE, "w") as f: f.write("next")
    return jsonify({"status": "success"})

@app.route('/api/prev', methods=['POST'])
def prev_image():
    with open(PREV_IMAGE_FILE, "w") as f: f.write("prev")
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
        
    # Fallback to checking the cached image list
    all_images = common.get_images()
    # Check if filename matches the end of any path or the filename exactly
    for rel_path in all_images:
        if rel_path == filename or os.path.basename(rel_path) == filename:
            full_path = os.path.join(image_dir, rel_path)
            return send_from_directory(os.path.dirname(full_path), os.path.basename(full_path))

    # Also check removed_dir (less common, so we can do a quick check)
    if os.path.exists(remove_dir):
        # We'll do a quick check for the filename in remove_dir without a full walk if possible,
        # but since removed images are structure-preserved, we might need a walk.
        # However, this is a fallback case.
        for root, dirs, files in os.walk(remove_dir):
            if filename in files or any(filename == os.path.relpath(os.path.join(root, f), remove_dir) for f in files):
                 # Find the exact match
                 for f in files:
                     if f == filename or os.path.relpath(os.path.join(root, f), remove_dir) == filename:
                         return send_from_directory(root, f)

    return f"File {filename} not found", 404

@app.route('/api/download/<path:filename>')
def download_image(filename):
    config = get_config()
    image_dir = common.get_image_dir()
    # Try to find the file just like serve_image does
    full_path = os.path.join(image_dir, filename)
    if not (os.path.exists(full_path) and os.path.isfile(full_path)):
        # Check cached list
        all_images = common.get_images()
        found = False
        for rel_path in all_images:
            if rel_path == filename or os.path.basename(rel_path) == filename:
                full_path = os.path.join(image_dir, rel_path)
                found = True
                break
        
        if not found:
             # Check captures dir for camera downloads
             captures_dir = config.get('CAMERA', 'imagedir_captures', fallback=os.path.join(PROJECT_ROOT, 'captures/'))
             full_path = os.path.join(captures_dir, filename)
             if not (os.path.exists(full_path) and os.path.isfile(full_path)):
                 return f"File {filename} not found for download", 404

    return send_from_directory(os.path.dirname(full_path), os.path.basename(full_path), as_attachment=True)

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
                # Manual ON: Create override file, remove OFF override
                with open(MANUAL_ON_FILE, "w") as f: f.write("1")
                if os.path.exists(MANUAL_OFF_FILE):
                    os.remove(MANUAL_OFF_FILE)
            else:
                # Manual OFF: Create override file, remove ON override
                with open(MANUAL_OFF_FILE, "w") as f: f.write("1")
                if os.path.exists(MANUAL_ON_FILE):
                    os.remove(MANUAL_ON_FILE)

            set_screen_state(on)
            presence_data["screen_state"] = state
            if on:
                presence_data["last_presence_time"] = time.time()
            common.notify_display_process()
            return jsonify({"status": "success", "state": state})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        state = get_hardware_screen_state()
        presence_data["screen_state"] = state
        return jsonify({"status": "success", "state": state})

@app.route('/api/internal/screen_state', methods=['POST'])
def sync_screen_state():
    data = request.json
    state = data.get('state')
    if state in ['on', 'off']:
        presence_data["screen_state"] = state
        if state == 'on':
            presence_data["last_presence_time"] = time.time()
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



@app.route('/api/capture_video', methods=['POST'])
def capture_video():
    config = get_config()
    if not config.getboolean('CAMERA', 'enabled', fallback=True):
        return jsonify({"error": "Camera is disabled"}), 404

    # Get current resolution from config to match live video
    res = config.get('CAMERA', 'video_resolution', fallback='640x480').split('x')
    width, height = res[0], res[1]

    capture_dir = config.get('CAMERA', 'imagedir_captures', fallback='captures/')
    if not os.path.isabs(capture_dir):
        capture_dir = os.path.join(PROJECT_ROOT, capture_dir)
        
    if not os.path.exists(capture_dir):
        os.makedirs(capture_dir)
    
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_file = os.path.join(capture_dir, f"video_{timestamp}.mp4")
    
    # Use the same command logic as video_feed
    vid_cmd = 'rpicam-vid'
    if shutil.which('rpicam-vid') is None and shutil.which('libcamera-vid'): vid_cmd = 'libcamera-vid'
    
    cmd = [vid_cmd, '-t', '60000', '--codec', 'libav', '--libav-format', 'mp4', '--width', width, '--height', height,'--bitrate', '5000000', '--hflip', '--vflip', '--tuning-file', '/usr/share/libcamera/ipa/rpi/vc4/ov5647_noir.json',  '-o', output_file, '-n']
    
    def record():
        logging.info(f"Starting video capture: {output_file}")
        with camera_lock:
            try:
                subprocess.run(cmd, check=True, timeout=70)
                logging.info(f"Video capture complete: {output_file}")
            except Exception as e:
                logging.error(f"Video capture failed: {e}")
                
    threading.Thread(target=record).start()
    return jsonify({"status": "started", "file": output_file}), 200

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
            headers = {}
            if request.args.get('download') == '1':
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                headers["Content-Disposition"] = f"attachment; filename=capture_{timestamp}.jpg"
            return Response(result.stdout, mimetype='image/jpeg', headers=headers)
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

def get_dir_size_mb(path):
    total = 0
    if os.path.exists(path):
        try:
            def _scan_size(p):
                size = 0
                with os.scandir(p) as it:
                    for entry in it:
                        if entry.is_file(follow_symlinks=False):
                            size += entry.stat().st_size
                        elif entry.is_dir(follow_symlinks=False):
                            size += _scan_size(entry.path)
                return size
            total = _scan_size(path)
        except:
            pass
    return round(total / (2**20), 2)

@app.route('/api/sync/status', methods=['GET'])
def get_sync_status():
    sync_status_path = common.SYNC_STATUS_FILE
    if os.path.exists(sync_status_path):
        try:
            with open(sync_status_path, 'r') as f:
                status = json.load(f)

            # Add storage breakdown
            image_dir = common.get_image_dir()
            google_photos_dir = os.path.join(image_dir, 'google_photos')

            gp_size = get_dir_size_mb(google_photos_dir)
            total_size = get_dir_size_mb(image_dir)
            local_size = max(0, round(total_size - gp_size, 2))

            status["google_photos_size_mb"] = gp_size
            status["local_folders_size_mb"] = local_size

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
        },
        "ble_devices_in_range": presence_data["ble_devices_in_range"],
        "ble_detailed_devices": proximity_scanner.get_detailed_devices() if proximity_scanner else [],
        "ble_pairing_mode": {
            "active": _pairing_mode_active,
            "remaining": max(0, int(_pairing_mode_end_time - time.time())) if _pairing_mode_active else 0
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
            
        # Trigger immediate sync for the new album at NORMAL speed (NOT fast sync)
        threading.Thread(
            target=downloader.download_album, 
            args=(album_id, url, path, False), 
            daemon=True
        ).start()
        
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
    except Exception as e:
        return jsonify({"error": f"Internal system error while saving configuration: {str(e)}"}), 500
        
    return jsonify({"status": "success"})

@app.route('/api/albums/update', methods=['POST'])
def update_album_settings_api():
    data = request.json
    album_id = data.get('id')
    settings = data.get('settings', {})
    
    albums = downloader.get_albums()
    changed = False
    for album in albums:
        if album['id'] == album_id:
            for key, value in settings.items():
                album[key] = value
            changed = True
            break
            
    if changed:
        with open(downloader.ALBUMS_FILE, 'w') as f:
            json.dump(albums, f)
        return jsonify({"status": "success"})
    return jsonify({"error": "Album not found"}), 404

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
    # sync_album_selection_on_startup()
    proximity_scanner = proximity.ProximityScanner(on_proximity_detected)
    proximity_scanner.start()
    
    threading.Thread(target=presence_detection_thread, daemon=True).start()
    threading.Thread(target=camera_scheduler_thread, daemon=True).start()
    threading.Thread(target=google_photos_sync_thread, daemon=True).start()
    app.run(host='0.0.0.0', port=5001)
