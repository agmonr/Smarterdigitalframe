import asyncio
import statistics
import threading
import time
import logging
from datetime import datetime, timedelta
from bleak import BleakScanner
import common

# Tracking dictionary
# {address: {"rssi_history": [], "first_seen": datetime, "last_seen": datetime, "name": str, "is_dynamic": bool, "ignore_reason": str}}
device_registry = {}

def rssi_to_distance(rssi):
    """Estimate distance in meters from RSSI using a standard path-loss model."""
    if rssi >= 0: return 0.1
    # Formula: distance = 10 ^ ((Measured Power - RSSI) / (10 * N))
    # Measured Power (RSSI at 1m): -60
    # N (Path loss exponent): 2.5
    return round(10 ** ((-60 - rssi) / 25), 1)

class ProximityScanner:
    def __init__(self, callback_on_detection):
        self.callback_on_detection = callback_on_detection
        self.scanner = None
        self.running = False
        self.thread = None
        self.loop = None
        self.devices_in_range = 0
        self.logger = common.setup_logger('proximity', 'proximity.log')

    def is_device_dynamic(self, addr, name, rssi, config, advertisement_data=None):
        stable_threshold = config.getfloat('PROXIMITY', 'stable_threshold', fallback=0.5)
        min_samples = config.getint('PROXIMITY', 'min_samples', fallback=10)
        ignore_after_hours = config.getint('PROXIMITY', 'ignore_after_hours', fallback=2)
        ignored_list = config.get('PROXIMITY', 'ignored_addresses', fallback='').split(',')
        ignored_list = [a.strip().upper() for a in ignored_list if a.strip()]
        
        now = datetime.now()
        
        if addr not in device_registry:
            device_registry[addr] = {
                "rssi_history": [rssi], 
                "first_seen": now, 
                "last_seen": now, 
                "name": name or "Unknown",
                "is_dynamic": True,
                "ignore_reason": ""
            }

            # 1. Smart Tag Filter: Check for known tracker keywords in name
            tag_keywords = ["TAG", "TILE", "NUT", "TRACKER", "BEACON", "G-TAG", "ITAG"]
            upper_name = (name or "").upper()
            if any(k in upper_name for k in tag_keywords):
                device_registry[addr]["is_dynamic"] = False
                device_registry[addr]["ignore_reason"] = "Smart Tag (Name)"
                return False

            # 2. Smart Tag Filter: Check manufacturer data (Apple=0x004c, Samsung=0x0075)
            if advertisement_data and advertisement_data.manufacturer_data:
                m_ids = advertisement_data.manufacturer_data.keys()
                # Apple AirTags and Samsung SmartTags often use these IDs
                if 0x004c in m_ids or 0x0075 in m_ids:
                    # Heuristic: if it's Apple/Samsung but has no friendly name, it's likely a tag or system device
                    if not name or name == addr:
                        device_registry[addr]["is_dynamic"] = False
                        device_registry[addr]["ignore_reason"] = "Smart Tag (MFG Data)"
                        return False

            # Initial check for manual ignore
            if addr.upper() in ignored_list:
                device_registry[addr]["is_dynamic"] = False
                device_registry[addr]["ignore_reason"] = "Manually Ignored"
            return device_registry[addr]["is_dynamic"]
        
        device_registry[addr]["last_seen"] = now
        if name: device_registry[addr]["name"] = name
        
        # Check manual ignore first
        if addr.upper() in ignored_list:
            device_registry[addr]["is_dynamic"] = False
            device_registry[addr]["ignore_reason"] = "Manually Ignored"
            return False

        # If already flagged as a tag, keep it ignored
        if device_registry[addr]["ignore_reason"].startswith("Smart Tag"):
            return False

        # Check hard limit
        if now - device_registry[addr]["first_seen"] > timedelta(hours=ignore_after_hours):
            device_registry[addr]["is_dynamic"] = False
            device_registry[addr]["ignore_reason"] = f"Seen > {ignore_after_hours}h"
            return False
        
        # Add new RSSI reading
        history = device_registry[addr]["rssi_history"]
        history.append(rssi)
        if len(history) > 20: history.pop(0)
        
        if len(history) >= min_samples:
            stdev = statistics.stdev(history)
            if stdev < stable_threshold:
                device_registry[addr]["is_dynamic"] = False
                device_registry[addr]["ignore_reason"] = "Stable signal (Static)"
                return False
        
        device_registry[addr]["is_dynamic"] = True
        device_registry[addr]["ignore_reason"] = ""
        return True

    def ble_callback(self, device, advertisement_data):
        try:
            config = common.get_config()
            if not config.getboolean('PROXIMITY', 'enabled', fallback=False):
                return

            dist_threshold = config.getfloat('PROXIMITY', 'distance_threshold', fallback=4.0)
            # Update registry even if below threshold to keep track of devices in range vs out of range
            is_dynamic = self.is_device_dynamic(device.address, device.name, advertisement_data.rssi, config, advertisement_data)
            
            distance = rssi_to_distance(advertisement_data.rssi)
            if distance <= dist_threshold:
                if is_dynamic:
                    self.logger.debug(f"Dynamic Device Detected: {device.address} ({device.name}) | Distance: {distance}m (RSSI: {advertisement_data.rssi})")
                    self.callback_on_detection()
        except Exception as e:
            self.logger.error(f"Error in ble_callback: {e}")

    async def run_scanner(self):
        self.logger.info("Starting BleakScanner")
        self.scanner = BleakScanner(self.ble_callback)
        await self.scanner.start()
        
        last_cleanup = time.time()
        while self.running:
            await asyncio.sleep(5)
            self.update_device_count()
            
            # Cleanup every 5 minutes
            if time.time() - last_cleanup > 300:
                self.cleanup_registry()
                last_cleanup = time.time()
                
        await self.scanner.stop()
        self.logger.info("BleakScanner stopped")

    def update_device_count(self):
        config = common.get_config()
        dist_threshold = config.getfloat('PROXIMITY', 'distance_threshold', fallback=4.0)
        now = datetime.now()
        count = 0
        for addr, data in device_registry.items():
            # Device seen in last 30 seconds and last distance was below threshold
            time_diff = (now - data["last_seen"]).total_seconds()
            last_rssi = data["rssi_history"][-1]
            distance = rssi_to_distance(last_rssi)
            if time_diff < 30:
                if distance <= dist_threshold and data.get("is_dynamic", True):
                    count += 1
        self.devices_in_range = count

    def get_detailed_devices(self):
        config = common.get_config()
        dist_threshold = config.getfloat('PROXIMITY', 'distance_threshold', fallback=4.0)
        now = datetime.now()
        detailed_list = []
        
        for addr, data in device_registry.items():
            last_rssi = data["rssi_history"][-1]
            distance = rssi_to_distance(last_rssi)
            time_diff = (now - data["last_seen"]).total_seconds()
            
            status = "Out of Range"
            if time_diff < 30:
                if not data.get("is_dynamic", True):
                    status = "Ignored"
                elif distance > dist_threshold:
                    status = "Away"
                else:
                    status = "Active"
            
            detailed_list.append({
                "address": addr,
                "name": data.get("name", "Unknown"),
                "rssi": last_rssi,
                "distance_m": distance,
                "status": status,
                "ignore_reason": data.get("ignore_reason", ""),
                "last_seen_sec": int(time_diff)
            })
            
        return sorted(detailed_list, key=lambda x: x["distance_m"])

    def cleanup_registry(self):
        now = datetime.now()
        to_delete = []
        for addr, data in device_registry.items():
            # Remove devices not seen for more than 15 minutes
            if now - data["last_seen"] > timedelta(minutes=15):
                to_delete.append(addr)
        for addr in to_delete:
            del device_registry[addr]
        self.logger.info(f"Cleaned up {len(to_delete)} devices from registry")

    def start(self):
        config = common.get_config()
        if not config.getboolean('PROXIMITY', 'enabled', fallback=False):
            self.logger.info("Proximity detection is disabled in config. Not starting scanner.")
            return

        if self.running: return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.logger.info("Proximity scanner thread started")

    def _run_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.run_scanner())
        except Exception as e:
            self.logger.error(f"Error in async loop: {e}")
        finally:
            self.loop.close()

    def stop(self):
        self.logger.info("Stopping proximity scanner")
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
