from us_earnings_monitor.investor_analysis_v5_sparse import ProductionInvestorV5Client
from us_earnings_monitor.models import EarningsEvent


class CaptureClient(ProductionInvestorV5Client):
    def __init__(self):
        self.prompt = ""
        self.stage = ""
        self._last_mapper_started_at = 0.0

    def _json(self, prompt, stage, tools=None):
        self.prompt = prompt
        self.stage = stage
        return {"processed_unit_ids": ["doc#u1"], "cards": []}


def _unit():
    return {
        "unit_id": "doc#u1", "document_key": "doc", "title": "Transcript",
        "source": "official_ir", "document_kind": "transcript", "phase": "qa",
        "position": 1, "text": "Operator procedural text with no investment-material information.",
    }


def test_sparse_mapper_forbids_null_schema_padding_and_acknowledges_empty_units(monkeypatch):
    monkeypatch.setenv("GEMINI_V5_MAPPER_MIN_INTERVAL_SECONDS", "0")
    client = CaptureClient()
    event = EarningsEvent("SNOW_2026-07-31_Q2", "SNOW", 2027, "Q2", "2026-09-02T16:00:00-04:00")
    result = client._extract_batch(event, [_unit()], "v5_extract")
    assert result["processed_unit_ids"] == ["doc#u1"]
    assert "SPARSE JSON ONLY" in client.prompt
    assert "OMIT every null" in client.prompt
    assert "zero material cards" in client.prompt
    assert "Maximum 8 cards per unit" in client.prompt
    assert "metric,value,unit" in client.prompt
    assert '"metric":string|null' not in client.prompt


def test_mapper_pacing_waits_before_next_request(monkeypatch):
    client = CaptureClient()
    monkeypatch.setenv("GEMINI_V5_MAPPER_MIN_INTERVAL_SECONDS", "4.2")
    clock = {"now": 100.0}
    sleeps = []

    monkeypatch.setattr("us_earnings_monitor.investor_analysis_v5_sparse.time.monotonic", lambda: clock["now"])
    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds
    monkeypatch.setattr("us_earnings_monitor.investor_analysis_v5_sparse.time.sleep", fake_sleep)

    client._pace_mapper()
    clock["now"] += 1.0
    client._pace_mapper()
    assert sleeps and round(sleeps[0], 1) == 3.2
