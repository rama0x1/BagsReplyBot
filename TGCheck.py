import requests
import os

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    print("ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as environment variables.")
    exit(1)

url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
payload = {
    "chat_id": TELEGRAM_CHAT_ID,
    "text": "✅ Test message from royalty-watcher bot",
    "parse_mode": "HTML"
}

print(f"Sending to chat_id={TELEGRAM_CHAT_ID}...")
r = requests.post(url, data=payload)

print("Status code:", r.status_code)
print("Response:", r.text)
