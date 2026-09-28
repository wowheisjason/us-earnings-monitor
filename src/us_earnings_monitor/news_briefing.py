from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests


UTC = timezone.utc
MAX_ITEMS = 10
MAX_SUMMARY_CHARS = 300
REQUEST_TIMEOUT = 25
GDELT_RETRIES = 3
GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
SEARCHES = (
    "(inflation OR CPI OR GDP OR PMI OR central bank OR interest rate OR tariff OR export control OR oil OR energy)",
    "(AI OR GPU OR semiconductor OR HBM OR memory OR foundry OR TSMC OR NVIDIA OR AMD OR ASML)",
    '("data center" OR datacenter OR "liquid cooling" OR "power grid" OR transformer OR switchgear OR networking OR optical)',
)
PREFERRED_DOMAINS = {
    "reuters.com": 100,
    "bloomberg.com": 98,
    "ft.com": 96,
    "wsj.com": 95,
    "apnews.com": 94,
    "cnbc.com": 92,
    "nikkei.com": 90,
    "asia.nikkei.com": 90,
    "federalreserve.gov": 88,
    "sec.gov": 88,
    "ecb.europa.eu": 88,
    "boj.or.jp": 88,
    "mof.go.jp": 88,
    "nvidia.com": 86,
    "tsmc.com": 86,
    "intel.com": 86,
}


@dataclass(frozen=True)
class Article:
    url: str
    title: str
    domain: str
    seen_at: datetime
    published_at: datetime | None = None
    description: str = ""
    source_name: str = ""

    @property
    def effective_date(self) -> datetime:
        return self.published_at or self.seen_at


@dataclass(frozen=True)
class BriefingItem:
    emoji: str
    headline: str
    fact: str
    summary: str
    source: str
    article_date: str
    url: str


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not (k.lower().startswith("utm_") or k.lower() in {"ref", "source", "ocid", "output"})]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", urlencode(query), ""))


def _parse_gdelt_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _parse_any_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    parsed = _parse_gdelt_date(text)
    if parsed:
        return parsed
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _domain_score(domain: str) -> int:
    domain = domain.lower().removeprefix("www.")
    for preferred, score in PREFERRED_DOMAINS.items():
        if domain == preferred or domain.endswith("." + preferred):
            return score
    return 50


def _source_name(domain: str) -> str:
    labels = {
        "reuters.com": "Reuters",
        "bloomberg.com": "Bloomberg",
        "ft.com": "Financial Times",
        "wsj.com": "Wall Street Journal",
        "apnews.com": "AP",
        "cnbc.com": "CNBC",
        "nikkei.com": "Nikkei",
        "asia.nikkei.com": "Nikkei Asia",
        "federalreserve.gov": "Federal Reserve",
        "sec.gov": "SEC",
        "ecb.europa.eu": "ECB",
        "boj.or.jp": "日本銀行",
        "mof.go.jp": "日本財務省",
        "nvidia.com": "NVIDIA",
        "tsmc.com": "TSMC",
        "intel.com": "Intel",
    }
    clean = domain.lower().removeprefix("www.")
    return next((name for host, name in labels.items() if clean == host or clean.endswith("." + host)), domain)


def _json_ld_published_at(text: str) -> datetime | None:
    for raw in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.I | re.S):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict):
                value = node.get("datePublished") or node.get("dateCreated")
                parsed = _parse_any_date(value)
                if parsed:
                    return parsed
    patterns = (
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([^"\']+)',
        r'<time[^>]+datetime=["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.S)
        parsed = _parse_any_date(match.group(1) if match else None)
        if parsed:
            return parsed
    return None


def _page_metadata(session: requests.Session, article: Article) -> Article:
    try:
        response = session.get(article.url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "us-earnings-monitor-news/1.0"})
        response.raise_for_status()
    except requests.RequestException:
        return article
    text = response.text[:2_000_000]
    published_at = _json_ld_published_at(text) or article.published_at
    description_match = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]+content=["\']([^"\']+)',
        text,
        re.I | re.S,
    )
    title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', text, re.I | re.S)
    return Article(
        url=article.url,
        title=html.unescape(title_match.group(1).strip()) if title_match else article.title,
        domain=article.domain,
        seen_at=article.seen_at,
        published_at=published_at,
        description=html.unescape(description_match.group(1).strip()) if description_match else article.description,
        source_name=article.source_name,
    )


def _gdelt_search(session: requests.Session, query: str) -> requests.Response | None:
    """Fetch one GDELT search, tolerating transient public-API throttling."""
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": 20,
        "sort": "HybridRel",
        "timespan": "24h",
    }
    headers = {"User-Agent": "us-earnings-monitor-news/1.0"}
    for attempt in range(GDELT_RETRIES + 1):
        try:
            response = session.get(GDELT_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT, headers=headers)
        except requests.RequestException as exc:
            if attempt >= GDELT_RETRIES:
                print(f"GDELT request skipped after retries: {exc}")
                return None
            time.sleep(2 ** attempt)
            continue
        if response.status_code != 429:
            try:
                response.raise_for_status()
            except requests.RequestException as exc:
                print(f"GDELT query skipped: {exc}")
                return None
            return response
        if attempt >= GDELT_RETRIES:
            print("GDELT query skipped after repeated HTTP 429 throttling")
            return None
        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = max(2, min(int(retry_after), 20))
        except ValueError:
            delay = 2 ** (attempt + 1)
        time.sleep(delay)
    return None


