from us_earnings_monitor.investor_analysis_v5_sparse import ProductionInvestorV5Client
from us_earnings_monitor.models import EarningsEvent


class CaptureClient(ProductionInvestorV5Client):
    def __init__(self):
        self.prompt = ""
        self.stage = ""

    def _json(self, prompt, stage, tools=None):
        self.prompt = prompt
        self.stage = stage
        return {"processed_unit_ids": ["doc#u1"], "cards": []}


def test_sparse_mapper_forbids_null_schema_padding_and_acknowledges_empty_units():
    client = CaptureClient()
    event = EarningsEvent("SNOW_2026-07-31_Q2", "SNOW", 2027, "Q2", "2026-09-02T16:00:00-04:00")
    result = client._extract_batch(event, [{
        "unit_id": "doc#u1", "document_key": "doc", "title": "Transcript",
        "source": "official_ir", "document_kind": "transcript", "phase": "qa",
        "position": 1, "text": "Operator procedural text with no investment-material information.",
    }], "v5_extract")
    assert result["processed_unit_ids"] == ["doc#u1"]
    assert "SPARSE JSON ONLY" in client.prompt
    assert "OMIT every null" in client.prompt
    assert "zero material cards" in client.prompt
    assert "Maximum 8 cards per unit" in client.prompt
    assert "metric,value,unit" in client.prompt
    assert '"metric":string|null' not in client.prompt
