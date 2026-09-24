"""Push messages to your own Telegram via a bot.

Setup (2 minutes):
  1. In Telegram, message @BotFather -> /newbot -> copy the token.
  2. Send any message to your new bot, then open
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id": ...}.
  3. export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
"""

import json
import os

from . import net


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send(text: str) -> bool:
    if not configured():
        print("[alerts] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set; printing instead:\n" + text)
        return False
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    body = json.dumps({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000],
                       "disable_web_page_preview": True}).encode()
    try:
        net.request(url, headers={"Content-Type": "application/json"}, data=body)
        return True
    except net.HttpError as exc:
        print(f"[alerts] send failed: {exc}")
        return False
