import subprocess
import os
import time
import sys
import configparser
import argparse

NM_CONF_DIR = "/etc/NetworkManager/system-connections/"
BACKUP_DIR = "/tmp/nm_backup"
FAILED_DIR = "/tmp/failed_wifi_configs"
FLAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.wifi_configured')

def run_command(cmd, check=False, shell=False):
    try:
        if shell:
            result = subprocess.run(cmd, capture_output=True, text=True, check=check, shell=True)
        else:
            result = subprocess.run(cmd, capture_output=True, text=True, check=check)
        return result
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {cmd}\nError: {e.stderr}")
        return e

def backup_configs():
    print(f"Backing up existing configurations to {BACKUP_DIR}...")
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)
    
    # Check if there are any files to move
    # We use sudo ls to ensure we can see the files
    res = run_command(f"sudo ls -A {NM_CONF_DIR}", shell=True)
    if not isinstance(res, Exception) and res.returncode == 0 and res.stdout.strip():
        # Move all files from system-connections to backup
        # Use shell=True to handle the wildcard *
        run_command(f"sudo mv {NM_CONF_DIR}* {BACKUP_DIR}/", shell=True)
    else:
        print("No configurations to backup or error accessing directory.")

def restore_configs():
    print(f"Restoring configurations from {BACKUP_DIR}...")
    if not os.path.exists(BACKUP_DIR):
        print("Backup directory not found. Nothing to restore.")
        return
    
    res = run_command(f"ls -A {BACKUP_DIR}", shell=True)
    if not isinstance(res, Exception) and res.returncode == 0 and res.stdout.strip():
        run_command(f"sudo mv {BACKUP_DIR}/* {NM_CONF_DIR}", shell=True)
    
    # Cleanup backup dir
    try:
        if os.path.exists(BACKUP_DIR):
            run_command(f"sudo rm -rf {BACKUP_DIR}", shell=True)
    except:
        pass

def move_failed_configs():
    print(f"Moving failed configurations to {FAILED_DIR} for debugging...")
    if not os.path.exists(FAILED_DIR):
        os.makedirs(FAILED_DIR)
    
    # Use sudo ls to check for files
    res = run_command(f"sudo ls -A {NM_CONF_DIR}", shell=True)
    if not isinstance(res, Exception) and res.returncode == 0 and res.stdout.strip():
        # Move files to failed dir, timestamped
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        target = os.path.join(FAILED_DIR, timestamp)
        run_command(f"sudo mkdir -p {target}", shell=True)
        run_command(f"sudo mv {NM_CONF_DIR}* {target}/", shell=True)
    else:
        print("No failed configurations found to move.")

def create_wifi_config(ssid, password):
    print(f"Creating configuration for {ssid}...")
    # Add the connection profile
    res = run_command([
        'sudo', 'nmcli', 'con', 'add', 'type', 'wifi', 'con-name', ssid, 'ifname', '*', 'ssid', ssid
    ])
    if isinstance(res, Exception) or res.returncode != 0:
        return res
    
    # Configure security
    run_command(['sudo', 'nmcli', 'con', 'modify', ssid, 
                 '802-11-wireless-security.key-mgmt', 'wpa-psk',
                 '802-11-wireless-security.psk', password,
                 '802-11-wireless-security.psk-flags', '0',
                 'connection.permissions', ''])
    return res

