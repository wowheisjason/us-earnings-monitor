# 美股財報自動監控與分析

以 **SEC EDGAR + 公司官方 IR** 組成 event-level evidence package 的低 token 財報監控器。程式先偵測財報事件，再蒐集同公司、同 fiscal period 的官方文件，完成來源狀態、去重與 deterministic validation 後，才呼叫分析模型並推送一份繁體中文 Telegram 報告。

## 架構

```text
SEC EDGAR 偵測 earnings event
        ↓
Event grouping / dedup
        ↓
Company IR discovery
  ├─ IR index / quarterly-results page
  ├─ same-host earnings event page
  ├─ Press Release / Financial Tables / Performance Review
  └─ Transcript → Prepared remarks + Q&A
        ↓
Source Manifest + collection state
        ↓
Deterministic extraction / validation
  ├─ XBRL / HTML / PDF / XLSX
  ├─ guidance low / midpoint / high
  ├─ GAAP vs non-GAAP
  ├─ standard FCF vs Adjusted FCF
  └─ transcript availability state
        ↓
Analysis provider
  → facts → investor analysis → reject-oriented audit
        ↓
Publish gate → Telegram
```

- 追蹤名單在 `watchlist.yaml`。
- SEC 的 10-Q、10-K、20-F、40-F 與 earnings-related 8-K / EX-99 是主要法定來源。
- 公司 IR 是同一 earnings event 的必要補充來源，不再只視為可有可無的 presentation enrichment。
- 同公司、同 fiscal period 的 10-Q、8-K、Press Release、Financial Tables、Performance Review、Presentation、Transcript / Q&A 合併成一份事件報告。
- IR crawler 支援一般 `.pdf/.html/.xlsx`，也支援 Dell 等網站使用的 `/static-files/<UUID>` extensionless asset，並可跟進一層同網域 earnings event detail page。
- broad IR index 不會把某一季度 heading 套用到整頁；period-less 文件只有在明確 event detail page 或有日期/period 證據時才能掛入事件。

## Transcript / Q&A 完整性

Transcript 與 Q&A 使用獨立 collection state：

- `FOUND`
- `EXPECTED_NOT_YET_AVAILABLE`
- `NOT_FOUND_AFTER_RETRY`
- `CONFIRMED_NOT_PUBLISHED`
- `UNKNOWN`

財報剛發布但 Transcript 尚未上線時，不會直接寫成「官方未提供」。在預設 24 小時 collection window 內，publish gate 會等待後續排程重試；若 IR 抓取本身失敗或只有部分 URL 成功，也不會被誤記成完整檢查。

Transcript 抽取保留連續對話脈絡，不使用一般財務 keyword filter 切碎 Q&A。Q&A 會優先整理 demand、pricing、supply、margin、guidance、customer、inventory、competition、capex、risk 等投資訊號。

## Deterministic validation

能由程式驗證的事情優先不交給 LLM：

- event grouping / URL 去重
- guidance range 完整性與 midpoint 算術
- GAAP / non-GAAP 與 company-defined metric label
- standard FCF / Adjusted FCF taxonomy
- Adjusted metric reconciliation 是否缺失
- 未配置外部 consensus provider 時，不允許模型自行產生 market consensus
- Source Manifest / Official IR completeness / Transcript status

任何 critical completeness 或 evidence 問題會阻止正式推播，或進入 `needs_human_review`。

## 分析模型：free-first、可替換

分析層透過 `AnalysisClient` protocol 與資料蒐集、驗證、Telegram 完全解耦。

目前 unattended GitHub Actions 預設：

```text
ANALYSIS_PROVIDER=gemini
```

原因是專案已有 Gemini API，可在免費額度內運作。系統不新增任何必須付費的 OpenAI API 或第三方 consensus/data provider。

ChatGPT / GPT 可以在互動式研究與人工複核時直接使用，但 GitHub Actions 無法免費呼叫「這個 ChatGPT 對話」本身，因此 production automation 不會假裝把 ChatGPT Plus 當成 API。未來若有其他免費 API provider，只需新增 analysis adapter，不需要改 discovery / validation / state / Telegram。

## Analyst / Auditor 規則

1. **facts extraction**：只用 official evidence，缺值用 `null`，不得補數字。
2. **guidance**：官方有 range 時保存 `low / midpoint / high`，不得只把 midpoint 當完整 guidance。
3. **cash flow**：Operating Cash Flow、standard FCF、Adjusted FCF 分開；Adjusted metric 優先抽 reconciliation。
4. **consensus**：與 company guidance 完全分離；未配置外部 provider 時寫「未納入外部市場共識，因此不判定 Beat/Miss」。
5. **analyst**：客觀事實、management view、投資判讀與 unknown 分開。
6. **auditor**：採 reject-oriented 稽核；unsupported claim、numerical error、missing transcript、range/midpoint confusion、GAAP/non-GAAP/FCF confusion 任一 critical issue 均不得通過。

只有 audit `overall_score >= 90`、`pass=true`，且 unsupported claims、numerical errors、critical issues 與 deterministic validation issues 全部為空才會推播。

## 排程與 token 效率

GitHub Actions 在交易日 **America/New_York** 時區執行：07:15、09:15、16:15、17:30、20:00。前幾次執行主要負責 discovery / retry；20:00 ET 進入分析窗口。若 Transcript 尚在 collection window，正式分析會延後到後續 run。

沒有新文件、重複文件、非 earnings event，以及可由 Python 完成的驗證都不需要 LLM，因此主要 token 成本集中在真正的新財報事件與 Transcript/Q&A 分析。

## 安裝、測試與乾跑

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
$env:PYTHONPATH = "src"
python -m us_earnings_monitor --fixture fixtures/disclosures.json --dry-run --at 2026-08-04T20:00:00-04:00
pytest -q
```

`--dry-run` 不呼叫分析 provider、不發 Telegram、不寫 state。Pull Request 另有純 pytest workflow，不需要 Gemini / Telegram secrets，也不會碰 production state。

首次部署可用 workflow dispatch 的 `initialize_baseline` 將既有文件標記為已處理，避免歷史申報被當成新事件。

## GitHub 設定

在 **Settings → Secrets and variables → Actions**：

Secrets：
- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Variables：
- `SEC_USER_AGENT`，例如 `us-earnings-monitor/0.2 your-email@example.com`

GitHub Actions workflow permissions 需為 **Read and write permissions**，production workflow 才能提交 `data/state.json`。

## 官方來源與合規

- **SEC EDGAR**：公開 submissions / filing archive，不需付費 API。
- **Company IR**：只讀 watchlist allowlist 的 HTTPS 頁面、同網域 event detail page 與官方文件；不繞過登入、robots 或授權限制。
- **Market consensus**：目前未接付費 FactSet / LSEG / Bloomberg 等 provider，因此不做 beat/miss 判定。

這是研究輔助，並非投資建議。

## 專案結構

```text
src/us_earnings_monitor/
  sources/          # SEC / Official IR discovery
  grouping.py       # event-level grouping
  extract.py        # HTML / PDF / XLSX / XBRL / Transcript extraction
  quality.py        # source manifest / collection state / publish gate
  validation.py     # deterministic validation
  analysis.py       # provider abstraction
  gemini.py         # current free-tier automated LLM adapter
  telegram.py
fixtures/
tests/
data/state.json
.github/workflows/
watchlist.yaml
```
