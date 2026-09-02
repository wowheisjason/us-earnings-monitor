from io import BytesIO

from openpyxl import Workbook

from us_earnings_monitor.extract import _inline_xbrl_facts, _xlsx_relevant_text


def test_xlsx_extraction_keeps_financial_rows_only():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Quarter"
    sheet.append(["Company logo", "sample"])
    sheet.append(["Revenue", 12345, "USD millions"])
    stream = BytesIO()
    workbook.save(stream)

    result = _xlsx_relevant_text(stream.getvalue())
    assert "Revenue" in result
    assert "12345" in result
    assert "Company logo" not in result


def test_inline_xbrl_extraction_keeps_structured_financial_fact():
    html = b'''<html><body>
      <ix:nonfraction name="us-gaap:Revenues" contextref="CurrentQ2" unitref="USD" scale="6">12345</ix:nonfraction>
      <ix:nonfraction name="us-gaap:EntityCommonStockSharesOutstanding" contextref="CurrentQ2">999</ix:nonfraction>
    </body></html>'''
    facts = _inline_xbrl_facts(html)
    assert facts == [{
        "concept": "us-gaap:Revenues",
        "value": "12345",
        "context": "CurrentQ2",
        "unit": "USD",
        "scale": "6",
        "source_file": "inline-xbrl",
    }]

