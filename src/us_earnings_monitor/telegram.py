from __future__ import annotations

import os

import requests


def send_report(text: str, token: str | None = None, chat_id: str | None = None, parse_mode: str | None = None) -> None:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required to publish")
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=30)
    response.raise_for_status()

