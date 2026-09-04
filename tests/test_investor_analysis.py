from types import SimpleNamespace

from us_earnings_monitor.analysis import build_analysis_client
from us_earnings_monitor.investor_analysis import InvestorFrameworkGeminiClient


class CaptureClient(InvestorFrameworkGeminiClient):
    def __init__(self):
        self.calls = []

    def _json(self, prompt, stage, tools=None):
        self.calls.append((stage, prompt))
        return {}


def test_analysis_provider_routes_to_investor_framework(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    client = build_analysis_client("gemini")
    assert isinstance(client, InvestorFrameworkGeminiClient)


def test_investor_prompt_contains_required_buy_side_framework():
    client = CaptureClient()
    event = SimpleNamespace(event_id="TEST_FY2027_Q2")
    client.analyze(event, {"collection_status": {}}, [])
    stage, prompt = client.calls[-1]
    assert stage == "analyst"
    for token in (
        "Change Detection",
        "Materiality Filter",
        "Customer Proof",
        "Causal Chain",
        "Evidence Strength",
        "Driver / Lever / Catalyst / Risk",
        "🔄 本季變化:",
        "🔗 因果鏈與單位經濟:",
        "⚖️ 反證與未知:",
    ):
        assert token in prompt


def test_extractor_collects_non_financial_quantitative_evidence():
    client = CaptureClient()
    event = SimpleNamespace(event_id="TEST_FY2027_Q2")
    client.extract_facts(event, [])
    stage, prompt = client.calls[-1]
    assert stage == "facts"
    assert "quantitative_evidence" in prompt
    assert "customer_cases" in prompt
    assert "change_signals" in prompt
    assert "cost savings" in prompt
