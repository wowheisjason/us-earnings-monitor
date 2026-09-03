from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ..grouping import infer_period
from ..models import Company, Disclosure
from .base import SourceAdapter

LOG = logging.getLogger(__name__)

_PERIODIC_FORMS = {"10-Q", "10-Q/A", "10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
_CURRENT_FORMS = {"8-K", "8-K/A", "6-K", "6-K/A"}
_RELEVANT_TERMS = (
    "earnings", "financial results", "quarterly results", "annual results", "shareholder letter",
    "investor presentation", "earnings presentation", "results presentation", "press release",
)
_EXHIBIT_PERIOD = re.compile(r"(?:q([1-4])|([1-4])q)[^a-z0-9]*fy(20)?(\d{2})", re.IGNORECASE)
_PERIOD_END_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2})[-_./]?(\d{2})[-_./]?(\d{2})(?!\d)"),
    re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)"),
)


class SecEdgarAdapter(SourceAdapter):
    """Official SEC EDGAR submissions and filing-attachment adapter."""

    source_name = "sec_edgar"
    submissions_url = "https://data.sec.gov/submissions/CIK{cik}.json"
    archives_root = "https://www.sec.gov/Archives/edgar/data"
    timeout = 30

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.headers = {
            "User-Agent": os.getenv(
                "SEC_USER_AGENT",
                "us-earnings-monitor/0.1 https://github.com/wowheisjason/us-earnings-monitor",
            ),
            "Accept-Encoding": "gzip, deflate",
        }

    @staticmethod
    def _records(payload: dict) -> list[dict]:
        recent = payload.get("filings", {}).get("recent", {})
        keys = [key for key, value in recent.items() if isinstance(value, list)]
        count = max((len(recent[key]) for key in keys), default=0)
        records = [{key: recent[key][index] if index < len(recent[key]) else "" for key in keys}
                   for index in range(count)]
        for record in records:
            record["_fiscalYearEnd"] = str(payload.get("fiscalYearEnd", ""))
        return records

    @staticmethod
    def _quarter(record: dict, records: list[dict]) -> str | None:
        form = str(record.get("form", "")).upper()
        if form.startswith(("10-K", "20-F", "40-F")):
            return "Q4"
        if not form.startswith("10-Q"):
            probe = Disclosure("sec_edgar", "probe", None, str(record.get("primaryDocDescription", "")), "", "")
            return infer_period(probe)[1]
        report_date = str(record.get("reportDate", ""))
        fiscal_year_end = str(record.get("_fiscalYearEnd", ""))
        if report_date and len(fiscal_year_end) == 4:
            report_month = int(report_date[5:7])
            end_month = int(fiscal_year_end[:2])
            distance = (report_month - end_month) % 12
            nearest = min((3, 6, 9), key=lambda value: abs(value - distance))
            return {3: "Q1", 6: "Q2", 9: "Q3"}[nearest]
        annual_dates = sorted({str(item.get("reportDate", "")) for item in records
                               if str(item.get("form", "")).upper().startswith(("10-K", "20-F", "40-F"))
                               and str(item.get("reportDate", "")) < report_date})
        last_annual = annual_dates[-1] if annual_dates else ""
        quarterly_dates = sorted({str(item.get("reportDate", "")) for item in records
                                  if str(item.get("form", "")).upper().startswith("10-Q")
                                  and last_annual < str(item.get("reportDate", "")) <= report_date})
        return f"Q{min(3, max(1, len(quarterly_dates)))}"

    @staticmethod
    def _fiscal_year(record: dict, report_date: str | None) -> int | None:
        if not report_date:
            return None
        fiscal_year_end = str(record.get("_fiscalYearEnd", ""))
        if len(fiscal_year_end) != 4:
            return int(report_date[:4])
        period_mmdd = report_date[5:7] + report_date[8:10]
        return int(report_date[:4]) + (1 if period_mmdd > fiscal_year_end else 0)

    @staticmethod
    def _period_end_from_text(*values: str) -> str | None:
        """Extract a reporting-period end embedded in an issuer filename/title."""
        for value in values:
            for index, pattern in enumerate(_PERIOD_END_PATTERNS):
                match = pattern.search(str(value or ""))
                if not match:
                    continue
                parts = match.groups()
                year, month, day = (parts if index == 0 else (parts[2], parts[0], parts[1]))
                try:
                    return date(int(year), int(month), int(day)).isoformat()
                except ValueError:
                    continue
        return None

    @staticmethod
    def _quarter_for_period_end(period_end: str | None, fiscal_year_end: str) -> str | None:
        if not period_end or len(fiscal_year_end) != 4:
            return None
        try:
            report_month = int(period_end[5:7])
            end_month = int(fiscal_year_end[:2])
        except (TypeError, ValueError):
            return None
        distance = (report_month - end_month) % 12
        nearest = min((3, 6, 9), key=lambda value: abs(value - distance))
        return {3: "Q1", 6: "Q2", 9: "Q3"}[nearest]

    @staticmethod
    def _current_form_is_relevant(record: dict) -> bool:
        form = str(record.get("form", "")).upper()
        if form.startswith("8-K"):
            items = str(record.get("items", ""))
            return "2.02" in items or "7.01" in items
        return form.startswith("6-K")

    def _attachment_rows(self, company: Company, record: dict) -> list[tuple[str, str, str]]:
        accession = str(record["accessionNumber"])
        folder = f"{self.archives_root}/{int(company.cik)}/{accession.replace('-', '')}/"
        index_url = urljoin(folder, f"{accession}-index.html")
        response = self.session.get(index_url, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "lxml")
        rows: list[tuple[str, str, str]] = []
        for row in soup.select("table.tableFile tr"):
            cells = row.select("td")
            link = row.select_one("a[href]")
            if len(cells) < 4 or link is None:
                continue
            description = cells[1].get_text(" ", strip=True)
            document_type = cells[3].get_text(" ", strip=True).upper()
            document_url = urljoin(folder, link.get("href", ""))
            if document_url.casefold().split("?", 1)[0].endswith((".jpg", ".jpeg", ".gif", ".png", ".css", ".xml", ".xsd")):
                continue
            rows.append((description or document_type, document_type, document_url))
        return rows

    def _period_for_current(self, record: dict, records: list[dict]) -> tuple[str | None, str | None]:
        filing_date = str(record.get("filingDate", ""))
        candidates = [item for item in records if str(item.get("form", "")).upper() in _PERIODIC_FORMS
                      and item.get("reportDate") and abs((date.fromisoformat(str(item["filingDate"])) - date.fromisoformat(filing_date)).days) <= 3]
        if not candidates:
            return None, None
        chosen = min(candidates, key=lambda item: abs((date.fromisoformat(str(item["filingDate"])) - date.fromisoformat(filing_date)).days))
        return str(chosen.get("reportDate") or "") or None, self._quarter(chosen, records)

    def _disclosures(self, company: Company, record: dict, records: list[dict]) -> list[Disclosure]:
        form = str(record.get("form", "")).upper()
        accession = str(record["accessionNumber"])
        filing_date = str(record["filingDate"])
        # SEC's 8-K reportDate is normally the event/filing date, not the
        # earnings period end. Only periodic filings may use it directly.
        report_date = (str(record.get("reportDate") or "") or None) if form in _PERIODIC_FORMS else None
        quarter = self._quarter(record, records) if form in _PERIODIC_FORMS else None
        if form in _CURRENT_FORMS:
            report_date, quarter = self._period_for_current(record, records)

        if form in _PERIODIC_FORMS:
            primary = str(record.get("primaryDocument", ""))
            folder = f"{self.archives_root}/{int(company.cik)}/{accession.replace('-', '')}/"
            rows = [(str(record.get("primaryDocDescription") or f"Form {form}"), form, urljoin(folder, primary))]
        else:
            rows = self._attachment_rows(company, record)

        attachment_period_end = next(
            (period for description, document_type, document_url in rows
             if document_type.startswith("EX-99")
             and (period := self._period_end_from_text(description, document_url))),
            None,
        )
        period_end = report_date or attachment_period_end
        fiscal_year_end = str(record.get("_fiscalYearEnd", ""))
        quarter = quarter or self._quarter_for_period_end(period_end, fiscal_year_end)
        fiscal_year = self._fiscal_year(record, period_end)
        earnings_event = form.startswith("8-K") and "2.02" in str(record.get("items", ""))
        if form.startswith("6-K"):
            earnings_event = any(term in f"{record.get('primaryDocDescription', '')} {record.get('primaryDocument', '')}".casefold()
                                 for term in _RELEVANT_TERMS)

        found: list[Disclosure] = []
        for description, document_type, document_url in rows:
            is_primary = document_type == form
            is_exhibit = document_type.startswith("EX-99")
            filename = document_url.rsplit("/", 1)[-1]
            relevant_text = f"{description} {record.get('primaryDocDescription', '')} {filename}".casefold()
            if form in _CURRENT_FORMS and not is_primary and not (
                is_exhibit and (earnings_event or any(term in relevant_text for term in _RELEVANT_TERMS))
            ):
                continue
            if form.startswith("6-K") and not (is_primary or any(term in relevant_text for term in _RELEVANT_TERMS)):
                continue
            exhibit_period = _EXHIBIT_PERIOD.search(filename)
            filename_fy = (int(f"20{exhibit_period.group(4)}") if exhibit_period.group(3)
                           else 2000 + int(exhibit_period.group(4))) if exhibit_period else None
            filename_quarter = f"Q{exhibit_period.group(1) or exhibit_period.group(2)}" if exhibit_period else None
            filename_period_end = self._period_end_from_text(description, filename) if is_exhibit else None
            row_period_end = filename_period_end or period_end
            display_description = filename if is_exhibit and exhibit_period else description
            title = f"{company.name} Form {form} — {display_description}"
            if earnings_event and "earnings" not in title.casefold() and "results" not in title.casefold():
                title = f"{company.name} Earnings results — {title}"
            probe = Disclosure("sec_edgar", "probe", company.ticker, title, filing_date, document_url)
            inferred_fy, inferred_quarter = infer_period(probe)
            accepted = str(record.get("acceptanceDateTime") or "")
            published_at = accepted if accepted else f"{filing_date}T00:00:00-04:00"
            found.append(Disclosure(
                source=self.source_name,
                source_id=f"{accession}:{document_url.rsplit('/', 1)[-1]}",
                ticker=company.ticker,
                title=title,
                published_at=published_at,
                url=document_url,
                document_url=document_url,
                fiscal_year=filename_fy or self._fiscal_year(record, row_period_end) or fiscal_year or inferred_fy,
                quarter=filename_quarter or quarter or self._quarter_for_period_end(row_period_end, fiscal_year_end) or inferred_quarter,
                period_end=row_period_end,
                metadata={"service": self.source_name, "cik": company.cik, "accession": accession,
                          "form": form, "document_type": document_type, "filing_date": filing_date,
                          "earnings_event": earnings_event,
                          "period_end_source": "attachment_filename" if filename_period_end
                          else "periodic_anchor" if report_date else None},
            ))
        return found

    def discover(self, companies: list[Company], day: date) -> list[Disclosure]:
        earliest = day - timedelta(days=3)
        found: list[Disclosure] = []
        for company in companies:
            try:
                response = self.session.get(self.submissions_url.format(cik=company.cik.zfill(10)),
                                            headers=self.headers, timeout=self.timeout)
                response.raise_for_status()
                records = self._records(response.json())
                for record in records:
                    filing_date = str(record.get("filingDate", ""))
                    form = str(record.get("form", "")).upper()
                    if not filing_date or not (earliest <= date.fromisoformat(filing_date) <= day):
                        continue
                    if form not in _PERIODIC_FORMS | _CURRENT_FORMS:
                        continue
                    if form in _CURRENT_FORMS and not self._current_form_is_relevant(record):
                        continue
                    found.extend(self._disclosures(company, record, records))
            except Exception as exc:  # noqa: BLE001 - one issuer cannot stop the watchlist
                LOG.warning("SEC EDGAR unavailable for %s (CIK %s): %s", company.ticker, company.cik, exc)
        return found

