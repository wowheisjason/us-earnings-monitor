"""Bounded, fail-closed enrichment; accessible articles only, never search snippets."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from ..models import Disclosure

DOMAINS = ("reuters.com", "bloomberg.com", "nikkei.com", "ft.com", "wsj.com")
POLICY = """SECONDARY Q&A EVIDENCE POLICY:
media_qa_report is an article reporting Q&A, NOT an official transcript. Its exact
text proves only what the outlet reported. Keep article URL, publication time,
retrieval time and outlet. Distinguish attributed direct management quotation,
reporter paraphrase/commentary, analyst QUESTION and model inference. Never turn
commentary, questions or paraphrase into management's verbatim words. Use official
documents for financial facts. Do not populate qa with reconstructed exchanges;
secondary observations belong in industry_signals with explicit media attribution.
Absence of official Q&A is a disclosed limitation, never a reason alone to reject
an otherwise verified report. Later updates require substantive new evidence.
"""


def allowed_url(url: str) -> bool:
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return p.scheme == "https" and not p.username and not p.password and p.port in (None, 443) and any(host == d or host.endswith("." + d) for d in DOMAINS)
    except ValueError:
        return False


def article_url(value: str) -> str | None:
    if allowed_url(value):
        return value
    p = urlparse(value)
    if p.hostname in {"www.bing.com", "bing.com"} and p.path == "/news/apiclick.aspx":
        targets = parse_qs(p.query).get("url", [])
        if len(targets) == 1 and allowed_url(targets[0]):
            return targets[0]
    return None


def _published(soup):
    for attrs in ({"property": "article:published_time"}, {"name": "datePublished"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return tag["content"]
    tag = soup.find("time", datetime=True)
    if tag:
        return tag["datetime"]
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        nodes = value if isinstance(value, list) else [value]
        for node in nodes:
            if isinstance(node, dict):
                nodes.extend(node.get("@graph", []))
                if node.get("datePublished"):
                    return node["datePublished"]
    return None


def verified_article(blob, company, event):
    soup = BeautifulSoup(blob, "lxml")
    raw_date = _published(soup)
    try:
        published = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00"))
        first = datetime.fromisoformat(event.first_seen_at)
        if published.tzinfo is None or first.tzinfo is None or abs(published - first) > timedelta(days=7):
            return None
    except (ValueError, TypeError):
        return None
    article = soup.find("article")
    if article is None:
        return None
    for tag in article(["script", "style", "nav", "footer", "aside"]):
        tag.decompose()
    text = article.get_text("\n", strip=True)
    lower = text.casefold()
    names = (company.name, getattr(company, "name_ja", ""))
    if not any(len(n) >= 3 and n.casefold() in lower for n in names):
        return None
    q = re.search(r"[1-4]", str(event.quarter or ""))
    quarter = q.group() if q else ""
    if not event.fiscal_year or str(event.fiscal_year) not in text or not quarter or not any(k in lower for k in ("q" + quarter, "第" + quarter + "四半期")):
        return None
    # A generic earnings article or outlet commentary is not Q&A reporting.
    explicit = any(k in lower for k in ("question-and-answer", "question and answer", "q&a", "質疑応答"))
    dialogue = ("asked" in lower and any(k in lower for k in ("replied", "responded", "told analysts"))) or ("質問" in text and "回答" in text)
    if len(text) < 500 or not (explicit and dialogue):
        return None
    return text[:24000], published.isoformat()


class CredibleMediaAdapter:
    """One search, at most three articles, 35-second budget; no paywall bypass."""

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def fetch(self, company, event, now):
        started = time.monotonic()
        attempts = []
        query = f'"{company.name}" {event.fiscal_year} {event.quarter} earnings Q&A (' + " OR ".join("site:" + d for d in DOMAINS) + ")"
        try:
            response = self.session.get("https://www.bing.com/news/search", params={"q": query, "format": "rss"}, timeout=8)
            response.raise_for_status()
            urls = [target for node in ET.fromstring(response.content).findall(".//item/link") if node.text and (target := article_url(node.text))]
        except (requests.RequestException, ET.ParseError):
            return [], {"reason": "media_search_unavailable", "attempts": attempts}
        for url in list(dict.fromkeys(urls))[:3]:
            if time.monotonic() - started >= 27:
                break
            try:
                response = self.session.get(url, timeout=8, allow_redirects=False)
                response.raise_for_status()
                if response.status_code != 200:
                    continue
                article = verified_article(response.content, company, event)
                attempts.append({"url": url, "verified": bool(article)})
                if not article:
                    continue
                text, published = article
                outlet = urlparse(url).hostname
                doc = Disclosure(source="credible_media_qa", source_id=hashlib.sha256(url.encode()).hexdigest()[:24], ticker=company.ticker,
                    title=f"{company.name} {event.fiscal_year} {event.quarter} — {outlet} Q&A reporting (secondary)", published_at=published,
                    url=url, fiscal_year=event.fiscal_year, quarter=event.quarter, document_kind="media_qa_report",
                    metadata={"media_text": text, "provenance": "credible_media_qa", "qualitative_only": True,
                              "retrieved_at": now.isoformat(), "published_at": published, "outlet": outlet})
                if hasattr(doc, "period_end"):
                    doc.period_end = getattr(event, "period_end", None)
                return [doc], {"reason": "verified_secondary_qa_reporting", "attempts": attempts, "provenance": "credible_media_qa"}
            except requests.RequestException:
                attempts.append({"url": url, "verified": False, "reason": "unavailable"})
        return [], {"reason": "no_verified_qa_reporting", "attempts": attempts}
