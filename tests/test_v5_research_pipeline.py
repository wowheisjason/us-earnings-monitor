from __future__ import annotations

from us_earnings_monitor.analysis import build_analysis_client
from us_earnings_monitor.extract import EvidenceExtractor
from us_earnings_monitor.gemini_v2 import GeminiV2Client, _generation_config
from us_earnings_monitor.investor_analysis_v5 import ProductionInvestorV5Client
from us_earnings_monitor.models import Disclosure, EarningsEvent, Evidence
from us_earnings_monitor.research_packet import build_research_packet
from us_earnings_monitor.research_units import batch_units, coverage_result, unitize_evidence


def test_transcript_is_unitized_end_to_end_without_sampling():
    prepared = "Prepared remarks demand revenue margin. " * 1200
    qa = "\n\nQuestion-and-Answer Session\n\nAnalyst asks about demand. Management answers directly. " * 500
    raw = prepared + qa
    evidence = [Evidence(
        "official_ir:t1", "Company Q2 Earnings Call Transcript", "https://example.test/t1", raw, [],
        {"document_kind": "transcript", "source": "official_ir", "raw_chars": len(raw), "truncated": False},
    )]
    units, manifest = unitize_evidence(evidence)
    assert len(units) > 6  # V4 used to sample a maximum of six groups.
    assert any(unit["phase"] == "prepared_remarks" for unit in units)
    assert any(unit["phase"] == "qa" for unit in units)
    assert manifest["expected_unit_count"] == len(units)
    assert manifest["source_truncated_documents"] == []

    batches = batch_units(units)
    flattened = [unit["unit_id"] for batch in batches for unit in batch]
    assert flattened == [unit["unit_id"] for unit in units]
    assert len(flattened) == len(set(flattened))


def test_coverage_requires_every_unit_and_rejects_source_truncation():
    evidence = [Evidence(
        "official_ir:t1", "Transcript", "https://example.test/t1", "A" * 20000, [],
        {"document_kind": "transcript", "source": "official_ir", "raw_chars": 20000, "truncated": False},
    )]
    units, manifest = unitize_evidence(evidence)
    full = coverage_result(manifest, [unit["unit_id"] for unit in units])
    assert full["complete"] is True
    partial = coverage_result(manifest, [unit["unit_id"] for unit in units[:-1]])
    assert partial["complete"] is False
    assert partial["coverage_ratio"] < 1.0

    truncated_evidence = [Evidence(
        "official_ir:t2", "Transcript", "https://example.test/t2", "B" * 1000, [],
        {"document_kind": "transcript", "source": "official_ir", "raw_chars": 5000, "truncated": True},
    )]
    truncated_units, truncated_manifest = unitize_evidence(truncated_evidence)
    truncated = coverage_result(truncated_manifest, [unit["unit_id"] for unit in truncated_units])
    assert truncated["complete"] is False
    assert truncated["source_truncated_documents"] == ["official_ir:t2"]


def test_packet_selects_after_full_extraction_and_preserves_material_qa_and_guidance():
    cards = []
    for index in range(120):
        cards.append({
            "card_id": f"c{index}", "unit_id": f"u{index}", "document_key": "d1", "source": "official_ir",
            "card_type": "fact", "topic": "other", "statement": f"low value detail {index}",
            "materiality_candidate": 1, "quote": f"quote {index}",
        })
    cards.extend([
        {"card_id": "g1", "unit_id": "ug", "document_key": "d1", "source": "official_ir",
         "card_type": "guidance", "topic": "guidance", "statement": "full-year guidance raised",
         "materiality_candidate": 5, "quote": "guidance raised"},
        {"card_id": "q1", "unit_id": "uq", "document_key": "d2", "source": "official_ir",
         "card_type": "qa", "topic": "qa_management", "question_summary": "Is demand durable?",
         "answer_summary": "Management gave a partial answer", "answer_quality": "partial",
         "materiality_candidate": 4, "quote": "demand durable"},
    ])
    coverage = {"expected_unit_count": 122, "processed_unit_count": 122, "coverage_ratio": 1.0,
                "complete": True, "transcript_unit_count": 20, "qa_unit_count": 10,
                "missing_unit_ids": [], "source_truncated_documents": []}
    packet = build_research_packet(cards, coverage)
    assert packet["selection"]["raw_card_count"] == 122
    assert packet["selection"]["selected_card_count"] <= 90
    selected_ids = {card.get("card_id") for card in packet["cards"]}
    assert "g1" in selected_ids
    assert "q1" in selected_ids
    assert packet["selection"]["omitted_low_materiality_card_count"] > 0


def test_v5_models_are_routed_by_stage_and_have_smaller_extractor_budget(monkeypatch):
    client = GeminiV2Client(api_key="test")
    assert client._stage_models("v5_extract")[0] == "gemini-3.5-flash-lite"
    assert client._stage_models("v5_analyst")[0] == "gemini-3.6-flash"
    assert client._stage_models("v5_auditor")[0] == "gemini-3.5-flash"
    assert _generation_config("gemini-3.5-flash-lite", "v5_extract")["maxOutputTokens"] == 3200
    assert _generation_config("gemini-3.6-flash", "v5_analyst")["maxOutputTokens"] == 6000


def test_analysis_builder_routes_to_v5(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    assert isinstance(build_analysis_client("gemini"), ProductionInvestorV5Client)


def test_alpha_vantage_transcript_is_not_precompressed_to_46k():
    raw = "Speaker: complete transcript content.\n\n" * 2500
    disclosure = Disclosure(
        source="alpha_vantage_transcript", source_id="x", ticker="TEST", title="TEST Earnings Call Transcript",
        published_at="2026-09-01T00:00:00-04:00", url="https://example.test/transcript",
        document_url="https://example.test/transcript", document_kind="transcript",
        metadata={"transcript_text": raw},
    )
    evidence = EvidenceExtractor().fetch(disclosure)
    assert len(raw) > 46000
    assert evidence.text == raw
    assert evidence.metadata["selection_mode"] == "full_transcript"
    assert evidence.metadata["truncated"] is False


class _AuditSpy(ProductionInvestorV5Client):
    def __init__(self):
        super().__init__(api_key="test")
        self.prompts = []

    def _json(self, prompt, stage, tools=None):
        self.prompts.append((stage, prompt))
        if stage == "v5_auditor":
            return {"overall_score": 95, "unsupported_claims": [], "numerical_errors": [],
                    "missing_material_points": [], "causal_reasoning_errors": [], "qa_interpretation_errors": [],
                    "accounting_guidance_errors": [], "critical_issues": [], "pass": True,
                    "corrected_telegram_draft": "ok"}
        raise AssertionError(stage)


def test_auditor_uses_research_packet_not_raw_long_transcript():
    client = _AuditSpy()
    event = EarningsEvent("TEST_FY2027_Q2", "TEST", 2027, "Q2", "2026-09-01T00:00:00-04:00")
    packet = {"coverage": {"complete": True, "coverage_ratio": 1.0}, "cards": [{"statement": "material fact"}]}
    facts = {"research_packet": packet, "quote_validation_issues": []}
    raw_marker = "RAW_TRANSCRIPT_SHOULD_NOT_ENTER_AUDIT" * 10000
    evidence = [Evidence("d1", "Transcript", "u", raw_marker)]
    client.audit(event, facts, {"telegram_draft": "analysis"}, evidence)
    prompt = client.prompts[-1][1]
    assert "material fact" in prompt
    assert "RAW_TRANSCRIPT_SHOULD_NOT_ENTER_AUDIT" not in prompt
