import os
import re
import requests
import json
import logging
import shutil
import time
import hashlib
import common
from datetime import datetime

# Setup Logging using common library
logger = common.setup_logger(__name__)

STATUS_FILE = common.ALBUM_STATUS_FILE
SYNC_STATUS_FILE = common.SYNC_STATUS_FILE
ALBUMS_FILE = os.path.join(common.PROJECT_ROOT, "albums.json")
STORAGE_GUARDRAIL_GB = 1.0

# RAM-based path for persisted speed limit
SPEED_CONFIG_FILE = os.path.join(common.SHM_ROOT, "download_speed.json")

def get_persisted_speed(default_speed=4.0):
    """Reads the saved speed limit from the RAM-based config file."""
    if os.path.exists(SPEED_CONFIG_FILE):
        try:
            with open(SPEED_CONFIG_FILE, 'r') as f:
                data = json.load(f)
                return data.get("current_speed_mbps", default_speed)
        except:
            pass
    return default_speed

def save_persisted_speed(new_speed, current_saved_speed):
    """Compares speeds in RAM first. Only writes if the speed has actually changed."""
    if abs(new_speed - current_saved_speed) > 0.01:
        try:
            with open(SPEED_CONFIG_FILE, 'w') as f:
                json.dump({"current_speed_mbps": new_speed}, f)
            return True
        except:
            pass
    return False

def get_image_filename(url):
    """Use MD5 of the base URL to get a stable, unique filename."""
    base_url = url.split('=')[0]
    return hashlib.md5(base_url.encode()).hexdigest() + ".jpg"

def get_album_status():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def update_album_status(album_id, status, extra_info=None):
    status_data = get_album_status()
    if album_id not in status_data or isinstance(status_data[album_id], str):
        status_data[album_id] = {"status": status, "last_sync": datetime.now().isoformat()}
    else:
        status_data[album_id]["status"] = status
        status_data[album_id]["last_sync"] = datetime.now().isoformat()
    
    if extra_info:
        status_data[album_id].update(extra_info)
        
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status_data, f)
    except Exception as e:
        logger.error(f"Error updating status file: {e}")

def update_global_status(operation, message=""):
    """Update a global status file for the dashboard."""
    global_status = {
        "operation": operation,
        "message": message,
        "timestamp": datetime.now().isoformat(),
        "free_space_gb": get_free_space_gb()
    }
    try:
        with open(SYNC_STATUS_FILE, 'w') as f:
            json.dump(global_status, f)
    except Exception as e:
        logger.error(f"Error updating global status: {e}")

def get_free_space_gb():
    usage = shutil.disk_usage(common.PROJECT_ROOT)
    return usage.free / (2**30)

def check_storage_guardrail():
    """Ensure at least 1GB free space by deleting individual images, oldest first."""
    free_gb = get_free_space_gb()
    if free_gb >= STORAGE_GUARDRAIL_GB:
        return

    logger.info(f"Storage guardrail triggered: {free_gb:.2f}GB free. Need {STORAGE_GUARDRAIL_GB}GB.")
    update_global_status("Evicting", f"Low space: {free_gb:.2f}GB. Freeing up to {STORAGE_GUARDRAIL_GB}GB.")
    
    # Get currently playing image to protect it
    current_image_path = None
    state = common.get_state()
    if state and "full_path" in state:
        current_image_path = state["full_path"]

    # Collect all images with their mtime
    all_images = []
    image_dir = common.get_image_dir()
    if not os.path.exists(image_dir):
        return

    for root, _, files in os.walk(image_dir):
        for f in files:
            if f.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.bmp')):
                full_path = os.path.join(root, f)
                if full_path == current_image_path:
                    continue
                try:
                    all_images.append((full_path, os.path.getmtime(full_path)))
                except:
                    continue
    
    # Sort by mtime (oldest first)
    all_images.sort(key=lambda x: x[1])
    
    evicted_count = 0
    for img_path, _ in all_images:
        if get_free_space_gb() >= STORAGE_GUARDRAIL_GB:
            break
        
        try:
            os.remove(img_path)
            evicted_count += 1
            if evicted_count % 10 == 0:
                logger.info(f"Evicted {evicted_count} images... Current free: {get_free_space_gb():.2f}GB")
        except Exception as e:
            logger.error(f"Error evicting {img_path}: {e}")
            
    logger.info(f"Guardrail complete. Evicted {evicted_count} images. Free space: {get_free_space_gb():.2f}GB")
    update_global_status("Idle", f"Storage cleared: {evicted_count} old images removed.")

