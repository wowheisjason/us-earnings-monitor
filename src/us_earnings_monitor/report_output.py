from __future__ import annotations

import re

_INTERNAL_LABEL = re.compile(
    r"\[(?:\s*M[1-5]\s*[|｜]\s*[ABCD]\s*|\s*(?:Driver|Lever|Catalyst|Risk|Missing|Alternative)\s*)\]\s*",
    re.IGNORECASE,
)


def clean_user_report(text: str) -> str:
    """Remove internal analyst taxonomy from user-facing Telegram text."""
    value = _INTERNAL_LABEL.sub("", text or "")
    value = re.sub(r"(?im)^\s*└\s*Weak link\s*:\s*", "└ 尚未確認: ", value)
    value = re.sub(r"(?im)^\s*•\s*Claim\s*:\s*", "• ", value)
    value = re.sub(r"\s*\|\s*Evidence\s*:\s*", "；證據：", value)
    return value.strip()


def chinese_language_error(text: str) -> str | None:
    """Reject reports that are materially English prose rather than zh-TW.

    Proper nouns, financial metrics and product names may remain English. The
    threshold is deliberately loose; it only catches outputs like the AVGO live
    regression where nearly every explanatory sentence was English.
    """
    value = clean_user_report(text)
    if len(value) < 240:
        return None
    cjk = len(re.findall(r"[\u3400-\u9fff]", value))
    latin = len(re.findall(r"[A-Za-z]", value))
    if cjk < 120 or (latin > 0 and cjk / (cjk + latin) < 0.22):
        return "telegram_draft is predominantly English; explanatory prose must be Taiwan Traditional Chinese"
    return None
