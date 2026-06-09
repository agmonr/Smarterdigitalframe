import requests
import re
import json

url = "https://photos.app.goo.gl/V7VqYuAevcv4KGqx5"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

print(f"Fetching {url}...")
response = requests.get(url, headers=headers, timeout=30)

with open("album_page.html", "w") as f:
    f.write(response.text)

print("Saved album_page.html")
