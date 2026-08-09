"""Telegram delivery — the most reliable way to get this on your phone."""

from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
_LIMIT = 4000  # Telegram caps messages at 4096 characters


def send_telegram(bot_token: str, chat_id: str, text: str) -> None:
    if not bot_token or not chat_id:
        raise ValueError(
            "Telegram not configured. Create a bot with @BotFather, then set "
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env."
        )

    for chunk in _chunks(text):
        response = requests.post(
            API.format(token=bot_token),
            json={"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True},
            timeout=25,
        )
        if not response.ok:
            raise RuntimeError(f"Telegram API returned {response.status_code}: {response.text[:200]}")

    log.info("sent Telegram summary to chat %s", chat_id)


def _chunks(text: str) -> list[str]:
    """Split on line boundaries so a job entry never straddles two messages."""
    if len(text) <= _LIMIT:
        return [text]

    out, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > _LIMIT:
            out.append(current)
            current = ""
        current += line
    if current:
        out.append(current)
    return out
