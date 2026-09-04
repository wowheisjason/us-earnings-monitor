from us_earnings_monitor.investor_analysis_runtime import ProductionInvestorV3Client
from us_earnings_monitor.report_contract import stage_contract


def test_v4_runtime_imports_and_contract_is_attached():
    assert ProductionInvestorV3Client is not None
    assert "投資結論與邏輯" in stage_contract("analyst_v3")
