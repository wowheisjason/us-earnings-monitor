from __future__ import annotations

import os

import requests


def send_report(
    text: str,
    token: str | None = None,
    chat_id: str | None = None,
    parse_mode: str | None = None,
) -> int:
    """Send a Telegram message and verify Telegram accepted it.

    Telegram returns HTTP 200 for some API-level failures, so checking only
    ``raise_for_status`` can falsely mark a notification as delivered.
    """
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required to publish")
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    response = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=30)
    response.raise_for_status()
    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError("Telegram returned a non-JSON response") from exc
    if result.get("ok") is not True:
        raise RuntimeError(f"Telegram rejected sendMessage: {result.get('description', 'unknown error')}")
    message_id = result.get("result", {}).get("message_id")
    if not isinstance(message_id, int):
        raise RuntimeError("Telegram accepted sendMessage without a message_id")
    return message_id
