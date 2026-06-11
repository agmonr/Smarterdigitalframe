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

class ProximityScanner:
    def __init__(self, callback_on_detection):
        self.callback_on_detection = callback_on_detection
        self.scanner = None
        self.running = False
        self.thread = None
        self.loop = None
        self.devices_in_range = 0
        self.logger = common.setup_logger('proximity', 'proximity.log')

    def is_device_dynamic(self, addr, name, rssi, config):
        stable_threshold = config.getfloat('PROXIMITY', 'stable_threshold', fallback=0.5)
        min_samples = config.getint('PROXIMITY', 'min_samples', fallback=10)
        ignore_after_hours = config.getint('PROXIMITY', 'ignore_after_hours', fallback=2)
        
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
            return True
        
        device_registry[addr]["last_seen"] = now
        if name: device_registry[addr]["name"] = name
        
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

            rssi_threshold = config.getint('PROXIMITY', 'rssi_threshold', fallback=-75)
            # Update registry even if below threshold to keep track of devices in range vs out of range
            is_dynamic = self.is_device_dynamic(device.address, device.name, advertisement_data.rssi, config)
            
            if advertisement_data.rssi >= rssi_threshold:
                if is_dynamic:
                    self.logger.debug(f"Dynamic Device Detected: {device.address} ({device.name}) | RSSI: {advertisement_data.rssi}")
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
            
            # Cleanup every hour
            if time.time() - last_cleanup > 3600:
                self.cleanup_registry()
                last_cleanup = time.time()
                
        await self.scanner.stop()
        self.logger.info("BleakScanner stopped")

    def update_device_count(self):
        config = common.get_config()
        rssi_threshold = config.getint('PROXIMITY', 'rssi_threshold', fallback=-75)
        now = datetime.now()
        count = 0
        for addr, data in device_registry.items():
            # Device seen in last 30 seconds and last RSSI was above threshold
            time_diff = (now - data["last_seen"]).total_seconds()
            last_rssi = data["rssi_history"][-1]
            if time_diff < 30:
                if last_rssi >= rssi_threshold and data.get("is_dynamic", True):
                    count += 1
        self.devices_in_range = count

    def get_detailed_devices(self):
        config = common.get_config()
        rssi_threshold = config.getint('PROXIMITY', 'rssi_threshold', fallback=-75)
        now = datetime.now()
        detailed_list = []
        
        for addr, data in device_registry.items():
            last_rssi = data["rssi_history"][-1]
            time_diff = (now - data["last_seen"]).total_seconds()
            
            status = "Out of Range"
            if time_diff < 30:
                if not data.get("is_dynamic", True):
                    status = "Ignored"
                elif last_rssi < rssi_threshold:
                    status = "Weak Signal"
                else:
                    status = "Active"
            
            detailed_list.append({
                "address": addr,
                "name": data.get("name", "Unknown"),
                "rssi": last_rssi,
                "status": status,
                "ignore_reason": data.get("ignore_reason", ""),
                "last_seen_sec": int(time_diff)
            })
            
        return sorted(detailed_list, key=lambda x: x["rssi"], reverse=True)

    def cleanup_registry(self):
        now = datetime.now()
        to_delete = []
        for addr, data in device_registry.items():
            # Remove devices not seen for more than 4 hours
            if now - data["last_seen"] > timedelta(hours=4):
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
