from __future__ import annotations

import argparse
import html
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .analysis import build_analysis_client
from .calendar import is_likely_trading_day
from .config import load_watchlist
from .extract import EvidenceExtractor
from .grouping import align_companion_periods, attach, classify_document, ready_for_analysis, title_is_earnings
from .models import Disclosure, EarningsEvent, now_iso
from .quality import publication_gate, update_collection_status
from .sources import OfficialIrAdapter, SecEdgarAdapter, active_events_for_ir
from .state import StateStore
from .telegram import send_report
from .validation import validate_extracted_facts

ET = ZoneInfo("America/New_York")
LOG = logging.getLogger("us_earnings_monitor")
REPORT_MAX_CHARS = 4_000


def _format_report_html(text: str) -> str:
    marker = "📈 關鍵指標:\n"
    next_marker = "\n\n🏢 業務部門:"
    if marker not in text or next_marker not in text:
        return html.escape(text)
    before, remainder = text.split(marker, 1)
    table, after = remainder.split(next_marker, 1)
    return (html.escape(before) + "📈 關鍵指標:\n<pre>" + html.escape(table.strip())
            + "</pre>\n\n🏢 業務部門:" + html.escape(after))


def _compose_report(text: str, documents: list[Disclosure]) -> str:
    source_lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for item in documents:
        if item.metadata.get("format") == "ixbrl":
            continue
        key = (item.title.removesuffix(" [XBRL]"), item.url)
        if key in seen:
            continue
        seen.add(key)
        source_lines.append(f'• <a href="{html.escape(item.url, quote=True)}">{html.escape(key[0])}</a>')
    sources = "\n\n資料來源:\n" + "\n".join(source_lines)
    body = text.strip()
    while body and len(_format_report_html(body)) + len(sources) > REPORT_MAX_CHARS:
        body = body[:-100].rstrip()
    if body != text.strip():
        body = body.rstrip("…") + "…"
    return _format_report_html(body) + sources


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Official-source US earnings monitor")
    parser.add_argument("--watchlist", default="watchlist.yaml")
    parser.add_argument("--state", default="data/state.json")
    parser.add_argument("--fixture", help="Use normalized disclosure fixture instead of network sources")
    parser.add_argument("--dry-run", action="store_true", help="Never call the analysis provider, Telegram, or write state")
    parser.add_argument("--baseline", action="store_true", help="Record current documents as already processed; never analyze or notify")
    parser.add_argument("--at", help="ISO datetime for deterministic tests; defaults to current America/New_York time")
    parser.add_argument("--tickers", help="Comma-separated ticker allowlist for an authorized manual test")
    return parser.parse_args()


def load_fixture(path: str) -> list[Disclosure]:
    return [Disclosure.from_dict(value) for value in json.loads(Path(path).read_text(encoding="utf-8"))]


def discover(args: argparse.Namespace, companies, now: datetime, skip_sources: set[str] | None = None) -> tuple[list[Disclosure], set[str]]:
    if args.fixture:
        return load_fixture(args.fixture), {"fixture"}
    results: list[Disclosure] = []
    succeeded: set[str] = set()
    skip_sources = skip_sources or set()
    for adapter in (SecEdgarAdapter(),):
        source_name = getattr(adapter, "source_name", type(adapter).__name__)
        if source_name in skip_sources:
            continue
        try:
            results.extend(adapter.discover(companies, now.date()))
            succeeded.add(source_name)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("%s discovery failed: %s", type(adapter).__name__, exc)
    return results, succeeded


def ingest(disclosures: list[Disclosure], store: StateStore, patterns: list[str], now: datetime) -> tuple[list[EarningsEvent], int]:
    align_companion_periods(disclosures)
    changed: dict[str, EarningsEvent] = {}
    ignored = 0
    for disclosure in disclosures:
        disclosure.document_kind = classify_document(disclosure.title)
        if not disclosure.ticker or not title_is_earnings(disclosure.title, patterns):
            ignored += 1
            continue
        if store.seen_document(disclosure):
            continue
        from .grouping import event_id
        eid = event_id(disclosure)
        if not eid:
            LOG.info("Skipped ungroupable earnings disclosure: %s", disclosure.title)
            ignored += 1
            continue
        existing_event = store.get_event(eid)
        if store.equivalent_primary_document(disclosure, existing_event):
            LOG.info("Skipped official-IR mirror already collected from a primary source: %s", disclosure.title)
            continue
        event = attach(existing_event, disclosure, now)
        assert event is not None
        store.add_document(disclosure)
        store.put_event(event)
        changed[eid] = event
    return list(changed.values()), ignored


def _event_documents(event: EarningsEvent, store: StateStore) -> list[Disclosure]:
    return [store.get_document(key) for key in event.documents]


