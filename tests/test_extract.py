from io import BytesIO

from openpyxl import Workbook

from us_earnings_monitor.extract import EvidenceExtractor, _inline_xbrl_facts, _relevant_text, _xlsx_relevant_text
from us_earnings_monitor.models import Disclosure


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


class FakeResponse:
    def __init__(self, content: bytes, content_type: str = "text/html"):
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


def test_transcript_extraction_preserves_non_keyword_qa_context():
    transcript = b"""<html><body>
    <p>Operator: We will now begin the question-and-answer session.</p>
    <p>Analyst: Is the recent server demand mostly a pull-forward?</p>
    <p>CFO: No. We see broad modernization across several customer groups and the installed base remains old.</p>
    </body></html>"""
    disclosure = Disclosure(
        "official_ir", "t1", "DELL", "Transcript", "2026-09-01T20:00:00-04:00",
        "https://ir.example/static-files/uuid", document_url="https://ir.example/static-files/uuid",
        fiscal_year=2027, quarter="Q2", document_kind="transcript",
    )
    evidence = EvidenceExtractor(session=FakeSession(FakeResponse(transcript))).fetch(disclosure)
    assert "question-and-answer session" in evidence.text
    assert "pull-forward" in evidence.text
    assert "installed base remains old" in evidence.text


def test_financial_relevance_keeps_adjusted_fcf_reconciliation_lines():
    text = """Operating cash flow was $2.225 billion.\n\nAdjusted free cash flow was $8.149 billion.\n\nReconciliation: financing receivables of $6.667 billion and equipment under operating leases of $0.496 billion were added back."""
    retained = _relevant_text(text)
    for value in ("Adjusted free cash flow", "Reconciliation", "financing receivables", "equipment under operating leases"):
        assert value in retained
