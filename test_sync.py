import requests
import re

url = "https://photos.app.goo.gl/V7VqYuAevcv4KGqx5"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

print(f"Fetching {url}...")
response = requests.get(url, headers=headers, timeout=30)
print(f"Status: {response.status_code}")

image_patterns = [
    r'https://lh3\.googleusercontent\.com/[a-zA-Z0-9_-]+',
    r'https://photos\.google\.com/share/[a-zA-Z0-9_-]+/photo/[a-zA-Z0-9_-]+'
]

found_urls = set()
for pattern in image_patterns:
    matches = re.findall(pattern, response.text)
    found_urls.update(matches)
    
print(f"Regex found {len(found_urls)} URLs")

json_urls = re.findall(r'\"(https://lh3\.googleusercontent\.com/[^\"]+)\"', response.text)
print(f"JSON regex found {len(json_urls)} URLs")

unique_images = []
for img in list(found_urls) + json_urls:
    if len(img.split('/')[-1]) < 60: continue
    if 'googleusercontent.com' not in img: continue
    if img not in unique_images:
        unique_images.append(img)

print(f"Filtered to {len(unique_images)} candidate images")
if unique_images:
    print(f"First image URL: {unique_images[0]}")
