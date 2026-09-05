import inspect

from us_earnings_monitor.analysis import build_analysis_client
from us_earnings_monitor.evidence_architecture import extraction_groups, quote_validation_issues, sectionize
from us_earnings_monitor.investor_analysis_runtime import ProductionInvestorV3Client, _harden_audit_result
from us_earnings_monitor.investor_analysis_v3 import InvestorFrameworkV3Client
from us_earnings_monitor.investor_analysis_v5 import ProductionInvestorV5Client
from us_earnings_monitor.models import Evidence


def test_long_transcript_keeps_late_qa_coverage():
    prepared = "Prepared remarks\n" + ("revenue context and operating detail. " * 1600)
    qa = "Question-and-Answer Session\n" + ("Analyst: margin question. CFO: margin answer. " * 2200)
    late = "\n\nAnalyst: UNIQUE_LATE_QA about pricing. CEO: UNIQUE_LATE_ANSWER."
    evidence = [Evidence("doc:1", "Official Transcript", "https://example.com/t", prepared + "\n\n" + qa + late)]

    sections = sectionize(evidence)
    assert any("UNIQUE_LATE_QA" in section["text"] for section in sections)
    groups = extraction_groups(evidence)
    assert len(groups) <= 6
    selected = "\n".join(section["text"] for group in groups for section in group)
    assert "UNIQUE_LATE_QA" in selected


def test_quote_validation_detects_unbacked_quote():
    evidence = [Evidence("doc:1", "Release", "", "Revenue grew 20 percent and margin improved materially.")]
    facts = {"facts": [{"metric": "Revenue", "evidence": {"document_key": "doc:1", "quote": "This fabricated quote is definitely not present in the official source document."}}]}
    assert quote_validation_issues(facts, evidence)


def test_production_builder_uses_v5_while_v3_remains_available_for_retrieval(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    client = build_analysis_client("gemini")
    assert isinstance(client, ProductionInvestorV5Client)
    assert ProductionInvestorV3Client is not ProductionInvestorV5Client


def test_v3_prompt_has_two_axis_materiality_and_cross_context():
    source = inspect.getsource(InvestorFrameworkV3Client.analyze)
    # V3 remains as a compatibility/retrieval component while V5 is production analysis.
    assert "Materiality 1-5" in source
    assert "Evidence A-D" in source
    assert "Cross-Context" in source
    assert "Headline" in source
    assert "Value-Chain Shift" in source


def test_v3_semantic_errors_are_promoted_to_critical_gate():
    audit = {
        "overall_score": 96,
        "pass": True,
        "critical_issues": [],
        "materiality_score_errors": ["boilerplate incorrectly scored M5"],
        "cross_context_errors": [],
        "value_chain_errors": [],
        "causal_chain_errors": [],
        "evidence_grade_errors": [],
    }
    hardened = _harden_audit_result(audit)
    assert hardened["pass"] is False
    assert "deterministic_v3_gate:materiality_score_errors" in hardened["critical_issues"]
