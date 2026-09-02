from datetime import date

from us_earnings_monitor.models import Company
from us_earnings_monitor.sources.sec import SecEdgarAdapter


class Response:
    def __init__(self, payload=None, content=b""): self.payload, self.content = payload, content
    def json(self): return self.payload
    def raise_for_status(self): return None


class Session:
    def get(self, url, **kwargs):
        if "submissions" in url:
            return Response({"fiscalYearEnd": "1231", "filings": {"recent": {
                "accessionNumber": ["0001181412-26-000001", "0001181412-26-000002"],
                "filingDate": ["2026-08-04", "2026-08-04"], "reportDate": ["2026-08-04", "2026-06-30"],
                "acceptanceDateTime": ["2026-08-04T16:05:00.000Z", "2026-08-04T17:00:00.000Z"],
                "form": ["8-K", "10-Q"], "primaryDocument": ["spcx-8k.htm", "spcx-10q.htm"],
                "primaryDocDescription": ["8-K", "10-Q"], "items": ["2.02,9.01", ""]
            }}})
        html = b'''<table class="tableFile"><tr><th>Seq</th></tr>
        <tr><td>1</td><td>8-K</td><td><a href="spcx-8k.htm">spcx-8k.htm</a></td><td>8-K</td><td>1</td></tr>
        <tr><td>2</td><td>Q2 2026 Earnings Release</td><td><a href="earnings.htm">earnings.htm</a></td><td>EX-99.1</td><td>1</td></tr>
        <tr><td>3</td><td>Employment agreement</td><td><a href="other.htm">other.htm</a></td><td>EX-10.1</td><td>1</td></tr></table>'''
        return Response(content=html)


def test_sec_discovers_periodic_filing_and_relevant_earnings_exhibit():
    company = Company("SPCX", "SpaceX", "0001181412")
    docs = SecEdgarAdapter(session=Session()).discover([company], date(2026, 8, 4))
    assert len(docs) == 3
    assert {doc.metadata["document_type"] for doc in docs} == {"8-K", "EX-99.1", "10-Q"}
    assert all(doc.period_end == "2026-06-30" for doc in docs)
    assert all((doc.fiscal_year, doc.quarter) == (2026, "Q2") for doc in docs)

