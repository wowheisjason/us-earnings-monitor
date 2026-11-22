"""Isolated historical source/model replay + separately labeled Telegram service test.

Never resends a historical report. Real provider failures remain failures.
"""
import argparse
import hashlib
import importlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path


def valid_receipt(receipt):
    return receipt.get("status") == "accepted" and type(receipt.get("message_id")) is int and receipt["message_id"] > 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("market", choices=("us", "jp"))
    parser.add_argument("--sources-only", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    package = args.market + "_earnings_monitor"
    runner = importlib.import_module(package + ".__main__")
    Store = importlib.import_module(package + ".state").StateStore
    models = importlib.import_module(package + ".models")
    production_path = Path("data/state.json")
    before = hashlib.sha256(production_path.read_bytes()).hexdigest()
    out = Path(".verification/qa-fallback")
    out.mkdir(parents=True, exist_ok=True)
    store = Store(out / "state.json")
    if not store.all_events():
        if args.market == "jp":
            _, patterns = runner.load_watchlist("watchlist.yaml")
            runner.ingest(runner.load_fixture("fixtures/fujikura_2026-08-07.json"), store, patterns,
                          datetime.fromisoformat("2026-08-07T20:30:00+09:00"))
        else:
            original = json.loads(production_path.read_text(encoding="utf-8"))
            raw = original["events"]["MU_2026-09-30"]
            event = models.EarningsEvent.from_dict(raw)
            event.status, event.report_version, event.last_analyzed_document_count, event.delivery = "collecting", 0, 0, {}
            event.collection_status.pop("analysis_recovery", None)
            event.collection_status.pop("analyzed_document_keys", None)
            store.put_event(event)
            for key in event.documents:
                store.data["documents"][key] = original["documents"][key]
        store.save()
    summary = {"market": args.market, "historical_reports_sent": 0, "production_state_sha256": before,
               "source_checks": [], "report_gate": "not_run", "telegram_service_test": "not_run",
               "checkpoint_reuse": bool(store.get_analysis_checkpoint(store.all_events()[0].event_id))}
    try:
        event = store.all_events()[0]
        companies, patterns = runner.load_watchlist("watchlist.yaml")
        company = next(company for company in companies if company.ticker == event.ticker)
        replay_now = datetime.fromisoformat(event.first_seen_at)
        # Exercise the actual discovery/fallback route in isolated state, not
        # only previously known financial URLs. Failure is recorded, not hidden.
        if args.market == "us":
            period_documents = [store.get_document(key) for key in event.documents]
            period_evidence = []
            for doc in period_documents:
                if doc.source == "sec_edgar" and doc.document_kind == "financial_results":
                    try:
                        period_evidence.append(runner.EvidenceExtractor().fetch(doc))
                    except Exception:
                        pass  # Incomplete is safer than a guessed fiscal period.
            importlib.import_module(package + ".grouping").repair_incomplete_event_period(event, period_documents, official_evidence=period_evidence)
            store.put_event(event)
            router_module = importlib.import_module(package + ".retrieval")
            client = None if args.sources_only else importlib.import_module(package + ".analysis").build_ir_research_client()
            collected = router_module.IrRetrievalRouter(client).collect(company, event, replay_now, dry_run=args.sources_only)
            summary["fallback_retrieval"] = collected.status
            runner.ingest(collected.documents, store, patterns, replay_now)
        else:
            official = importlib.import_module(package + ".sources.official_ir").OfficialIrAdapter([event]).discover([company], replay_now.date())
            runner.ingest(official, store, patterns, replay_now)
            current = store.get_event(event.event_id)
            if not any(store.get_document(key).document_kind in {"qa", "transcript"} for key in current.documents):
                media, status = importlib.import_module(package + ".sources.credible_media").CredibleMediaAdapter().fetch(company, event, replay_now)
                runner.ingest(media, store, patterns, replay_now)
                summary["fallback_retrieval"] = status
        event = store.get_event(event.event_id)
        for key in event.documents:
            doc = store.get_document(key)
            try:
                evidence = runner.EvidenceExtractor().fetch(doc)
                summary["source_checks"].append({"key": key, "url": doc.document_url or doc.url,
                    "published_at": doc.published_at, "retrieved_at": datetime.now().astimezone().isoformat(),
                    "usable": bool(evidence.text or evidence.structured_facts), "chars": len(evidence.text),
                    "primary_official": doc.source in {"official_ir", "tdnet", "edinet", "jpx_listed_company", "sec_edgar"} and doc.document_kind in {"financial_results", "financial_tables", "performance_review"}})
            except Exception as exc:
                summary["source_checks"].append({"key": key, "url": doc.document_url or doc.url, "error_type": type(exc).__name__})
        if args.sources_only:
            return 0 if any(s.get("usable") and s.get("primary_official") for s in summary["source_checks"]) else 1
        if not os.getenv("GEMINI_API_KEY"):
            summary["report_gate"] = "blocked_missing_provider_credentials"
            return 1
        real_send = runner.send_report
        def capture_report(text, **kwargs):
            (out / "audited-report.txt").write_text("【測試｜隔離歷史回放，不推播此歷史報告】\n" + text, encoding="utf-8")
            return 1  # Intercepted boundary only; never represented as a real receipt.
        runner.send_report = capture_report
        if args.market == "jp":
            outcome = runner._run_analysis(event, store, False)
        else:
            build_client = importlib.import_module(package + ".analysis").build_analysis_client
            outcome = runner._run_analysis(event, store, build_client(), False, datetime.fromisoformat("2026-10-04T20:30:00-04:00"))
        summary["report_gate"] = outcome
        event.delivery = {"status": "intercepted_test_boundary", "real_message_sent": False}
        store.put_event(event)
        store.save()
        if outcome != "published":
            return 1
        receipt_path = out / "telegram-service-receipt.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            summary["telegram_service_test"] = receipt
            return 0 if valid_receipt(receipt) else 1
        # Persist intent before transport. Never automatically retry an ambiguous send.
        receipt = {"status": "sending", "market": args.market, "kind": "labeled_service_test"}
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        message_id = real_send(f"【測試】{args.market.upper()} 財報流程服務驗證：隔離歷史資料已通過本次分析與審核。此訊息為連線測試，沒有重發歷史報告，也不代表 12 小時 SLA 已達成。")
        receipt.update({"status": "accepted", "message_id": message_id, "accepted_at": datetime.now().astimezone().isoformat()})
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["telegram_service_test"] = receipt
        return 0
    except Exception as exc:
        summary["failure_type"] = type(exc).__name__
        return 1
    finally:
        summary["production_state_unchanged"] = before == hashlib.sha256(production_path.read_bytes()).hexdigest()
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False))
        if not summary["production_state_unchanged"]:
            raise RuntimeError("production state changed during verification")


if __name__ == "__main__":
    raise SystemExit(main())
