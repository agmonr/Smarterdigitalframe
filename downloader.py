import os
import re
import requests
import json
import logging

# Setup Logging
logging.basicConfig(
    filename='digitalframe.log',
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

STATUS_FILE = "album_status.json"
ALBUMS_FILE = "albums.json"

def get_album_status():
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def update_album_status(album_id, status):
    status_data = get_album_status()
    status_data[album_id] = status
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status_data, f)
    except Exception as e:
        logger.error(f"Error updating status file: {e}")

def get_albums():
    if os.path.exists(ALBUMS_FILE):
        try:
            with open(ALBUMS_FILE, 'r') as f:
                return json.load(f)
        except:
            return []
    return []

def download_album(album_id, url, output_dir):
    update_album_status(album_id, "Syncing...")
    logger.debug(f"Starting sync for album: {album_id} ({url})")
    try:
        if not os.path.isabs(output_dir):
            # Use project-local images directory as base
            base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
            output_dir = os.path.join(base_dir, output_dir)
            
        os.makedirs(output_dir, exist_ok=True)
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=30)
        logger.debug(f"Album page request status: {response.status_code}")
        
        if response.status_code != 200:
            update_album_status(album_id, f"Error: HTTP {response.status_code}")
            return False

        # Look for image URLs in the JSON data
        # Google Photos uses a specific format for these URLs
        # They usually look like: https://lh3.googleusercontent.com/pw/AM-JK...
        # or https://lh3.googleusercontent.com/lr/A...
        
        # This regex looks for common Google Photos image base URLs
        image_patterns = [
            r'https://lh3\.googleusercontent\.com/[a-zA-Z0-9_-]+',
            r'https://photos\.google\.com/share/[a-zA-Z0-9_-]+/photo/[a-zA-Z0-9_-]+'
        ]
        
        found_urls = set()
        for pattern in image_patterns:
            matches = re.findall(pattern, response.text)
            found_urls.update(matches)
            
        logger.debug(f"Initial regex found {len(found_urls)} URLs")

        # More specific extraction from JSON blobs
        # Looking for ["https://lh3.googleusercontent.com/pw/...", width, height]
        json_urls = re.findall(r'\"(https://lh3\.googleusercontent\.com/[^\"]+)\"', response.text)
        found_urls.update(json_urls)
        
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
        
        # Count existing images in the directory
        existing_files = [f for f in os.listdir(output_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        initial_count = len(existing_files)
        
        count = 0
        new_count = 0
        for i, img_url in enumerate(unique_images):
            # Strip existing parameters (anything after =)
            base_url = img_url.split('=')[0]
            
            # We use =w3000 to get a high-res version
            full_img_url = base_url + "=w3000"
            file_path = os.path.join(output_dir, f"image_{i}.jpg")
            
            try:
                # Check if we should download
                if not os.path.exists(file_path):
                    logger.debug(f"Downloading image {i}: {full_img_url}")
                    img_res = requests.get(full_img_url, headers=headers, timeout=15)
                    
                    if img_res.status_code == 200:
                        content_type = img_res.headers.get('Content-Type', '')
                        if 'image' in content_type:
                            with open(file_path, 'wb') as f:
                                f.write(img_res.content)
                            new_count += 1
                        else:
                            logger.warning(f"URL {base_url} did not return an image (Content-Type: {content_type})")
                    else:
                        logger.warning(f"Failed to download image {i}: HTTP {img_res.status_code}")
                count += 1
            except Exception as e:
                logger.error(f"Error processing image {i}: {e}")
        
        # Final count of images in directory
        final_files = [f for f in os.listdir(output_dir) if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        total_in_dir = len(final_files)
        
        update_album_status(album_id, f"Synced. Files: {total_in_dir} (New: {new_count})")
        return True
    except Exception as e:
        logger.error(f"Sync failed for {album_id}: {e}", exc_info=True)
        update_album_status(album_id, f"Error: {str(e)}")
        return False

def sync_all():
    albums = get_albums()
    for album in albums:
        download_album(album['id'], album['url'], album['path'])

if __name__ == "__main__":
    sync_all()