def connect_to_wifi(ssid, password):
    print(f"--- Starting connection attempt to SSID: {ssid} ---")
    
    # 1. Move all wifi config files to a new place
    backup_configs()
    
    # 2. Restart network manager
    print("Restarting NetworkManager...")
    run_command(['sudo', 'systemctl', 'restart', 'NetworkManager'])
    time.sleep(2)
    
    # 3. Create the config file
    res = create_wifi_config(ssid, password)
    if isinstance(res, Exception) or res.returncode != 0:
        error_msg = res.stderr if not isinstance(res, Exception) else str(res)
        print(f"Failed to create config: {error_msg}")
        move_failed_configs()
        restore_configs()
        run_command(['sudo', 'systemctl', 'restart', 'NetworkManager'])
        return False

    # 4. Restart the service again
    print("Restarting NetworkManager to load new config...")
    run_command(['sudo', 'systemctl', 'restart', 'NetworkManager'])
    
    # Wait for autoconnect or try explicit up
    print("Waiting 5 seconds for connection...")
    time.sleep(5)
    
    ap_name, _ = get_config_network()
    if is_wifi_connected(ap_name):
        print("Connection successful.")
        with open(FLAG_FILE, 'w') as f:
            f.write('configured')
        return True
    else:
        print("Not automatically connected. Trying manual up...")
        res_up = run_command(['sudo', 'nmcli', 'con', 'up', ssid])
        
        if not isinstance(res_up, Exception) and res_up.returncode == 0:
            print("Manual connection successful.")
            with open(FLAG_FILE, 'w') as f:
                f.write('configured')
            return True
        else:
            error_msg = res_up.stderr if not isinstance(res_up, Exception) else str(res_up)
            print(f"Connection failed: {error_msg}")
            
            # 5. Move the failed config files to /tmp/ for debug
            move_failed_configs()
            
            # 6. Bring back the old config files and restart network manager
            restore_configs()
            print("Restarting NetworkManager after rollback...")
            run_command(['sudo', 'systemctl', 'restart', 'NetworkManager'])
            return False

def get_config_network():
    config = configparser.ConfigParser()
    ap_name = "dframe"
    ap_password = "DigitalFrame"
    try:
        config.read('config.ini')
        if 'NETWORK' in config:
            if 'ap_name' in config['NETWORK']:
                ap_name = config['NETWORK']['ap_name']
            if 'ap_password' in config['NETWORK']:
                ap_password = config['NETWORK']['ap_password']
    except Exception as e:
        print(f"Error reading config.ini: {e}")
    return ap_name, ap_password

def get_wifi_interface():
    res = run_command(['nmcli', '-t', '-f', 'DEVICE,TYPE', 'dev'])
    if isinstance(res, Exception): return None
    for line in res.stdout.strip().split('\n'):
        parts = line.split(':')
        if len(parts) == 2 and parts[1] == 'wifi':
            return parts[0]
    return None

def is_wifi_connected(ap_name):
    res = run_command(['nmcli', '-t', '-f', 'DEVICE,ACTIVE,SSID', 'dev', 'wifi'])
    if isinstance(res, Exception): return False
    for line in res.stdout.strip().split('\n'):
        parts = line.split(':')
        if len(parts) >= 3 and parts[1].lower() == 'yes':
            ssid = parts[2]
            if ssid not in [ap_name, 'DigitalFrame_Setup']:
                return True
    return False

def unblock_wifi():
    print("Checking for rfkill blocks...")
    rfkill_path = None
    for path in ['/usr/sbin/rfkill', '/usr/bin/rfkill']:
        if os.path.exists(path):
            rfkill_path = path
            break
    
    if rfkill_path:
        print("Unblocking wifi via rfkill...")
        run_command(['sudo', rfkill_path, 'unblock', 'wifi'])
    else:
        # Try calling it directly if not in common paths but in PATH
        try:
            run_command(['sudo', 'rfkill', 'unblock', 'wifi'])
        except Exception:
            print("rfkill not found, skipping.")

