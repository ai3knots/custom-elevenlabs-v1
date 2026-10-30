import os
import json
import urllib.request
from dotenv import load_dotenv

load_dotenv(r'C:\Users\Khizar\Desktop\Khizar-v1\.env')
api_key = os.getenv('GEMINI_API_KEY', '').strip('"\'')
url = f'https://generativelanguage.googleapis.com/v1beta/models?key={api_key}'
print(f"Checking URL with key length: {len(api_key)}")

try:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read().decode())
        models = [m.get('name') for m in data.get('models', [])]
        print(f"Total models returned: {len(models)}")
        for m in sorted(models):
            if 'gemini' in m.lower():
                print(" ", m)
except Exception as e:
    print("Error:", e)

