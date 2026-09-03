from __future__ import annotations

import os
import time

import requests


def send_report(
    text: str,
    token: str | None = None,
    chat_id: str | None = None,
    parse_mode: str | None = None,
    *,
    max_attempts: int = 3,
    session: requests.Session | None = None,
) -> dict:
    """Deliver a report with bounded retry for transient Telegram failures."""
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required to publish")
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    client = session or requests
    attempts = max(1, max_attempts)
    for attempt in range(attempts):
        try:
            response = client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=30)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        delay = min(10.0, max(0.5, float(retry_after)))
                    except ValueError:
                        delay = min(10.0, 0.5 * (2 ** attempt))
                    time.sleep(delay)
                    continue
            response.raise_for_status()
            result = response.json()
            if result.get("ok") is not True:
                raise RuntimeError(f"Telegram rejected message: {result.get('description', 'unknown error')}")
            return result
        except requests.RequestException:
            if attempt + 1 >= attempts:
                raise
            time.sleep(min(10.0, 0.5 * (2 ** attempt)))
    raise RuntimeError("Telegram delivery exhausted retry attempts")

