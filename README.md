# 美股財報自動監控與分析

以官方 SEC EDGAR 資料為主、公司 IR 為輔的低 token 自動化監控器。程式先過濾、去重與合併同一財報事件；只有有新且重要的財報資料，才會呼叫 Gemini 並推送一份繁體中文 Telegram 報告。

## 架構

```text
SEC EDGAR submissions + filing attachments
  → ticker / Form / title filter → 同事件歸併 → 20:00 ET 聚合完成
  → XBRL / HTML / PDF 擷取相關 evidence
  → Gemini facts → 投資人分析 → 產業專家 audit → publish gate → Telegram
                       ↑
               公司官方 IR（僅 SEC 已建立事件後補件）
```

- 追蹤名單在 `watchlist.yaml`，目前 36 家，包含 `SPCX`、`CBRS`、`SKHY`。
- SEC 的 10-Q、10-K、20-F、40-F 為主錨點；8-K 的 2.02/7.01 與相關 EX-99 earnings release 才會保留。
- 同公司同一 `period_end` 歸併，例如 `SPCX_2026-06-30_Q2`。10-Q、8-K earnings release、presentation、Q&A、transcript 只生成一份事件報告。
- 公司 IR 不會主動掃描或啟動 AI；只有 SEC 已發現的近期事件才讀取 allowlist 的靜態頁面。這避免歷史文件、一般新聞與重複分析。
- Python 優先擷取 inline XBRL 結構化數字；HTML/PDF 僅擷取財報相關段落。Gemini evidence 總量上限為 48,000 字元。

## Gemini 與推播門檻

1. **facts extraction**：缺值填 `null`，禁止猜測。
2. **analyst**：專業投資人角度，將 Facts、Interpretation、Investment implications、Unknown 分開。
3. **auditor**：產業專家回看官方 evidence，檢查數字、無依據敘述、遺漏與誤導推論。

只有 `overall_score >= 90` 且 `unsupported_claims` 為空才推播；否則自動修訂一次，仍失敗則標示 `needs_human_review`。最終 Telegram 使用繁體中文，約 500 字，包含關鍵指標表格、財測、產業訊號、風險／未知及去重的官方來源連結。

## 排程與 token 效率

GitHub Actions 在交易日 **America/New_York** 時區執行：07:15、09:15、16:15、17:30 只收集；**20:00 ET** 才聚合並分析。沒有新、已去重，或不屬於財報的申報時不會呼叫 Gemini，因此日常檢查本身不消耗 Gemini token。GitHub 排程可能延遲數分鐘，20:00 後的最終 run 仍會分析。

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

乾跑不下載文件、不呼叫 Gemini、不寫 state、不發 Telegram。首次部署請用 workflow dispatch 勾選 `initialize_baseline` 一次，將現有文件視為已處理，避免把舊申報當成新財報；不要同時勾選 `dry_run`。

需要端到端驗證時，workflow dispatch 可填 `test_at`（America/New_York ISO 時間），以該日期的 SEC 申報跑一次；這會使用 Gemini 並推送 Telegram，僅限人工授權測試。

## GitHub 設定

在 **Settings → Secrets and variables → Actions** 新增三個 secrets：

- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

另在 **Variables** 新增非敏感變數 `SEC_USER_AGENT`，例如 `us-earnings-monitor/0.1 your-email@example.com`。SEC 要求自動化客戶明確識別身分；不需要 SEC API key。這個值同時用於 submissions 偵測與 filing 附件下載。將 Actions 的 workflow permissions 設成 **Read and write permissions**，讓它提交 `data/state.json` 作為可審查的去重狀態。

不要把任何 key 放進 `.env.example`、程式碼或 git commit。Telegram bot 必須已被加入目標群組／頻道並有發訊權限。

## 官方來源與合規

- **SEC EDGAR**：使用公開 `data.sec.gov` submissions API 和 filing archive，不需要付費 API。
- **公司 IR（輔助）**：只讀設定檔列出的 HTTPS 靜態頁面及同網域直接文件；不執行 JavaScript、不全站爬蟲、不繞過 robots、登入或授權限制。若公司 IR 使用官方 CDN，可將 CDN 網域明列在該公司的 `ir_additional_urls`；需要特定授權 API 時可新增 adapter，無須修改分析流程。

這是研究輔助，並非投資建議。

## 專案結構

```text
src/us_earnings_monitor/   # SEC、歸併、抽取、Gemini、Telegram
fixtures/                  # 離線流程測試資料
tests/                     # 單元與流程測試
data/state.json            # JSON 去重與事件狀態
.github/workflows/         # 排程 workflow
watchlist.yaml             # ticker / CIK / IR allowlist
```

