"""
Telegram bot long-poller for FundScan alerts.

Run alongside the main app:
    python -m fundscan.alerts_bot

Handles /connect <code> to link a Telegram chat to a user account.
"""
import logging
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path="/Users/bilguunboursier/Documents/Claude/Projects/fundscan/.env")

from .alerts import link_telegram

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BASE = f"https://api.telegram.org/bot{TOKEN}"


def send(chat_id: str, text: str):
    try:
        httpx.post(f"{BASE}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception as e:
        log.warning("Send failed: %s", e)


def poll():
    offset = 0
    log.info("Bot polling started")
    while True:
        try:
            r = httpx.get(f"{BASE}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35)
            updates = r.json().get("result", [])
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if text.startswith("/connect ") and chat_id:
                    code = text.split(" ", 1)[1].strip()
                    ok = link_telegram(code, chat_id)
                    reply = (
                        "✅ Connected! You'll get Telegram alerts when funding rates cross your threshold."
                        if ok else
                        "❌ Invalid or expired code. Visit fundscan.uk/account for a new one."
                    )
                    send(chat_id, reply)
                elif text == "/start":
                    send(chat_id, "Welcome to FundScan Alerts! Go to fundscan.uk/account and use the /connect command shown there.")
        except Exception as e:
            log.error("Poll error: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    poll()
