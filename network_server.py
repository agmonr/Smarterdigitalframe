from flask import Flask, jsonify, request, render_template
import subprocess
import json
import os
import time
import threading
import common

app = Flask(__name__, template_folder='templates')

WIFI_WATCHDOG_CHECK_INTERVAL = 15   # how often to poll while connected
WIFI_WATCHDOG_RETRY_INTERVAL = 60   # how often to retry recovery while lost
WIFI_WATCHDOG_RADIO_RESET_EVERY = 5 # after this many failed attempts, reset the radio

def get_config_network():
    config = common.get_config()
    ap_name = "dframe"
    ap_password = "DigitalFrame"
    if 'NETWORK' in config:
        if 'ap_name' in config['NETWORK']:
            ap_name = config['NETWORK']['ap_name']
        if 'ap_password' in config['NETWORK']:
            ap_password = config['NETWORK']['ap_password']
    return ap_name, ap_password

def _get_active_wifi_connection():
    """Returns (is_ap_active, client_ssid) from active nmcli connections."""
    ap_name, _ = get_config_network()
    try:
        res = subprocess.run(['nmcli', '-t', '-f', 'TYPE,NAME', 'con', 'show', '--active'],
                              capture_output=True, text=True, timeout=5)
        is_ap_active = False
        client_ssid = None
        for line in res.stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 2 and parts[0].strip() in ('802-11-wireless', 'wifi'):
                name = parts[1].strip()
                if name in (ap_name, 'DigitalFrame_Setup'):
                    is_ap_active = True
                else:
                    client_ssid = name
        return is_ap_active, client_ssid
    except Exception:
        return False, None

def _get_wifi_device():
    try:
        res = subprocess.run(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'dev'],
                              capture_output=True, text=True, timeout=5)
        for line in res.stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) == 2 and parts[1] == 'wifi':
                return parts[0]
    except Exception:
        pass
    return None

def _attempt_wifi_recovery(device, attempt, log):
    log(f"Recovery attempt #{attempt} on device {device}: nmcli device connect")
    try:
        subprocess.run(['sudo', 'nmcli', 'device', 'connect', device],
                        capture_output=True, text=True, timeout=20)
    except Exception as e:
        log(f"nmcli device connect failed: {e}")

    # Milder reconnect attempts don't always clear a stuck radio/driver state.
    # Every few failed attempts, escalate to toggling the radio off/on.
    if attempt % WIFI_WATCHDOG_RADIO_RESET_EVERY == 0:
        log(f"Still lost after {attempt} attempts - resetting wifi radio")
        try:
            subprocess.run(['sudo', 'nmcli', 'radio', 'wifi', 'off'], capture_output=True, text=True, timeout=10)
            time.sleep(2)
            subprocess.run(['sudo', 'nmcli', 'radio', 'wifi', 'on'], capture_output=True, text=True, timeout=10)
            time.sleep(3)
            subprocess.run(['sudo', 'nmcli', 'device', 'connect', device], capture_output=True, text=True, timeout=20)
        except Exception as e:
            log(f"Radio reset failed: {e}")