def discover_articles(now: datetime | None = None, session: requests.Session | None = None) -> list[Article]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now - timedelta(hours=24)
    session = session or requests.Session()
    found: dict[str, Article] = {}
    for query in SEARCHES:
        response = _gdelt_search(session, query)
        if response is None:
            continue
        for raw in response.json().get("articles", []):
            url = canonicalize_url(str(raw.get("url", "")))
            seen_at = _parse_gdelt_date(str(raw.get("seendate", "")))
            domain = urlparse(url).netloc.removeprefix("www.").lower()
            title = str(raw.get("title", "")).strip()
            if not url.startswith("https://") or not seen_at or not title or not domain:
                continue
            if not start <= seen_at <= now or url in found:
                continue
            found[url] = Article(url, title, domain, seen_at, seen_at, source_name=_source_name(domain))
    enriched = [_page_metadata(session, item) for item in found.values()]
    return sorted(
        (item for item in enriched if start <= item.effective_date <= now),
        key=lambda item: (_domain_score(item.domain), item.effective_date),
        reverse=True,
    )


def dedupe_and_limit(articles: Iterable[Article], limit: int = MAX_ITEMS) -> list[Article]:
    unique: dict[str, Article] = {}
    seen_titles: set[str] = set()
    for article in sorted(articles, key=lambda item: (_domain_score(item.domain), item.effective_date), reverse=True):
        normalized_title = re.sub(r"[^a-z0-9]+", " ", article.title.lower()).strip()
        if article.url in unique or (normalized_title and normalized_title in seen_titles):
            continue
        unique[article.url] = article
        seen_titles.add(normalized_title)
        if len(unique) >= limit:
            break
    return list(unique.values())


def _gemini_json(api_key: str, evidence: list[Article], model: str) -> list[dict[str, Any]]:
    prompt = """你是台灣投資人新聞編輯。只可根據提供的文章標題、摘要、來源、日期與網址寫作，不得補充未提供的事實、預測或投資建議。輸出 JSON array，不要 Markdown。每個物件欄位：emoji、headline、fact、summary、source、article_date、url。headline 簡潔；fact 只寫一句新事實；summary 使用繁體中文台灣用語且最多300中文字；url 必須原樣保留。若證據不足，省略該篇。"""
    payload = [{"title": a.title, "description": a.description, "source": a.source_name, "article_date": a.effective_date.strftime("%Y-%m-%d"), "url": a.url} for a in evidence]
    response = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": api_key},
        json={"contents": [{"parts": [{"text": prompt + "\n證據：\n" + json.dumps(payload, ensure_ascii=False)}]}], "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"}},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    body = response.json()
    text = body["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def validate_items(raw_items: Iterable[dict[str, Any]], evidence: Iterable[Article], now: datetime, limit: int = MAX_ITEMS) -> list[BriefingItem]:
    by_url = {a.url: a for a in evidence}
    start = now - timedelta(hours=24)
    result: list[BriefingItem] = []
    for raw in raw_items:
        url = canonicalize_url(str(raw.get("url", "")))
        article = by_url.get(url)
        if not article or not start <= article.effective_date <= now:
            continue
        headline = str(raw.get("headline", "")).strip()
        fact = str(raw.get("fact", "")).strip()
        summary = str(raw.get("summary", "")).strip()
        source = str(raw.get("source", article.source_name)).strip() or article.source_name
        if not headline or not fact or not summary or len(summary) > MAX_SUMMARY_CHARS:
            continue
        result.append(BriefingItem(
            emoji=str(raw.get("emoji", "📰"))[:2] or "📰",
            headline=headline,
            fact=fact,
            summary=summary,
            source=source,
            article_date=article.effective_date.strftime("%Y/%m/%d"),
            url=article.url,
        ))
        if len(result) >= limit:
            break
    return result


def format_briefing(items: list[BriefingItem]) -> str:
    if not items:
        return "<b>🔥 今日投資新聞 Briefing</b>\n\n過去24小時內沒有通過日期、來源與重複檢查的新聞。"
    blocks = [f"<b>🔥 今日最重要的 {len(items)} 則市場新聞</b>"]
    for index, item in enumerate(items, 1):
        blocks.append(
            "\n".join((
                f"<b>{index}. {html.escape(item.emoji)} {html.escape(item.headline)}</b>",
                html.escape(item.fact),
                f"<b>重點摘要</b>\n{html.escape(item.summary)}",
                f"<b>來源：{html.escape(item.source)}</b>",
                f"<b>[{item.article_date}] — <a href=\"{html.escape(item.url, quote=True)}\">連結</a></b>",
            ))
        )
    return "\n\n---\n\n".join(blocks)


def split_telegram(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n---\n\n"):
        candidate = block if not current else current + "\n\n---\n\n" + block
        if current and len(candidate) > limit:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def run(now: datetime | None = None, dry_run: bool = False) -> int:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    articles = dedupe_and_limit(discover_articles(now=now), MAX_ITEMS)
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required for Traditional Chinese news summaries")
    raw_items = _gemini_json(api_key, articles, os.environ.get("GEMINI_NEWS_MODEL", "gemini-2.5-flash")) if articles else []
    items = validate_items(raw_items, articles, now)
    message = format_briefing(items)
    print(message)
    if dry_run:
        return 0
    from us_earnings_monitor.telegram import send_report
    message_ids = [send_report(chunk, parse_mode="HTML") for chunk in split_telegram(message)]
    print(json.dumps({"telegram_send_ok": True, "message_ids": message_ids, "item_count": len(items)}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--at", help="UTC ISO timestamp for deterministic test runs")
    args = parser.parse_args()
    at = datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else None
    return run(now=at, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
