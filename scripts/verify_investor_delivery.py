"""Authorized isolated historical live replay; real sources, Gemini and Telegram.
No production state is modified. Only an auditor-approved report may be sent.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from us_earnings_monitor import __main__ as runner
from us_earnings_monitor.models import EarningsEvent
from us_earnings_monitor.state import StateStore
from us_earnings_monitor.analysis import build_analysis_client

out = Path(".verification")
out.mkdir(exist_ok=True)
store = StateStore(out / "state.json")
if not store.all_events():
    original = json.loads(Path("data/state.json").read_text())
    raw = original["events"]["MU_2026-09-30"]
    event = EarningsEvent.from_dict(raw)
    event.status = "collecting"
    event.delivery = {}
    event.last_analyzed_document_count = 0
    store.put_event(event)
    for key in event.documents:
        store.data["documents"][key] = original["documents"][key]
    store.save()
original_compose = runner._compose_report
runner._compose_report = lambda *a, **k: original_compose("【歷史資料驗證｜非即時行情】\n" + a[0], *a[1:], **k)
os.environ["TELEGRAM_CAPTURE_PATH"] = str(out / "telegram.txt")
failed = False
for event in store.all_events():
    if event.delivery.get("status") == "accepted":
        continue
    try:
        outcome = runner._run_analysis(event, store, build_analysis_client(), False, datetime.fromisoformat("2026-10-04T20:30:00-04:00"))
    except Exception as exc:
        # Provider exception URLs may contain credentials. Keep diagnostics bounded.
        print("LIVE_FAILURE", type(exc).__name__)
        failed = True
        continue
    print("LIVE_OUTCOME", event.event_id, outcome)
    failed = failed or outcome != "published"
store.save()
receipts = [e.delivery for e in store.all_events() if e.delivery.get("status") == "accepted"]
(out / "receipts.json").write_text(json.dumps(receipts, ensure_ascii=False, indent=2))
for receipt in receipts:
    print("TELEGRAM_RECEIPT", receipt["message_id"])
raise SystemExit(1 if failed or not receipts else 0)
