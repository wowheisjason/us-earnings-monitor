from us_earnings_monitor.extract import EvidenceExtractor
from us_earnings_monitor.models import Disclosure


class Response:
    headers = {"content-type": "text/html"}
    content = b"<html><body>Revenue was 10.</body></html>"
    def raise_for_status(self): return None


class Session:
    def __init__(self): self.headers = None
    def get(self, url, **kwargs): self.headers = kwargs["headers"]; return Response()


def test_sec_evidence_uses_configured_sec_user_agent(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "monitor/0.1 contact@example.com")
    session = Session()
    doc = Disclosure("sec_edgar", "a", "DELL", "Earnings", "2026-09-01T00:00:00-04:00", "https://www.sec.gov/file.htm", document_url="https://www.sec.gov/file.htm")
    EvidenceExtractor(session=session).fetch(doc)
    assert session.headers["User-Agent"] == "monitor/0.1 contact@example.com"