def _run_analysis(event: EarningsEvent, store: StateStore, dry_run: bool, now: datetime) -> str:
    docs = _event_documents(event, store)
    allowed, reasons, manifest = publication_gate(event, docs, now)
    LOG.info("%s source manifest: %s", event.event_id, manifest)
    if not allowed:
        LOG.info("%s publication gate pending: %s", event.event_id, reasons)
        return "collection_pending"
    if dry_run:
        return "dry_run"

    evidence = [EvidenceExtractor().fetch(doc) for doc in docs]
    client = build_analysis_client()
    facts = client.extract_facts(event, evidence)
    deterministic_issues = validate_extracted_facts(facts)
    facts["collection_status"] = event.collection_status
    facts["deterministic_validation_issues"] = deterministic_issues
    if deterministic_issues:
        LOG.warning("%s deterministic fact validation issues: %s", event.event_id, deterministic_issues)

    if event.status == "published" and not client.material_update(facts, event.last_analyzed_document_count, len(event.documents)):
        event.last_analyzed_document_count = len(event.documents)
        event.updated_at = now_iso(now)
        store.put_event(event)
        return "no_material_update"

    analysis = client.analyze(event, facts, evidence)
    audit = client.audit(event, facts, analysis, evidence)
    draft = audit.get("corrected_telegram_draft") or analysis.get("telegram_draft") or ""
    if (audit.get("overall_score", 0) < 90 or audit.get("unsupported_claims") or audit.get("numerical_errors")
            or audit.get("critical_issues") or deterministic_issues or len(draft) > REPORT_MAX_CHARS):
        analysis = client.revise(facts, analysis, audit)
        audit = client.audit(event, facts, analysis, evidence)

    if (audit.get("overall_score", 0) >= 90 and not audit.get("unsupported_claims")
            and not audit.get("numerical_errors") and not audit.get("critical_issues")
            and not deterministic_issues and audit.get("pass") is True):
        text = audit.get("corrected_telegram_draft") or analysis.get("telegram_draft") or ""
        if text:
            send_report(_compose_report(text, docs), parse_mode="HTML")
            event.status = "published"
            event.report_version += 1
            event.last_analyzed_document_count = len(event.documents)
            event.updated_at = now_iso(now)
            store.put_event(event)
            return "published"

    event.status = "needs_human_review"
    event.updated_at = now_iso(now)
    store.put_event(event)
    return "needs_human_review"


def mark_baseline(store: StateStore, now: datetime) -> int:
    count = 0
    for event in store.all_events():
        if len(event.documents) > event.last_analyzed_document_count:
            event.status = "published"
            event.last_analyzed_document_count = len(event.documents)
            event.updated_at = now_iso(now)
            store.put_event(event)
            count += 1
    return count


def main() -> int:
    args = parse_args()
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = datetime.fromisoformat(args.at).astimezone(ET) if args.at else datetime.now(ET)
    if not is_likely_trading_day(now):
        LOG.info("Not a weekday in America/New_York; no discovery run.")
        return 0
    companies, patterns = load_watchlist(args.watchlist)
    if args.tickers:
        requested = {ticker.strip().upper() for ticker in args.tickers.split(",") if ticker.strip()}
        companies = [company for company in companies if company.ticker.upper() in requested]
        if not companies:
            raise SystemExit("None of --tickers matched watchlist.yaml")
    store = StateStore(args.state)
    disclosures, _ = discover(args, companies, now)
    changed, ignored = ingest(disclosures, store, patterns, now)

    active_ir_events = active_events_for_ir(store.all_events(), now)
    if active_ir_events and not args.fixture:
        ir_disclosures = OfficialIrAdapter(active_ir_events).discover(companies, now.date())
        ir_changed, ir_ignored = ingest(ir_disclosures, store, patterns, now)
        changed_by_id = {event.event_id: event for event in [*changed, *ir_changed]}
        changed = list(changed_by_id.values())
        ignored += ir_ignored
        LOG.info("Official IR enrichment discovered %d document(s) for %d active event(s).", len(ir_disclosures), len(active_ir_events))

        for active in active_ir_events:
            current = store.get_event(active.event_id) or active
            update_collection_status(current, _event_documents(current, store), now, official_ir_checked=True)
            current.updated_at = now_iso(now)
            store.put_event(current)

    LOG.info("Discovered %d; changed events=%s; ignored=%d", len(disclosures), [e.event_id for e in changed], ignored)

    if args.baseline:
        LOG.info("Baseline recorded for %d events; no analysis-provider or Telegram calls were made.", mark_baseline(store, now))
        store.save()
        return 0

    pending = False
    for event in store.all_events():
        if event.status not in {"collecting", "published", "needs_human_review"} or len(event.documents) <= event.last_analyzed_document_count:
            continue
        if ready_for_analysis(event, now):
            outcome = _run_analysis(event, store, args.dry_run, now)
            LOG.info("%s: %s", event.event_id, outcome)
            pending = pending or outcome == "collection_pending"
        else:
            pending = True
    if pending:
        LOG.info("Documents collected; analysis is waiting for the scheduled window or additional official IR material.")
    if not args.dry_run:
        store.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
