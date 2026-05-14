"""
Telegram - sends messages to the user's Telegram chat.
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)
_URL = "https://api.telegram.org/bot{token}/sendMessage"


async def send(text: str, silent: bool = False) -> bool:
    token   = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().lstrip("=")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip().lstrip("=")
    print(f"[ADAM] {text}")
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(_URL.format(token=token), json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_notification": silent,
            })
            return r.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram failed: {e}")
        return False
