import re
import json

with open("album_page.html", "r") as f:
    content = f.read()

matches = re.finditer(r"AF_initDataCallback\(\{key: '([^']+)', hash: '[^']+', data:(.*?), sideChannel: \{ \}\}\);", content)

for match in matches:
    key = match.group(1)
    data_str = match.group(2)
    print(f"Key: {key}")
    try:
        data = json.loads(data_str)
        print(f"  Data type: {type(data)}")
        if isinstance(data, list):
            print(f"  List length: {len(data)}")
            # Search for LH3 URLs in the data
            if "lh3.googleusercontent.com" in data_str:
                print("  Contains LH3 URLs!")
                # Let's try to find where the media items are
                # Often it's in data[1] or data[0][1]
                pass
    except Exception as e:
        print(f"  Error parsing: {e}")
