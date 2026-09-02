from us_earnings_monitor.__main__ import REPORT_MAX_CHARS, _compose_report
from us_earnings_monitor.models import Disclosure


def test_report_limit_fits_telegram_single_message():
    assert 3_200 <= REPORT_MAX_CHARS <= 4_096


def test_report_formats_table_and_deduplicates_ixbrl_source():
    pdf = Disclosure("sec_edgar", "1", "SPCX", "Form 10-Q", "2026-08-04T00:00:00-04:00", "https://example.test/1.htm", metadata={"format": "html"})
    ixbrl = Disclosure("sec_edgar", "1:ixbrl", "SPCX", "Form 10-Q [XBRL]", "2026-08-04T00:00:00-04:00", "https://example.test/1.htm", metadata={"format": "ixbrl"})
    text = "SPCX FY2026 Q2\n\n📈 關鍵指標:\n指標 | 本期 | YoY\n營收 | 40.2億美元 | +50.1%\n\n🏢 業務部門:\n• Launch services"
    report = _compose_report(text, [pdf, ixbrl])
    assert "<pre>" in report
    assert report.count("https://example.test/1.htm") == 1
    assert "百萬" not in report

