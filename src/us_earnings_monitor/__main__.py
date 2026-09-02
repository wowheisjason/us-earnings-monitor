from __future__ import annotations

import argparse
import html
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .analysis import AnalysisClient, build_analysis_client, build_ir_research_client
from .calendar import is_likely_trading_day
from .config import load_watchlist
from .extract import EvidenceExtractor
from .grouping import align_companion_periods, attach, classify_document, ready_for_analysis, title_is_earnings
from .models import Disclosure, EarningsEvent, now_iso
from .quality import TRANSCRIPT_CONFIRMED_NONE, publication_gate, update_collection_status
from .retrieval import IrRetrievalRouter, schedule_next_ir_retry, should_attempt_ir
from .sources import SecEdgarAdapter, active_events_for_ir
from .state import StateStore
from .telegram import send_report
from .validation import validate_extracted_facts, validate_report_text

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
    parser.add_argument("--dry-run", action="store_true", help="No Gemini, Telegram, or state write; direct-source discovery only")
    parser.add_argument("--preview", action="store_true", help="Run live IR research + Gemini analysis, print report, no Telegram or state write")
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


def _run_analysis(event: EarningsEvent, store: StateStore, client: AnalysisClient, preview: bool, now: datetime) -> str:
    docs = _event_documents(event, store)
    allowed, reasons, manifest = publication_gate(event, docs, now)
    LOG.info("%s source manifest: %s", event.event_id, manifest)
    if not allowed:
        LOG.info("%s publication gate pending: %s", event.event_id, reasons)
        return "collection_pending"

    evidence = [EvidenceExtractor().fetch(doc) for doc in docs]
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
    report_issues = validate_report_text(draft)
    if report_issues:
        LOG.warning("%s deterministic report validation issues: %s", event.event_id, report_issues)

    if (audit.get("overall_score", 0) < 90 or audit.get("unsupported_claims") or audit.get("numerical_errors")
            or audit.get("critical_issues") or deterministic_issues or report_issues or len(draft) > REPORT_MAX_CHARS):
        facts["deterministic_report_issues"] = report_issues
        analysis = client.revise(facts, analysis, audit)
        audit = client.audit(event, facts, analysis, evidence)
        draft = audit.get("corrected_telegram_draft") or analysis.get("telegram_draft") or ""
        report_issues = validate_report_text(draft)

    if (audit.get("overall_score", 0) >= 90 and not audit.get("unsupported_claims")
            and not audit.get("numerical_errors") and not audit.get("critical_issues")
            and not deterministic_issues and not report_issues and audit.get("pass") is True):
        text = audit.get("corrected_telegram_draft") or analysis.get("telegram_draft") or ""
        if text:
            rendered = _compose_report(text, docs)
            if preview:
                print("PREVIEW_REPORT_BEGIN")
                print(rendered)
                print("PREVIEW_REPORT_END")
            else:
                send_report(rendered, parse_mode="HTML")
            event.status = "published"
            event.report_version += 1
            event.last_analyzed_document_count = len(event.documents)
            event.updated_at = now_iso(now)
            store.put_event(event)
            return "preview_published" if preview else "published"

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
    if args.dry_run and args.preview:
        raise SystemExit("--dry-run and --preview are mutually exclusive")
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
    company_by_ticker = {company.ticker: company for company in companies}

    store = StateStore(args.state)
    disclosures, _ = discover(args, companies, now)
    changed, ignored = ingest(disclosures, store, patterns, now)

    analysis_client: AnalysisClient | None = None
    active_ir_events = active_events_for_ir(store.all_events(), now)
    if active_ir_events and not args.fixture:
        research_client = None if args.dry_run else build_ir_research_client()
        router = IrRetrievalRouter(research_client)
        retrieval_results = {}
        ir_documents: list[Disclosure] = []

        for active in active_ir_events:
            if not should_attempt_ir(active, now):
                continue
            company = company_by_ticker.get(active.ticker)
            if not company:
                continue
            result = router.collect(company, active, now, dry_run=args.dry_run)
            retrieval_results[active.event_id] = result
            ir_documents.extend(result.documents)

        if ir_documents:
            ir_changed, ir_ignored = ingest(ir_documents, store, patterns, now)
            changed = list({event.event_id: event for event in [*changed, *ir_changed]}.values())
            ignored += ir_ignored

        for active in active_ir_events:
            result = retrieval_results.get(active.event_id)
            if result is None:
                continue
            current = store.get_event(active.event_id) or active
            status = result.status
            current.collection_status["ir_retrieval_provider"] = status.get("provider")
            current.collection_status["ir_retrieval_attempts"] = status.get("attempts", result.attempts)
            current.collection_status["ir_retrieval_last_at"] = now_iso(now)
            current.collection_status["gemini_ir_research_notes"] = status.get("research_notes", [])
            current.collection_status["gemini_ir_grounding"] = status.get("grounding", {})
            current.collection_status["gemini_ir_rejected_unofficial_urls"] = status.get("rejected_unofficial_urls", [])
            if status.get("model"):
                current.collection_status["gemini_ir_model"] = status["model"]
            call = status.get("call", {}) or {}
            if call.get("scheduled_at"):
                current.collection_status["earnings_call_scheduled_at"] = call["scheduled_at"]
            if call.get("status"):
                current.collection_status["earnings_call_status"] = call["status"]
            if status.get("transcript_status") == TRANSCRIPT_CONFIRMED_NONE:
                current.collection_status["transcript_status"] = TRANSCRIPT_CONFIRMED_NONE

            update_collection_status(current, _event_documents(current, store), now, official_ir_checked=result.complete)
            if not result.complete:
                current.collection_status["official_ir_last_attempt_incomplete"] = now_iso(now)
            else:
                current.collection_status.pop("official_ir_last_attempt_incomplete", None)
            schedule_next_ir_retry(current, now)
            current.updated_at = now_iso(now)
            store.put_event(current)

        LOG.info("IR enrichment: documents=%d active_events=%d attempted=%d",
                 len(ir_documents), len(active_ir_events), len(retrieval_results))

    LOG.info("Discovered %d SEC/fixture document(s); changed events=%s; ignored=%d",
             len(disclosures), [e.event_id for e in changed], ignored)

    if args.baseline:
        LOG.info("Baseline recorded for %d events; no analysis-provider or Telegram calls were made.", mark_baseline(store, now))
        if not args.preview and not args.dry_run:
            store.save()
        return 0

    pending = False
    for event in store.all_events():
        if event.status not in {"collecting", "published", "needs_human_review"} or len(event.documents) <= event.last_analyzed_document_count:
            continue
        if ready_for_analysis(event, now):
            if args.dry_run:
                outcome = "dry_run"
            else:
                analysis_client = analysis_client or build_analysis_client()
                outcome = _run_analysis(event, store, analysis_client, args.preview, now)
            LOG.info("%s: %s", event.event_id, outcome)
            pending = pending or outcome == "collection_pending"
        else:
            pending = True

    if pending:
        LOG.info("Documents collected; analysis is waiting for event completeness or additional official IR material.")
    if not args.dry_run and not args.preview:
        store.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
