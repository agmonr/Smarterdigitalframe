import requests

url = "https://lh3.googleusercontent.com/pw/AP1GczM3GkpqGyd7CO-tUwT6bH2Vi6WqWomocIqidkIHYF_Nc5uFm-NhmY9LWAvt0U_98n3uDW5Z5OAoImwRJ6e7u91n1xi2Em4fn3X06O030c8zERkZs1Mx=w3000"
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

print(f"Fetching {url}...")
response = requests.get(url, headers=headers, stream=True)
print(f"Status: {response.status_code}")
print("Headers:")
for k, v in response.headers.items():
    print(f"  {k}: {v}")

url_d = url.split('=')[0] + "=d"
print(f"\nFetching {url_d}...")
response_d = requests.get(url_d, headers=headers, stream=True)
print(f"Status: {response_d.status_code}")
print("Headers:")
for k, v in response_d.headers.items():
    print(f"  {k}: {v}")
