import requests
import re
import json

url = "https://photos.app.goo.gl/V7VqYuAevcv4KGqx5"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

print(f"Fetching {url}...")
response = requests.get(url, headers=headers, timeout=30)

# Look for AF_initDataCallback or other JSON blobs
pattern = r'AF_initDataCallback\(\{key: \'ds:1\', hash: \'\d+\', data:(.*?), sideChannel: \{ \}\}\);'
match = re.search(pattern, response.text)
if match:
    data_str = match.group(1)
    # The data is often deep nested. Let's try to parse it.
    try:
        data = json.loads(data_str)
        # Typically images are in data[1]
        images = data[1]
        for i, img in enumerate(images[:5]):
            print(f"Image {i}:")
            # This is a bit of a guess on indices based on known patterns
            # [0] is often the ID
            # [1] is often the URL
            # [2] is often width
            # [3] is often height
            # [5] is often timestamp
            # [?] is often filename
            print(json.dumps(img, indent=2)[:500])
    except Exception as e:
        print(f"Error parsing JSON: {e}")
else:
    print("AF_initDataCallback not found. Looking for other JSON...")
    # Sometimes it's in a script tag as a large array
    # Look for patterns like ["https://lh3.googleusercontent.com/...", ... ]
    pass
