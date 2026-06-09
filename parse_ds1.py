import re
import json

with open("album_page.html", "r") as f:
    content = f.read()

# Pattern to catch the data block for ds:1
pattern = r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', data:(.*?), sideChannel: \{ \}\}\);"
match = re.search(pattern, content)

if match:
    data_str = match.group(1)
    try:
        data = json.loads(data_str)
        # In ds:1, data[1] is usually the list of media items
        items = data[1]
        print(f"Found {len(items)} items.")
        for i, item in enumerate(items[:10]):
            print(f"Item {i}:")
            # print(json.dumps(item, indent=2))
            # Let's try to identify common fields
            # item[1] is usually the list of URLs/dimensions
            # item[2] is often the original filename
            # item[5] is often the timestamp
            try:
                url = item[1][0]
                filename = item[2]
                timestamp = item[5]
                print(f"  URL: {url[:50]}...")
                print(f"  Filename: {filename}")
                print(f"  Timestamp: {timestamp}")
            except:
                pass
    except Exception as e:
        print(f"Error parsing JSON: {e}")
else:
    print("ds:1 not found")
