from __future__ import annotations

import pytest

from us_earnings_monitor.checkpointing import put_stage
from us_earnings_monitor.investor_analysis_runtime import ProductionInvestorV3Client
from us_earnings_monitor.models import EarningsEvent, Evidence


EMPTY_PART = {
    "facts": [],
    "guidance": [],
    "segments": [],
    "cash_flow_and_capex": [],
    "quantitative_evidence": [],
    "change_signals": [],
    "customer_cases": [],
    "industry_signals": [],
    "management_claims": [],
    "management_comments": [],
    "qa": [],
    "market_consensus": [],
    "unknowns": [],
}


class ChunkFailsOnceClient(ProductionInvestorV3Client):
    def __init__(self):
        super().__init__(api_key="test")
        self.chunk_calls = 0
        self.consolidation_calls = 0

    def _extract_group(self, event, payload, *, partial):
        self.chunk_calls += 1
        if self.chunk_calls == 2:
            raise RuntimeError("503 second facts chunk")
        return {**EMPTY_PART, "event_id": event.event_id}

    def _consolidate_extractions(self, event, merged):
        # Production must not call this legacy LLM consolidation stage anymore.
        self.consolidation_calls += 1
        return {**EMPTY_PART, "event_id": event.event_id}

    def _extract_qa(self, event, evidence):
        return []

    def _cross_context_clusters(self, facts):
        return []


def test_second_chunk_failure_reuses_first_chunk_on_resume_without_llm_consolidation():
    # Four ~7k paragraphs deterministically form two <=22k extraction groups.
    text = "\n\n".join([character * 7000 for character in ("A", "B", "C", "D")])
    evidence = [Evidence("doc:1", "Earnings Call Transcript", "https://example.com", text, [])]
    event = EarningsEvent(
        "SNOW_FY2027_Q2",
        "SNOW",
        2027,
        "Q2",
        "2026-09-02T16:00:00-04:00",
    )
    checkpoint = {"pipeline_version": 2, "evidence_fingerprint": "test", "stages": {}}
    client = ChunkFailsOnceClient()
    client.configure_analysis_checkpoint(
        checkpoint,
        lambda stage, payload: put_stage(checkpoint, stage, payload),
    )

    with pytest.raises(RuntimeError, match="second facts chunk"):
        client.extract_facts(event, evidence)

    assert "facts_chunk_1" in checkpoint["stages"]
    assert "facts_chunk_2" not in checkpoint["stages"]
    assert client.chunk_calls == 2

    facts = client.extract_facts(event, evidence)

    # Resume calls only the missing second chunk; chunk 1 is not regenerated.
    assert client.chunk_calls == 3
    # The high-token consolidation LLM is deliberately gone.
    assert client.consolidation_calls == 0
    assert "facts_chunk_2" in checkpoint["stages"]
    assert "facts_deterministic_merge" in checkpoint["stages"]
    assert "cross_context_internal" in checkpoint["stages"]
    assert facts["extraction_group_count"] == 2
