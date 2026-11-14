# Watchlist 12-hour research target and moomoo integration design

Status (2026-10-16): **design and staged code only; not a deployed moomoo/OpenD integration**. CI passing is not a live delivery SLA.

## Product contract
For **material new issuer information** affecting companies in a watchlist, deliver a source-grounded Traditional-Chinese research report to Telegram **within 12 hours of the issuer's actual publication**, when the source is available. This is an operational target, **not a proven guarantee**. Track publication time, first observed time, source-completeness time, audit-passed time, and Telegram accepted time as separate timestamps. The signed Telegram message ID proves provider acceptance, not user receipt/read.

Scope: earnings/revenue/guidance, CapEx/orders, products/pricing, technology qualifications, major contracts, management changes, regulation and other thesis-changing disclosures. Ordinary reposts, rating articles and non-material events are candidates for daily digest only.

## Current production limitations
- US repository: 37 watchlist entries, SEC-first financial-event filter, IR/transcript enrichment, Gemini, four-section Telegram after audit. PR #22 adds a 24/7 four-hour heartbeat and deeper industry/Q&A evidence rules.
- JP repository: 40 watchlist entries, EDINET, optional authorized TDnet feed, JPX **daily** archive and allowlisted IR pages. PR #11 removes 20:00-only research and looks back three preceding dates in one JPX page load.
- The existing filters mostly target **earnings events**, not every material company announcement; satisfying the full product contract requires a distinct material-news intake and event router.
- If TDNET_FEED_URL is absent, JPX daily archive does not provide timely intraday publication data. Do not claim <=12h coverage of all Japan issuer announcements.
- US/JP branch changes do not affect scheduled jobs until reviewed, merged, and source/AI/Telegram live E2E verified. GitHub Actions scheduled work may start late or be interrupted.

## Simplest system shape (single pipeline, not three separate LLMs)
1. **Watchlist master**: US/JP existing YAML remains a stable fallback. Optional read-only moomoo OpenD integration calls `get_user_security_group()` then `get_user_security(group_name)` to retrieve the user's chosen watchlist **group**; this is NOT a positions API. Normalize exchanges/aliases/IDs (e.g. US.NVDA, JP.8035). Record sync time and report any mismatch. Do not overwrite YAML on a failed or empty sync; avoid mistaking a temporary API outage for user deletion.
2. **Source collectors**: SEC/EDGAR + official US IR; authorized JP TDnet intraday + EDINET + JPX/official IR; moomoo `get_search_news()` optionally discovers headlines, timestamps, security links and URLs. Its metadata is not evidence for an earnings call Q&A; fetch the issuer's original release/transcript where permitted.
3. **Dedup and event identity**: issuer ID + canonical source URL/document hash + publication time + material update type; correlate simultaneous media copies; keep source provenance. A meaningful new official attachment or new transcript can trigger an amended v2 report only for new facts.
4. **Research routing**: L1 for ordinary event summary (often digest), L2 for thesis-changing industry/supply-chain news, L3 earnings guidance/Q&A. Reuse `research_intelligence.py` extract/analysis/audit rules from US/JP. Separate fact/management quote/question/inference; prefer official source, do not infer suppliers or pricing from demand alone.
5. **Publish gate**: if the full call transcript is late, send a clearly tagged official-source-first briefing if its supported material facts can pass audit; enrich later when Q&A becomes available. Never bypass unsupported claim or numeric guards to meet the clock.
6. **Delivery**: one Telegram event state machine (new→analyzing→audited→sending→accepted/unknown); `unknown` must reconcile receipt before retry, not blindly resend. Gracefully suppress duplicates. Keep confidential OpenD credentials in a separate read-only connector, never in repo/workflow/logs.
7. **SLA observability**: persist `source_published_at`, `first_detected_at`, `analysis_started_at`, `audit_passed_at`, `telegram_accepted_at`. If publication timestamp is fabricated by an archive (e.g. JPX midnight), mark it `estimated` and report detection latency separately; never use it as a measured real publication-to-delivery SLA.

## Poll and alert policy
- Public-source discovery every 4 hours 24/7 in phase 1; AI only on new material source data. Existing US dense after-hours polls remain.
- For a tight 12h SLA, prefer a more frequent official-source poll of ~15–60min once a reliable intraday channel is available, with a 4h fallback and a per-event 8h warning; avoid escalating purely because social/news outlets repost a story.
- If full publication-to-Telegram within 12h must be contractually guaranteed, use a reliable always-on service/queue plus monitoring. GitHub Actions alone does not provide an execution-time SLA.

## moomoo OpenD prerequisites and policy
- Current moomoo API v10.11 offers read-only `get_user_security(group_name)`, `get_user_security_group()`, and `get_search_news(keyword, max_count=10, news_sub_type=...)`; verify Japanese-account authorization and that a continuously running OpenD gateway is available before scheduling sync.
- Rate cap: 10 watchlist API calls / 30s and 10 news-search calls / 30s. Respect rate limits, dedup headlines and fetch canonical issuer disclosures.
- Do not request positions, order placement, or trading unlock to meet this user requirement. If moomoo gateway is unavailable, keep watchlist YAML and official sources operational; log that live moomoo list sync is absent.

## Acceptance tests before production
- Replay US financial report + added transcript, JP same-day earnings + following-day JPX archive, and a hypothetical material non-earnings release. Assert one accepted report per material event/version and evidence-based distinctions about supply vs cleanroom, cancellation penalties, negotiation vs realization, and qualification vs HVM.
- Assert existing V5 US and V3 JP evidence audits, provenance validation, CI, and Telegram dedup remain intact.
- Measure `source_published_at→accepted_at` for genuinely timestamped live events; verify <=12h target on real examples in both markets, including late Friday and after-hours. If provider/source unavailability blocks analysis, record an explicit exception instead of claiming SLA success.
- Verify the selected moomoo watchlist group after OpenD connection and reconciliation. No live integration or measured SLA is implied by this design note.
