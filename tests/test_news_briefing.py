from datetime import datetime, timedelta, timezone

from us_earnings_monitor.news_briefing import Article, BriefingItem, canonicalize_url, dedupe_and_limit, format_briefing, split_telegram, validate_items


UTC = timezone.utc


def article(url: str, title: str, hours_ago: int = 1) -> Article:
    moment = datetime(2026, 9, 28, 23, 0, tzinfo=UTC) - timedelta(hours=hours_ago)
    return Article(url, title, "reuters.com", moment, moment, "摘要", "Reuters")


def test_url_canonicalization_removes_tracking_parameters():
    assert canonicalize_url("https://www.Reuters.com/story/?utm_source=x&ref=y&id=7") == "https://reuters.com/story?id=7"


def test_dedupe_and_limit_keeps_at_most_ten_and_drops_duplicate_titles():
    items = [article(f"https://reuters.com/{i}", "Same headline" if i < 2 else f"Headline {i}") for i in range(12)]
    result = dedupe_and_limit(items)
    assert len(result) == 10
    assert len({item.title for item in result}) == len(result)


def test_validate_items_rejects_old_article_and_long_summary():
    now = datetime(2026, 9, 28, 23, 0, tzinfo=UTC)
    fresh = article("https://reuters.com/fresh", "Fresh", 2)
    old = article("https://reuters.com/old", "Old", 25)
    raw = [
        {"emoji": "🧠", "headline": "新標題", "fact": "新事實", "summary": "短摘要", "source": "Reuters", "url": fresh.url},
        {"emoji": "📰", "headline": "舊標題", "fact": "舊事實", "summary": "短摘要", "source": "Reuters", "url": old.url},
        {"emoji": "📰", "headline": "太長", "fact": "事實", "summary": "x" * 301, "source": "Reuters", "url": fresh.url},
    ]
    result = validate_items(raw, [fresh, old], now)
    assert [item.url for item in result] == [fresh.url]


def test_format_briefing_uses_html_bold_and_link_label():
    text = format_briefing([BriefingItem("🧠", "標題", "新事實", "摘要", "Reuters", "2026/09/28", "https://reuters.com/x")])
    assert "<b>" in text
    assert '<a href="https://reuters.com/x">連結</a>' in text
    assert "https://reuters.com/x" not in text.replace('<a href="https://reuters.com/x">連結</a>', "")


def test_split_telegram_respects_limit():
    chunks = split_telegram("a" * 200 + "\n\n---\n\n" + "b" * 200, limit=250)
    assert len(chunks) == 2
    assert all(len(chunk) <= 250 for chunk in chunks)