def setup_adhoc():
    unblock_wifi()
    iface = get_wifi_interface()
    if not iface:
        print("No wifi interface found.")
        return

    ap_name, ap_password = get_config_network()
    
    if os.path.exists(FLAG_FILE):
        print("Flag file exists. Skipping AP setup.")
        return
    
    if is_wifi_connected(ap_name):
        print("Wifi already connected. Skipping AP setup.")
        return

    print(f"No active wifi connection. Setting up Access Point on {iface}...")
    
    # Remove old connections if they exist
    run_command(['sudo', 'nmcli', 'con', 'delete', ap_name])
    run_command(['sudo', 'nmcli', 'con', 'delete', 'DigitalFrame_Setup'])
    
    # Create AP connection
    # AP mode is much more compatible with modern devices than ad-hoc.
    res = run_command([
        'sudo', 'nmcli', 'con', 'add', 'type', 'wifi', 'ifname', iface, 'con-name', ap_name, 
        'autoconnect', 'yes', 'ssid', ap_name, 'mode', 'ap'
    ], check=False)
    
    if isinstance(res, subprocess.CalledProcessError) or (hasattr(res, 'returncode') and res.returncode != 0):
        print("Failed to create AP connection. The hardware might not support it.")
        return

    # Configure with WPA2 security and password from config
    run_command(['sudo', 'nmcli', 'con', 'modify', ap_name, 
                 '802-11-wireless-security.key-mgmt', 'wpa-psk',
                 '802-11-wireless-security.psk', ap_password,
                 '802-11-wireless-security.psk-flags', '0',
                 'connection.permissions', '',
                 'ipv4.method', 'manual', 
                 'ipv4.addresses', '192.168.4.1/24',
                 'ipv6.method', 'ignore'])
    
    print(f"Bringing up {ap_name} (AP Mode) with password '{ap_password}'...")
    # Try to bring up with retries
    success = False
    for i in range(3):
        # Use --ask and pipe the password to handle 'Secrets were required'
        res = subprocess.run(['sudo', 'nmcli', '--ask', 'con', 'up', ap_name], 
                             input=f"{ap_password}\n", capture_output=True, text=True)
        
        if res.returncode != 0:
            print(f"Piped activation failed for {ap_name}. Trying with passwd-file...")
            import tempfile
            fd, pw_file = tempfile.mkstemp()
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(f"802-11-wireless-security.psk:{ap_password}\n")
                res = subprocess.run(['sudo', 'nmcli', 'con', 'up', ap_name, 'passwd-file', pw_file], 
                                     capture_output=True, text=True)
            finally:
                if os.path.exists(pw_file):
                    os.remove(pw_file)

        if res.returncode == 0:
            success = True
            break
        print(f"Retry {i+1} to bring up AP: {res.stderr.strip()}")
        time.sleep(2)

    if not success:
        print("Failed to bring up AP connection.")
        # Try a simpler way as last resort
        subprocess.run(['sudo', 'nmcli', '--ask', 'dev', 'wifi', 'hotspot', 'ifname', iface, 'ssid', ap_name], 
                       input=f"{ap_password}\n", capture_output=True, text=True)
        return

    # Start dnsmasq
    print("Starting dnsmasq for DHCP...")
    # Kill any existing dnsmasq for this setup
    run_command(['sudo', 'pkill', '-9', '-f', f'dnsmasq.*{ap_name}'])
    run_command(['sudo', 'pkill', '-9', '-f', 'dnsmasq.*DigitalFrame_Setup'])
    
    dnsmasq_cmd = [
        'sudo', '/usr/sbin/dnsmasq', 
        '--interface=' + iface, 
        '--dhcp-range=192.168.4.10,192.168.4.100,255.255.255.0', 
        '--keep-in-foreground', 
        '--bind-interfaces',
        '--conf-file=' # No config file
    ]
    
    try:
        # Run in background
        subprocess.Popen(dnsmasq_cmd)
        print(f"Access Point '{ap_name}' is active. Password: {ap_password}, IP: 192.168.4.1")
    except Exception as e:
        print(f"Failed to start dnsmasq: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='WiFi Setup and Connection Utility')
    parser.add_argument('--ssid', help='SSID to connect to')
    parser.add_argument('--password', help='Password for the SSID')
    
    args = parser.parse_args()
    
    if args.ssid:
        if not args.password:
            print("Password is required when SSID is provided.")
            sys.exit(1)
        success = connect_to_wifi(args.ssid, args.password)
        if not success:
            sys.exit(1)
    else:
        setup_adhoc()
