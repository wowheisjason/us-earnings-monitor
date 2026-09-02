from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from openpyxl import load_workbook

from .models import Disclosure, Evidence

_KEYWORDS = ("revenue", "net sales", "operating income", "net income", "eps", "guidance",
             "outlook", "orders", "demand", "margin", "cash flow", "capital expenditure", "segment")


def _relevant_text(text: str, max_chars: int = 18000) -> str:
    chunks = re.split(r"(?:\n\s*\n|(?<=[。.!?])\s+)", text)
    selected = [chunk.strip() for chunk in chunks if any(k in chunk.casefold() for k in _KEYWORDS)]
    value = "\n".join(selected) or text
    return value[:max_chars]


def _xbrl_facts(blob: bytes) -> list[dict]:
    """Extract a compact, source-labelled set of numerical XBRL facts from a ZIP."""
    facts: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith((".xbrl", ".xml"))]
            for name in names[:8]:
                try:
                    root = ET.fromstring(archive.read(name))
                except ET.ParseError:
                    continue
                for elem in root.iter():
                    context = elem.attrib.get("contextRef")
                    value = (elem.text or "").strip()
                    if not context or not value or len(value) > 80:
                        continue
                    tag = elem.tag.rsplit("}", 1)[-1]
                    if any(word in tag.casefold() for word in ("revenue", "sales", "operatingincome", "profit", "eps", "netincome")):
                        facts.append({"concept": tag, "value": value, "context": context, "source_file": PurePosixPath(name).name})
                        if len(facts) >= 80:
                            return facts
    except zipfile.BadZipFile:
        return []
    return facts


def _inline_xbrl_facts(blob: bytes) -> list[dict]:
    """Extract compact facts from JPX inline-XBRL HTML without an AI call."""
    soup = BeautifulSoup(blob, "lxml")
    facts: list[dict] = []
    concepts = ("revenue", "sales", "operatingincome", "profit", "eps", "netincome")
    for tag in soup.find_all(True):
        if not tag.name.casefold().endswith("nonfraction"):
            continue
        concept = str(tag.get("name", ""))
        if not concept or not any(word in concept.casefold() for word in concepts):
            continue
        value = tag.get_text("", strip=True)
        if not value:
            continue
        facts.append({
            "concept": concept,
            "value": value,
            "context": tag.get("contextref") or tag.get("contextRef"),
            "unit": tag.get("unitref") or tag.get("unitRef"),
            "scale": tag.get("scale"),
            "source_file": "inline-xbrl",
        })
        if len(facts) >= 80:
            break
    return facts


def _xlsx_relevant_text(blob: bytes, max_chars: int = 18000) -> str:
    """Read formula results from official supplementary workbooks, row by row."""
    workbook = load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    selected: list[str] = []
    for sheet in workbook.worksheets[:20]:
        for row in sheet.iter_rows(max_row=600, max_col=50, values_only=True):
            values = [str(value).strip() for value in row if value is not None and str(value).strip()]
            if not values:
                continue
            line = " | ".join(values)
            if any(keyword in line.casefold() for keyword in _KEYWORDS):
                selected.append(f"[{sheet.title}] {line}")
            if sum(len(item) for item in selected) >= max_chars:
                return "\n".join(selected)[:max_chars]
    return "\n".join(selected)[:max_chars]


class EvidenceExtractor:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def fetch(self, disclosure: Disclosure) -> Evidence:
        if not disclosure.document_url:
            return Evidence(disclosure.key, disclosure.title, disclosure.url, "")
        response = self.session.get(disclosure.document_url, timeout=45, headers={
            "User-Agent": "us-earnings-monitor/0.1 https://github.com/wowheisjason/us-earnings-monitor"})
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").casefold()
        blob = response.content
        if "spreadsheet" in content_type or disclosure.document_url.casefold().split("?", 1)[0].endswith(".xlsx"):
            return Evidence(disclosure.key, disclosure.title, disclosure.url, _xlsx_relevant_text(blob))
        if "zip" in content_type or blob[:2] == b"PK":
            facts = _xbrl_facts(blob)
            return Evidence(disclosure.key, disclosure.title, disclosure.url, "", facts)
        if "pdf" in content_type or disclosure.document_url.casefold().split("?", 1)[0].endswith(".pdf"):
            reader = PdfReader(io.BytesIO(blob))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:30])
        else:
            text = BeautifulSoup(blob, "html.parser").get_text("\n", strip=True)
        facts = _inline_xbrl_facts(blob) if "html" in content_type or disclosure.document_url.casefold().endswith((".htm", ".html")) else []
        return Evidence(disclosure.key, disclosure.title, disclosure.url, _relevant_text(text), facts)

