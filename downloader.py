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

STATUS_FILE = os.path.join(common.PROJECT_ROOT, "album_status.json")
ALBUMS_FILE = os.path.join(common.PROJECT_ROOT, "albums.json")
STORAGE_GUARDRAIL_GB = 1.0

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
        with open(os.path.join(common.PROJECT_ROOT, "sync_status.json"), 'w') as f:
            json.dump(global_status, f)
    except Exception as e:
        logger.error(f"Error updating global status: {e}")

def get_free_space_gb():
    usage = shutil.disk_usage(common.PROJECT_ROOT)
    return usage.free / (2**30)

def check_storage_guardrail():
    """Ensure at least 1GB free space by evicting oldest albums."""
    free_gb = get_free_space_gb()
    if free_gb >= STORAGE_GUARDRAIL_GB:
        return

    logger.info(f"Storage guardrail triggered: {free_gb:.2f}GB free. Need {STORAGE_GUARDRAIL_GB}GB.")
    update_global_status("Evicting", f"Low space: {free_gb:.2f}GB. Freeing up to {STORAGE_GUARDRAIL_GB}GB.")
    
    # Get all local album directories
    albums = get_albums()
    album_dirs = []
    base_dir = os.path.join(common.PROJECT_ROOT, "images", "google_photos")
    
    if not os.path.exists(base_dir):
        return

    for d in os.listdir(base_dir):
        full_path = os.path.join(base_dir, d)
        if os.path.isdir(full_path):
            # Use mtime as "least recently used/synced"
            album_dirs.append((full_path, os.path.getmtime(full_path)))
    
    # Sort by mtime (oldest first)
    album_dirs.sort(key=lambda x: x[1])
    
    for album_path, _ in album_dirs:
        if get_free_space_gb() >= STORAGE_GUARDRAIL_GB:
            break
        
        album_name = os.path.basename(album_path)
        logger.info(f"Evicting album: {album_name}")
        update_global_status("Evicting", f"Deleting oldest album: {album_name}")
        try:
            shutil.rmtree(album_path)
            # Find album ID to update status
            for album in albums:
                if os.path.basename(album['path']) == album_name:
                    update_album_status(album['id'], "Evicted (Low Space)")
                    break
        except Exception as e:
            logger.error(f"Error evicting {album_path}: {e}")

def get_albums():
    if os.path.exists(ALBUMS_FILE):
        try:
            with open(ALBUMS_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def download_album(album_id, url, output_dir):
    check_storage_guardrail()
    update_album_status(album_id, "Syncing...")
    update_global_status("Syncing", f"Downloading album: {album_id}")
    
    logger.debug(f"Starting sync for album: {album_id} ({url})")
    try:
        if not os.path.isabs(output_dir):
            base_dir = os.path.join(common.PROJECT_ROOT, "images")
            output_dir = os.path.join(base_dir, output_dir)
            
        os.makedirs(output_dir, exist_ok=True)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            update_album_status(album_id, f"Error: HTTP {response.status_code}")
            return False

        image_patterns = [
            r'https://lh3\.googleusercontent\.com/[a-zA-Z0-9_-]+',
            r'https://photos\.google\.com/share/[a-zA-Z0-9_-]+/photo/[a-zA-Z0-9_-]+'
        ]
        
        found_urls = set()
        for pattern in image_patterns:
            matches = re.findall(pattern, response.text)
            found_urls.update(matches)
            
        json_urls = re.findall(r'\"(https://lh3\.googleusercontent\.com/[^\"]+)\"', response.text)
        found_urls.update(json_urls)
        
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
        
        for i, img_url in enumerate(unique_images):
            progress_msg = f"Album: {album_id} ({i+1}/{total_images})"
            update_global_status("Syncing", f"Downloading {progress_msg}")
            update_album_status(album_id, f"Syncing ({i+1}/{total_images})")
            
            filename = get_image_filename(img_url)
            verified_filenames.add(filename)
            file_path = os.path.join(output_dir, filename)
            
            # Use relative path from 'images' to check against 'removed'
            images_base = os.path.join(common.PROJECT_ROOT, "images")
            rel_path = os.path.relpath(file_path, images_base)
            removed_path = os.path.join(common.PROJECT_ROOT, "removed", rel_path)
            
            # Skip if file was previously removed
            if os.path.exists(removed_path):
                logger.debug(f"Skipping previously removed image: {rel_path}")
                continue
            
            if not os.path.exists(file_path):
                base_url = img_url.split('=')[0]
                full_img_url = base_url + "=w3000"
                start_time = time.time()
                try:
                    img_res = requests.get(full_img_url, headers=headers, timeout=15)
                    download_time = time.time() - start_time
                    
                    if img_res.status_code == 200:
                        content_type = img_res.headers.get('Content-Type', '')
                        if 'image' in content_type:
                            with open(file_path, 'wb') as f:
                                f.write(img_res.content)
                            new_count += 1
                            
                            # Adaptive Throttling: If download is slow (> 2s for example, or relative to interval)
                            # Or if it's "lagging" the system. Let's use 2s as a baseline for "slow".
                            if download_time > 2.0:
                                logger.info(f"Slow download detected ({download_time:.2f}s). Throttling for {throttle_pause}s")
                                update_global_status("Waiting due to slow download", f"Throttling for {throttle_pause}s after slow image download ({i+1}/{total_images})")
                                time.sleep(throttle_pause)
                                update_global_status("Syncing", f"Downloading {progress_msg}")
                        else:
                            logger.warning(f"URL {base_url} did not return an image")
                    else:
                        logger.warning(f"Failed to download image {i}: HTTP {img_res.status_code}")
                except Exception as e:
                    logger.error(f"Error processing image {i}: {e}")
            count += 1
        
        # Cleanup orphaned files (no longer in album or old naming style)
        for f in os.listdir(output_dir):
            if f.lower().endswith(('.jpg', '.jpeg', '.png')) and f not in verified_filenames:
                orphaned_path = os.path.join(output_dir, f)
                try:
                    os.remove(orphaned_path)
                    logger.info(f"Cleaned up orphaned image: {f}")
                except Exception as e:
                    logger.error(f"Error cleaning up orphaned image {f}: {e}")

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

def sync_all():
    update_global_status("Idle", "Checking for updates...")
    albums = get_albums()
    for album in albums:
        download_album(album['id'], album['url'], album['path'])
    update_global_status("Idle", "Sync complete.")

if __name__ == "__main__":
    sync_all()
