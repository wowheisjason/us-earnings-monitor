from us_earnings_monitor.document_compressor import compress_presentation_pages, compress_transcript_pages
from us_earnings_monitor.evidence_architecture import extraction_groups, sectionize
from us_earnings_monitor.models import Evidence
from us_earnings_monitor.report_output import chinese_language_error, clean_user_report


def test_transcript_compressor_prioritizes_qa_and_bounds_text():
    prepared = [f"Prepared page {i}. Revenue and guidance discussion. " * 120 for i in range(1, 9)]
    qa = [
        "Question-and-Answer Session\nAnalyst: What is AI demand?\nCEO: AI demand remains strong and capacity is constrained.",
        "Analyst: What about margins?\nCFO: Mix and pricing support gross margin, while supply remains tight.",
    ]
    text = compress_transcript_pages(prepared + qa, max_chars=12000)
    assert len(text) <= 12000
    assert "Question-and-Answer Session" in text
    assert "What about margins?" in text
    assert "[[PAGE 9]]" in text


def test_presentation_compressor_selects_high_signal_page():
    pages = ["cover", "agenda"] + ["ordinary product information" for _ in range(20)]
    pages[14] = "FY2027 guidance revenue $10 billion gross margin 65% AI capacity capex customer demand"
    text = compress_presentation_pages(pages, max_chars=5000)
    assert "FY2027 guidance" in text
    assert len(text) <= 5000


def test_extraction_groups_never_sample_away_real_qa():
    transcript = Evidence(
        "doc:t",
        "Q2 Earnings Call Transcript",
        "https://example.com/t",
        "\n\n".join(
            [f"Prepared remarks section {i} revenue guidance " * 100 for i in range(8)]
            + ["Question-and-Answer Session"]
            + [f"Analyst question {i}. CEO answer demand pricing margin competition. " * 100 for i in range(5)]
        ),
    )
    groups = extraction_groups([transcript])
    flattened = [section for group in groups for section in group]
    assert any(section["kind"] == "qa" for section in flattened)
    assert len(groups) <= 6


def test_user_report_strips_internal_taxonomy():
    value = clean_user_report("• [M5｜B] 需求改善\n• [Driver] AI需求\n• [Risk] 供給限制\n└ Weak link: ASP")
    assert "[M5" not in value
    assert "[Driver]" not in value
    assert "[Risk]" not in value
    assert "尚未確認" in value


def test_language_gate_rejects_english_body_but_allows_zh_tw_with_terms():
    english = "💡 核心投資結論:\n" + "Revenue growth is strong and management expects AI demand to improve margins. " * 30
    assert chinese_language_error(english)
    chinese = "💡 核心投資結論:\n" + "AI需求持續成長，管理層指出產品組合改善有助於 gross margin，但供給限制仍需觀察。" * 20
    assert chinese_language_error(chinese) is None
