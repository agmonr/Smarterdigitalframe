import json
import re

with open("data_ds1.txt", "r") as f:
    data = json.load(f)

items = data[1]
for i, item in enumerate(items[:10]):
    print(f"Item {i}:")
    # Recursively find all strings in the item
    def find_strings(obj):
        strs = []
        if isinstance(obj, str):
            strs.append(obj)
        elif isinstance(obj, list):
            for x in obj:
                strs.extend(find_strings(x))
        elif isinstance(obj, dict):
            for x in obj.values():
                strs.extend(find_strings(x))
        return strs
    
    all_strs = find_strings(item)
    filenames = [s for s in all_strs if re.search(r'\.(jpg|jpeg|png|gif|mp4|mov)$', s, re.I)]
    print(f"  Possible filenames: {filenames}")
    
    # Check timestamps
    def find_numbers(obj):
        nums = []
        if isinstance(obj, (int, float)):
            nums.append(obj)
        elif isinstance(obj, list):
            for x in obj:
                nums.extend(find_numbers(x))
        elif isinstance(obj, dict):
            for x in obj.values():
                nums.extend(find_numbers(x))
        return nums
    
    all_nums = find_numbers(item)
    # Timestamps in ms are usually around 1.7e12 now
    timestamps = [n for n in all_nums if 1e12 < n < 2e12]
    print(f"  Possible timestamps: {timestamps}")
