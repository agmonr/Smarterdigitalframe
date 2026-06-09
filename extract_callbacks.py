import re
import json

with open("album_page.html", "r") as f:
    content = f.read()

# Try to find all JSON-like objects starting with AF_initDataCallback
callbacks = re.findall(r'AF_initDataCallback\(\{.*?\}\);', content, re.DOTALL)
print(f"Found {len(callbacks)} callbacks.")

for i, cb in enumerate(callbacks):
    # Extract key and data
    key_match = re.search(r"key:\s*'([^']+)'", cb)
    if key_match:
        key = key_match.group(1)
        print(f"Callback {i}: key={key}")
        # Try to extract data: part. It usually starts with [ and ends with ] before sideChannel
        data_match = re.search(r"data:\s*(\[.*\]),\s*sideChannel:", cb, re.DOTALL)
        if data_match:
            data_str = data_match.group(1)
            try:
                data = json.loads(data_str)
                print(f"  Data is a list of length {len(data)}")
                if "lh3.googleusercontent.com" in data_str:
                    print("  Contains LH3 URLs!")
                    # Save a sample to file for inspection
                    with open(f"data_{key}.json", "w") as df:
                        json.dump(data, df, indent=2)
            except Exception as e:
                print(f"  Error parsing JSON for key {key}: {e}")