def wifi_watchdog_loop():
    """Runs forever in a background thread: detects loss of the *client*
    wifi connection (an intentional AP-setup session is not a failure) and
    tries to recover it, retrying every WIFI_WATCHDOG_RETRY_INTERVAL seconds
    until reconnected. Publishes connect/lost state via common.set_wifi_status()
    so display.py can show a 'wifi lost' icon."""
    log_file = "logs/wifi_watchdog.log"
    def log(msg):
        try:
            with open(log_file, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
        except Exception:
            pass
        print(msg)

    was_connected = True
    failure_count = 0

    while True:
        try:
            is_ap_active, client_ssid = _get_active_wifi_connection()

            if is_ap_active:
                # Intentional setup/hotspot mode - not a connectivity failure.
                common.set_wifi_status(True)
                was_connected = True
                failure_count = 0
                time.sleep(WIFI_WATCHDOG_CHECK_INTERVAL)
                continue

            connected = client_ssid is not None

            if connected:
                if not was_connected:
                    log(f"WiFi connectivity restored ({client_ssid}) after {failure_count} attempt(s).")
                common.set_wifi_status(True)
                was_connected = True
                failure_count = 0
                time.sleep(WIFI_WATCHDOG_CHECK_INTERVAL)
            else:
                if was_connected:
                    log("WiFi connectivity lost. Starting recovery attempts.")
                common.set_wifi_status(False)
                was_connected = False
                failure_count += 1

                device = _get_wifi_device()
                if device:
                    _attempt_wifi_recovery(device, failure_count, log)
                else:
                    log("No wifi device found; cannot attempt recovery.")
                time.sleep(WIFI_WATCHDOG_RETRY_INTERVAL)
        except Exception as e:
            log(f"Watchdog loop error: {e}")
            time.sleep(WIFI_WATCHDOG_RETRY_INTERVAL)

@app.route('/network')
def network_page():
    return render_template('network.html')

@app.route('/api/network/status', methods=['GET'])
def get_status():
    try:
        ap_name, _ = get_config_network()
        # Check active connections directly
        con_res = subprocess.run(['nmcli', '-t', '-f', 'TYPE,NAME', 'con', 'show', '--active'], capture_output=True, text=True)
        
        is_ap_active = False
        actual_ssid = None
        
        for line in con_res.stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 2:
                con_type = parts[0].strip()
                con_name = parts[1].strip()
                
                if con_type in ['802-11-wireless', 'wifi']:
                    if con_name in [ap_name, 'DigitalFrame_Setup']:
                        is_ap_active = True
                    else:
                        actual_ssid = con_name
                        break
        
        if actual_ssid:
            return jsonify({"ssid": actual_ssid})
        if is_ap_active:
            return jsonify({"ssid": "Access Point"})
            
        return jsonify({"ssid": "Not connected"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/network/scan', methods=['GET'])
def scan_networks():
    try:
        ap_name, _ = get_config_network()
        # Run nmcli scan
        result = subprocess.run(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY', 'dev', 'wifi', 'list'], capture_output=True, text=True, check=True)
        networks = []
        for line in result.stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 3:
                ssid = parts[0]
                # Filter out empty SSIDs and the setup AP name
                if ssid and ssid not in [ap_name, 'DigitalFrame_Setup']:
                    networks.append({"ssid": ssid, "signal": parts[1], "security": parts[2]})
        # Remove duplicates
        unique_networks = {n['ssid']: n for n in networks}.values()
        return jsonify({"status": "success", "networks": list(unique_networks)})
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"Failed to scan: {e.stderr}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/network/wifi', methods=['POST'])
def configure_wifi():
    data = request.json
    ssid = data.get('ssid')
    password = data.get('password')
    
    if not ssid:
        return jsonify({"error": "Missing SSID"}), 400

    log_file = "logs/network_debug.log"
    def log(msg):
        with open(log_file, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
        print(msg)

    log(f"--- Starting connection attempt to SSID: {ssid} via wifi_setup.py ---")

    try:
        # 1. Kill DHCP and other setup processes
        log("Stopping DHCP server...")
        subprocess.run(['sudo', 'pkill', '-9', '-f', 'dnsmasq'], capture_output=True)
        
        # 2. Call wifi_setup.py with the new SSID and password
        import sys
        # Use the same python interpreter
        cmd = [sys.executable, 'wifi_setup.py', '--ssid', ssid, '--password', password]
        log(f"Running command: {' '.join(cmd)}")
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        log(f"wifi_setup.py exit code: {result.returncode}")
        # Only log stdout/stderr if they aren't too huge, but usually they are fine
        if result.stdout:
            log(f"wifi_setup.py stdout: {result.stdout.strip()}")
        if result.stderr:
            log(f"wifi_setup.py stderr: {result.stderr.strip()}")

        if result.returncode == 0:
            log("Connection successful via wifi_setup.py.")
            return jsonify({"status": "success", "message": f"Connected to {ssid}"})
        else:
            log(f"Connection failed via wifi_setup.py with code {result.returncode}")
            return jsonify({"error": f"Failed to connect: {result.stderr or result.stdout}"}), 500

    except Exception as e:
        log(f"CRITICAL ERROR: {str(e)}")
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    threading.Thread(target=wifi_watchdog_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5003)