def get_albums():
    if os.path.exists(ALBUMS_FILE):
        try:
            with open(ALBUMS_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def download_album(album_id, url, output_dir, force_fast=False):
    check_storage_guardrail()
    update_album_status(album_id, "Syncing...")
    update_global_status("Syncing", f"Downloading album: {album_id}")
    
    logger.debug(f"Starting sync for album: {album_id} ({url})")
    try:
        if not os.path.isabs(output_dir):
            base_dir = common.get_image_dir()
            output_dir = os.path.join(base_dir, output_dir)
            
        # First sync detection: if directory is empty or doesn't exist, override force_fast to False
        is_first_sync = True
        if os.path.exists(output_dir):
            if any(f.lower().endswith(('.jpg', '.jpeg', '.png')) for f in os.listdir(output_dir)):
                is_first_sync = False
        
        if is_first_sync:
            if force_fast:
                logger.info(f"First sync detected for {album_id}. Keeping speed limit for safety.")
            force_fast = False

        os.makedirs(output_dir, exist_ok=True)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            update_album_status(album_id, f"Error: HTTP {response.status_code}")
            return False

        found_urls = set()
        # Use finditer to check context around each URL
        all_text = response.text
        
        # Combine patterns into one for context checking
        # Support lh3, lh4, lh5, etc. and be broad with characters (until a quote or space)
        combined_pattern = r'(https://lh[0-9]\.googleusercontent\.com/[^\s"\'\]]+|https://photos\.google\.com/share/[^\s"\'\]]+)'
        
        # More specific markers to avoid false positives with photos (which often have "is_video": false)
        video_true_markers = ['"is_video":true', '[true,null,"video"]', 'video-downloads', 'video-preview']
        # General markers that are rare near photos but common near videos
        general_video_markers = ['duration', '.mp4', '.mov', '.avi', '.mkv']
        
        for match in re.finditer(combined_pattern, all_text):
            img_url = match.group(1)
            
            # Extract 100 chars around the match (reduced from 200 for fewer false positives)
            start_ctx = max(0, match.start() - 100)
            end_ctx = min(len(all_text), match.end() + 100)
            context = all_text[start_ctx:end_ctx].lower().replace(' ', '') # remove spaces for easier matching
            
            is_video = any(marker in context for marker in video_true_markers) or \
                       any(marker in context for marker in general_video_markers)
            
            if is_video:
                logger.debug(f"Skipping potential video content: {img_url[:50]}... (found video marker in context)")
                continue
                
            found_urls.add(img_url)
        
        logger.info(f"Found {len(found_urls)} potential URLs in {album_id} before filtering.")
        
        # Filter: 
        # 1. Must be long enough to be a real photo ID
        # 2. Must not be a known UI element
        unique_images = []
        for img in found_urls:
            if len(img.split('/')[-1]) < 60: continue
            if 'googleusercontent.com' not in img: continue
            
            if img not in unique_images:
                unique_images.append(img)
                
        logger.info(f"Filtered to {len(unique_images)} candidate images for {album_id}")
        
        config = common.get_config()
        interval = config.getint('DEFAULT', 'interval', fallback=10)
        throttle_pause = interval / 2.0

        count = 0
        new_count = 0
        verified_filenames = set()
        total_images = len(unique_images)
        
        # Load persisted speed from RAM
        saved_speed = get_persisted_speed()
        target_speed_mbps = saved_speed
        
        # Enforce 4MB/s cap for standard syncs (micro SSD safety)
        if not force_fast:
            target_speed_mbps = min(target_speed_mbps, 4.0)
            
        chunk_size = 128 * 1024 # 128KB chunks are efficient for SD controllers
        current_actual_speed_mbps = 0.0
        
        for i, img_url in enumerate(unique_images):
            speed_info = f" ({current_actual_speed_mbps:.1f} MB/s)" if current_actual_speed_mbps > 0 else ""
            progress_msg = f"Album: {album_id} ({i+1}/{total_images}){speed_info}"
            update_global_status("Syncing", f"Downloading {progress_msg}")
            update_album_status(album_id, f"Syncing ({i+1}/{total_images}){speed_info}")
            
            filename = get_image_filename(img_url)
            verified_filenames.add(filename)
            file_path = os.path.join(output_dir, filename)
            
            # Use relative path from image_dir to check against 'removed'
            images_base = common.get_image_dir()
            rel_path = os.path.relpath(file_path, images_base)
            removed_path = os.path.join(common.PROJECT_ROOT, "removed", rel_path)
            
            # Skip if file was previously removed
            if os.path.exists(removed_path):
                logger.debug(f"Skipping previously removed image: {rel_path}")
                continue
            
            if not os.path.exists(file_path):
                base_url = img_url.split('=')[0]
                full_img_url = base_url + "=w3000"
                try:
                    img_res = requests.get(full_img_url, headers=headers, timeout=15, stream=True)
                    if img_res.status_code == 200:
                        content_type = img_res.headers.get('Content-Type', '')
                        if 'image' in content_type:
                            image_start_time = time.time()
                            bytes_downloaded = 0
                            with open(file_path, 'wb') as f:
                                # Start timing BEFORE the loop to catch the first chunk's download time
                                chunk_start_time = time.time()
                                for chunk in img_res.iter_content(chunk_size=chunk_size):
                                    if chunk:
                                        # Time elapsed since we started waiting for THIS chunk
                                        chunk_elapsed = time.time() - chunk_start_time
                                        
                                        f.write(chunk)
                                        f.flush() # Prevent massive RAM spikes to SD card
                                        
                                        bytes_downloaded += len(chunk)
                                        
                                        # Calculate actual speed for this chunk (Bytes / Seconds)
                                        # Include BOTH download time and write time for a realistic "system throughput"
                                        chunk_speed = len(chunk) / chunk_elapsed if chunk_elapsed > 0 else (target_speed_mbps * 1024 * 1024)
                                        current_actual_speed_mbps = chunk_speed / (1024 * 1024)

                                        if not force_fast:
                                            target_speed_bytes = target_speed_mbps * 1024 * 1024
                                            # Adaptive Throttling: If network slows below 95% of target, drop limit by 25%
                                            if chunk_speed < (target_speed_bytes * 0.95):
                                                new_speed = target_speed_mbps * 0.75
                                                if save_persisted_speed(new_speed, saved_speed):
                                                    logger.info(f"⚡ Speed cap adjusted: {new_speed:.2f} MB/s")
                                                    saved_speed = new_speed
                                                target_speed_mbps = new_speed
                                            else:
                                                # Enforce speed limit cap
                                                expected_time = len(chunk) / target_speed_bytes
                                                if chunk_elapsed < expected_time:
                                                    time.sleep(expected_time - chunk_elapsed)
                                        
                                        # Reset timer for the NEXT chunk's download
                                        chunk_start_time = time.time()
                            new_count += 1
                        else:
                            logger.warning(f"URL {base_url} did not return an image")
                    else:
                        logger.warning(f"Failed to download image {i}: HTTP {img_res.status_code}")
                except Exception as e:
                    logger.error(f"Error processing image {i}: {e}")
            count += 1
        
        # Cleanup orphaned files (no longer in album or old naming style)
        # SAFETY: Only perform cleanup if we actually found images in the cloud. 
        # If 0 images found, it's likely a scraping failure, so we skip cleanup to protect local files.
        if unique_images:
            for f in os.listdir(output_dir):
                if f.lower().endswith(('.jpg', '.jpeg', '.png')) and f not in verified_filenames:
                    orphaned_path = os.path.join(output_dir, f)
                    try:
                        os.remove(orphaned_path)
                        logger.info(f"Cleaned up orphaned image: {f}")
                    except Exception as e:
                        logger.error(f"Error cleaning up orphaned image {f}: {e}")
        else:
            logger.warning(f"No images found in cloud for album {album_id}. Skipping cleanup to protect local files.")

        final_files = [f for f in os.listdir(output_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        total_in_dir = len(final_files)
        
        album_size = sum(os.path.getsize(os.path.join(output_dir, f)) for f in final_files) / (2**20) # MB
        
        update_album_status(album_id, "Synced", {
            "file_count": total_in_dir,
            "new_files": new_count,
            "size_mb": round(album_size, 2)
        })
        return True
    except Exception as e:
        logger.error(f"Sync failed for {album_id}: {e}", exc_info=True)
        update_album_status(album_id, f"Error: {str(e)}")
        return False

def sync_all(force_fast=False):
    update_global_status("Idle", "Checking for updates...")
    albums = get_albums()
    for album in albums:
        download_album(album['id'], album['url'], album['path'], force_fast=force_fast)
    update_global_status("Idle", "Sync complete.")

if __name__ == "__main__":
    sync_all()
