# AI Evidence Log Notes — Urban Company 90-Day Repeat Discovery Engine

> Running audit trail of every data-source choice, keyword/filter change, bug fix, bias adjustment, and AI quality check (feeds directly into the final **AI Evidence Log** deliverable).

---

## Stage 1: Data Collection & Keyword Filtering

### Decision 1 — Play Store Pilot Audit (5,000 reviews)
- **Date:** 2026-11-25
- **Setup:** `play_store` with `balanced_by_rating: true` (`1,000` per star rating 1★–5★), `min_words_after_filter: 6`, `exclude_instahelp: false` (flag-only in v1).
- **Before counts:** `5,000` pulled → `1,074` kept (`21.5%`), including `119` InstaHelp-flagged reviews.
  - Kept by rating: 1★ `382` | 2★ `304` | 3★ `221` | 4★ `122` | 5★ `45`.
- **What the 10-review random sample & solo-keyword audit revealed:**
  1. **Bias #1 confirmed quantitatively:** Even with equal pulling across star ratings (1,000 each), 1★–2★ reviews are much longer and pass keyword filters 4x more often than 4★–5★ reviews (most 5★ reviews are 1–3 words like *"Good service"*).
  2. **InstaHelp leakage:** `119` kept reviews were about InstaHelp (explicitly excluded by the brief), plus `38` solo `"maid"` matches were mostly InstaHelp/househelp reviews using spellings like *"house help"*, *"houshelp"*, and *"helper service"*.
  3. **Noisy / off-topic keywords found:**
     - `"plus"` matched arithmetic like *"plus ₹99"* / *"plus taxes"* rather than UC Plus membership.
     - `"direct"` matched *"no direct customer care"* rather than hiring a technician directly offline.
     - `"safe"` matched an off-topic **Uber** driver review posted on Urban Company's Play Store listing.
     - `"water purifier"` matched `28` solo Native RO M1/M2/M3 Pro hardware reviews.
     - `"maid"` pulled in InstaHelp/househelp complaints instead of outside-app permanent maid comparisons.
  4. **Coverage gaps against the brief's 8 questions:** Missing need-trigger words (Q2: summer, Diwali, monsoon, shifting, emergency), city/household/discovery words (Q7: Bangalore, Mumbai, Gurgaon, bachelor, elderly, referral), and post-booking clarity words (Q1/Q8: rate card, exclusion, invoice, after service).

### Decision 2 — Keyword Cleanup, InstaHelp Exclusion, Native Hardware Flagging & Play Store Scale-Up
- **Date:** 2026-11-25
- **What changed in `config.yaml` and `collect/`:**
  - **Removed noisy/off-topic standalone keywords:** `plus`, `direct`, `safe`, `maid`, `water purifier`, `outside`, `bill`, `missing`, `wish`.
  - **Replaced with precise phrases:** `uc plus`, `plus membership`, `book directly`, `call directly`, `hire directly`, `feel safe`, `unsafe`, `safety`, `local maid`, `regular maid`, `permanent maid`, `ro service`, `ro repair`, `water purifier service`.
  - **Added broad service anchors (Stage 3 AI filter removes passing mentions):** `urban company services`, `urban company service`, `uc services`, `uc service`, `home services`, `home service`.
  - **Expanded keywords to cover all 8 brief questions (Q1–Q8):** Added seasonal/trigger terms, outside-app alternatives (`mygate`, `nobroker`, `justdial`, `watchman`, `diy`), city & household proxies, and post-booking expectations (`rate card`, `exclusion`, `after service`, `inspection`).
  - **Actively excluded InstaHelp (`exclude_instahelp: true`):** Expanded `instahelp_terms` to `11` variants (adding `insta house help`, `house help`, `househelp`, `houshelp`, `helper service`, `instant help`, `15 min help`, `10 min help`, `quick help`) and dropped matching records at collection time.
  - **Kept & flagged Native hardware (`is_native_hardware: true`):** Deliberately kept Native RO / Native Smart Lock posts (instead of deleting) because multi-category users (e.g., pilot Review #8: Native RO + bathroom cleaning) experience trust spillover into core home services; flagged with `8` terms (`native ro`, `ro native`, `native water purifier`, `native purifier`, `native lock`, `smart lock`, `native m1/m2/m3`, `m1/m2/m3 pro`) so Stage 4 and the dashboard can separate hardware warranty noise from core services.
  - **Raised `play_store max_reviews`:** `5,000` → `30,000` (`6,000` per star rating 1★–5★) to build strong 3★, 4★, and 5★ depth.
- **Before vs. After (`play_store`):**
  - **Before (v1 pilot):** `5,000` pulled → `1,074` kept (`21.5%`), `0` InstaHelp excluded (`119` still inside kept), 4★=`122`, 5★=`45`, date range `2025-02-23` to `2026-11-23`.
  - **After (v2 full):** `30,000` pulled → `7,193` kept (`24.0%`), **`937` InstaHelp excluded**, `79` Native hardware flagged, 1★=`2,187` | 2★=`2,237` | 3★=`1,571` | 4★=`787` | 5★=`411`, date range `2018-06-21` to `2026-11-23`.
  - **5-item random sample check:** `5/5 relevant` (including city-comparison Mumbai vs. Gurgaon, 5★ slot friction, stain-exclusion clarity, AC upsell pressure, and pricing).

### Decision 3 — App Store Collector ID Fix & Run
- **Date:** 2026-11-25
- **What changed:** Initial `config.yaml` had App Store ID `1032484089`, which returned `0` reviews from Apple's iTunes RSS feed. Verified official India App Store ID for *Urban Company: Home Services* as `1032480595` and updated `config.yaml`.
- **Before vs. After (`app_store`):**
  - **Before:** `0` pulled → `0` kept.
  - **After:** `500` pulled (max 10 RSS pages) → `136` kept (`27.2%`), **`54` InstaHelp excluded**, `6` Native hardware flagged, 1★=`81` | 2★=`10` | 3★=`3` | 4★=`8` | 5★=`34`, date range `2026-10-13` to `2026-11-23`.
  - **5-item random sample check:** `4/5 relevant`, `1/5 generic` (short 5★ praise *"Always UC do a great job"*).

### Decision 4 — Trustpilot (Review Forum Slot) Blocked by Anti-Bot Gate
- **Date:** 2026-11-25
- **What happened:** Tested Trustpilot (our Urban Company review-forum equivalent of TradingQ&A) on `www.urbancompany.com`, `urbancompany.com`, and `urbanclap.com`. All returned HTTP `403` with a *"Verifying Connection"* anti-bot gate.
- **Decision:** Stopped direct scraping immediately (zero bypass attempts, per permitted-access rules). Counts: `0` pulled → `0` kept.
- **Mitigation:** Rely on Play Store (`7,193`), App Store (`136`), YouTube (`673`), and Stage 5 Reddit (`collect/reddit_apify.py` across Indian city/household subreddits) — plus optional Urban Company public shareholder letters / earnings-call transcripts if added manually.

### Decision 5 — YouTube Collector Run & Title-Match Noise Observation
- **Date:** 2026-11-25
- **Run counts (`youtube`):** `42` videos scanned across `8` queries → `1,789` comments pulled → `673` kept (`37.6%`), **`22` InstaHelp excluded**, `0` Native hardware flagged, date range `2021-11-24` to `2026-11-18`.
- **Quota usage:** ~`842` units out of `10,000` free daily limit (**8.42%** — well under 10%).
- **5-item random sample check:** `2/5 relevant` (authentic Kerala/Malayalam & Manglish discussions comparing professional deep cleaning vs. everyday sweeping/awareness), `3/5 off-topic` (a US "$15,000 deep cleaning" viral Short, a bridal makeup Short, and a comedy vlog comment).
- **Why this happened & next step:** Broad non-UC queries (`"home deep cleaning service India experience"`, `"salon at home vs parlour India"`) + title-level keyword matching pulled in viral/entertainment Shorts. Stage 3's AI relevance filter (`pipeline/filter.py`) is specifically designed to filter out these off-topic YouTube comments while preserving genuine Indian home-service discussions (note: we should inspect YouTube "yes"/"no" examples carefully in the Stage 3 pilot!).

### Decision 6 — Reddit Collection via Open-Source Agent Reach ($0.00)
- **Date:** 2026-11-25
- **Tool & Cost decision:** Instead of spending Apify credits ($0.00 spent on Apify), upgraded open-source `agent-reach` to `v1.6.1` and built `collect/reddit_agent_reach.py` using `agent-reach reddit search <query> --limit 20 --backend public-web` (Exa public-web index, **$0.00 cost**).
- **Method:** Ran `24` targeted queries covering 11 Indian city/household subreddits (`r/bangalore`, `r/mumbai`, `r/delhi`, `r/hyderabad`, `r/pune`, `r/chennai`, `r/kolkata`, `r/gurgaon`, `r/noida`, `r/india`, `r/TwoXIndia`) plus UC-vs-local, deep cleaning, AC service, salon-at-home, pest control, painting, and "stopped using Urban Company" discussions.
- **Cleaning & privacy applied at collection:** Filtered out stock/IPO subreddits & titles (`1`), pre-2021 posts (`3`), off-topic threads (`11`), and InstaHelp mentions (`18`); flagged Native hardware (`14`); split threads into posts + individual parsed top comments with parent post attached as `context`; stripped all `u/` usernames and profile URLs (`0` username leaks verified across all `700` rows).
- **Counts (`reddit`):** `480` raw hits (`338` unique threads) → **`700` kept** (`306` posts + `394` comments), date range `2021-01-01` to `2026-10-28`, cost **`$0.00`**.
- **5-item random sample check:** `3/5 relevant` (Hyderabad first-time review skepticism, Gurgaon Yes Madam vs. UC comparison, Bangalore pest-control switch to DIY Amazon Advion gel), `1/5 generic` (short waxing joke reply), `1/5 off-topic` (offline spa request in Vizag).
- **Bias & limitation note:** Public-web search returns top-ranked threads and top comments (not exhaustive comment trees), and keyword queries deliberately target UC/home-service discussions — so dashboard metrics will include an *"Exclude curated Reddit"* toggle (just like the SIP guide's Prompt 9) to verify every headline finding holds with and without Reddit.

---

## Stage 1 Running Totals (Ready for Stage 2 `pipeline/clean.py`)

| Source | Pulled | Kept | InstaHelp Excluded | Native Flagged | Date Range | Cost |
|---|---:|---:|---:|---:|---|---:|
| Play Store | 30,000 | 7,193 | 937 | 79 | 2018-06-21 to 2026-11-23 | $0.00 |
| App Store | 500 | 136 | 54 | 6 | 2026-10-13 to 2026-11-23 | $0.00 |
| Trustpilot | 0 *(403 bot gate)* | 0 | 0 | 0 | — | $0.00 |
| YouTube | 1,789 | 673 | 22 | 0 | 2021-11-24 to 2026-11-18 | $0.00 |
| Reddit (Agent Reach) | 480 hits (338 threads) | 700 *(306 posts, 394 comments)* | 18 | 14 | 2021-01-01 to 2026-10-28 | $0.00 |
| **Total Customer Posts** | **32,769** | **8,702** | **1,031** | **99** | **2018-06-21 to 2026-11-23** | **$0.00** |
| *Context Source: UC Filings* | *8 PDFs (Q3 FY26–Q2 FY27)* | *243 pages (~656K chars)* | — | — | *Oct 2025 – Oct 2026* | *$0.00* |


---

## Stage 2: Deterministic Cleaning & Deduplication (`pipeline/clean.py`)

### Decision 7 — Merge, Deduplicate, Platform-Staff Filter & Short-Text Rule (Plain Code Only, $0.00)
- **Date:** 2026-11-25
- **Rules applied (`pipeline/clean.py`):**
  1. **Merged sources:** `play_store.jsonl`, `app_store.jsonl`, `youtube.jsonl`, `reddit.jsonl` (`trustpilot.jsonl` had 0 rows due to 403 gate).
  2. **Pre-clean guardrails:** Removed empty texts, promotional spam, official Urban Company staff/brand replies (`1` caught), and any residual InstaHelp mentions (`0` residual — Stage 1 already excluded `1,031`).
  3. **Exact & near-duplicate removal:** Exact match on normalized lowercase alphanumeric text (`9` dropped) + `rapidfuzz.fuzz.ratio >= 92.0` within `(source, length_bucket, first_token)` buckets (`3` dropped).
  4. **Short-text rule:** Drop items with fewer than `6` meaningful words (excluding URLs/emojis/pure digits) **UNLESS** they contain a configured Urban Company keyword from `config.yaml` (`23` short YouTube items dropped; `13` short items containing UC keywords were preserved with `short_kept_by_keyword: true`).
  5. **Enrichment:** Added `year`, rule-based `language` (`en` / `hi` / `hinglish` / `other`), and normalized `context` (parent post for Reddit comments, video title for YouTube comments).
  6. **Auditability:** Saved kept items to `data/processed/clean.jsonl` (`8,666`) and every dropped item with its exact `drop_reason` to `data/processed/dropped.jsonl` (`36`).
- **Why only 36 items dropped in Stage 2:** Stage 1 collectors already reduced `32,769` pulled items down to `8,702` by applying `min_words >= 6`, `exclude_instahelp`, keyword matching, and ID deduplication up front.

### Stage 2 Funnel Per Source

| Source | Raw (Stage 1 Kept) | After Pre-Clean | After Dedupe | After Length Filter (`clean.jsonl`) | Dropped in Stage 2 |
|---|---:|---:|---:|---:|---:|
| `play_store` | 7,193 | 7,192 | 7,190 | **7,190** | 3 *(1 staff, 1 exact dup, 1 near dup)* |
| `app_store` | 136 | 136 | 136 | **136** | 0 |
| `reddit` | 700 | 700 | 698 | **698** | 2 *(2 exact dups)* |
| `youtube` | 673 | 673 | 665 | **642** | 31 *(6 exact dups, 2 near dups, 23 short)* |
| **Total** | **8,702** | **8,701** | **8,689** | **8,666** | **36** |

### Clean Dataset Breakdown (`8,666` items)
- **By Year:** `2018`: 123 · `2019`: 447 · `2020`: 602 · **`2021`: 670 · `2022`: 868 · `2023`: 849 · `2024`: 631 · `2025`: 870 · `2026`: 3,606** (`7,494` items are **2021+**; `4,476` are **2025–2026**).
- **By Language:** `en`: 8,213 (94.8%) · `hinglish`: 379 (4.4%) · `other`: 50 (0.6%) · `hi`: 24 (0.3%).
- **Dropped short-item spot check (5 random of 23):** All 5 were short YouTube comments (e.g., *"bura mat manana.... But... Kyu"*, *"70 wali bhi kafi achhi thi"*, *"9:26 to kya hua jaat h"*, bare price queries like *"Price for 3 bhk n gallery"*) — nothing useful lost.


---

## Dedicated Context Source: Public Shareholder Letters & Earnings Transcripts (`pipeline/extract_filings.py`)

### Decision 8 — Separate Filings Pipeline, Word-per-Line PDF Fix, and 2 AI Corrections
- **Date:** 2026-11-25
- **Why kept separate from `clean.jsonl` / `filter.py`:** Corporate filings represent management commentary (not customer voices); mixing 243 pages of corporate text into customer datasets would distort customer theme percentages and truncate long financial prose to 150 words.
- **Volume analyzed:** All `8` public PDFs covering `Q3 FY26`, `Q4 FY26`, `Q1 FY27`, and `Q2 FY27` (`4` Shareholder Letters + `4` Earnings Call Transcripts, `243` total pages, `~656K` chars).
- **Bug caught & fixed (v1 → v2):** In v1, 3 earnings transcripts (`Q4 FY26`, `Q1 FY27`, `Q2 FY27`) yielded `0` matched passages (`233` total) because `pypdf` extracted word-per-line `\n \n` formatting. Fixed `clean_pdf_text()` to collapse word-per-line layouts (`avg_words_per_line < 2.5`), recovering `+168` transcript passages (`233` → **`401` matched passages** across all 8 PDFs; every PDF now yields 35–68 passages).
- **Synthesis model & tokens:** `openai/gpt-oss-120b`, `18,851` tokens total across 4 quarterly passes, saved to `data/processed/filings_insights.json` and `docs/filings_summary.md`.
- **Two AI Errors Caught & Corrected by Manual Audit (AI Evidence Log):**
  1. **Acronym hallucination (`Q1 FY27`):** `gpt-oss-120b` expanded `NTV` as *"New-to-vehicle (NTV)"* instead of **Net Transaction Value (NTV)**. Corrected in both JSON and Markdown.
  2. **Scope misattribution (`Q4 FY26`, p.13):** `gpt-oss-120b` extracted *"Monthly user retention: 30–33%"* and *"2.5 orders/month"* without stating the subject was **InstaHelp** (Q17 in the Q4 FY26 Shareholder Letter). Because InstaHelp is explicitly excluded from our core 90-day metric, misreading this as core-service retention would have been a serious analytical error. Verified against `filings_passages.jsonl` and relabelled explicitly as **InstaHelp (excluded category)**.


---

## Stage 3: AI Relevance Filter Pilots (`pipeline/filter.py` — `openai/gpt-oss-20b`)

### Decision 9 — Pilot v1 Rejected & Corrected to Pilot v2 (100 Stratified Items, Seed 42)
- **Date:** 2026-11-25
- **Model:** `openai/gpt-oss-20b` (Groq free tier, `25` items/batch, text truncated to `150` words, context to `25` words).
- **Pilot v1 Result (REJECTED):**
  - `yes: 85` | `no: 15` | `unsure: 0` | `rescued_by_rule: 0` (`14,036` tokens; `140.4` tokens/item).
  - By source: `play_store` 44 yes / 1 no · `app_store` 15 yes / 0 no · `reddit` 19 yes / 1 no · `youtube` 7 yes / 13 no.
  - **Why rejected:** (1) `3` of `15` `no` items were clearly wrong (all YouTube Hinglish/comparison posts: *"M bhi is diwali pr try karugi urban.."*, *"Apka budget company badhiya hai urban se"*, local shops vs. organized hair/skin); (2) model answered `unsure` `0` times and passed `98%` of Play Store / `100%` of App Store reviews as `yes`, including pure partner no-shows, damaged appliances, and pure Native RO hardware defects.
- **Changes made for Pilot v2:**
  - Added explicit rule: *"Simply being about Urban Company is NOT enough for `yes`"* + 6 distinct worked examples (2 `yes`, 2 `no`, 2 `unsure`).
  - Expanded deterministic safety net to include Hinglish first-person markers (`m`, `mai`, `main`, `mein`, `hum`, `apka`, `karugi`) and comparison/trigger terms (`diwali`, `budget company`, `urban se`, `local shop`, `not available`).
- **Pilot v2 Result (Same 100 Items):**
  - `yes: 35` | `no: 57` | `unsure: 8` (`8` rescued by safety net) (`14,694` tokens; `146.9` tokens/item).
  - By source: `play_store` 13 yes / 4 unsure / 28 no · `app_store` 6 yes / 1 unsure / 8 no · `reddit` 13 yes / 0 unsure / 7 no · `youtube` 3 yes / 3 unsure / 14 no.
  - **Before vs. After improvement:** All 3 previously wrong YouTube `no` items were fixed (`1` became `yes`, `2` rescued to `unsure`), and `28` pure partner/Native-RO/price rants on Play Store + `8` on App Store were cleanly filtered out as `no` (cutting Stage 4 volume by ~50%).
  - **Remaining edge cases found in full 57-item `no` audit:** `52–53/57` (`~92%`) `no` labels were accurate; `3–4` borderline/useful customer-side items were still marked `no` (`[N21]` pest control lasting 1 year explaining non-repeat within 90 days; `[N24]` app unable to change booking address to Delhi; `[N45]` 90-day vs. 30-day warranty mismatch + local switch; `[N47]` silent scope reduction on repeat sofa cleaning). Paused before full run pending safety-net tweak & user go-ahead.
- **v2.1 Safety-Net Update (approved Option A):** Added `local service/tech/vendor/repair`, `warranty`, `change location/address`, `earlier it was`, `last year`, and `notification` to `_RESCUE_SIGNAL_RE` so the 4 audited edge cases (`[N21]`, `[N24]`, `[N45]`, `[N47]`) are safely upgraded to `unsure` instead of dropped, then launched full run (`2021+` first, `--auto-resume`) writing incrementally to `data/processed/filtered.jsonl`.
- **Provider Switch at `1,350 / 8,666` (`openai/gpt-oss-20b` → `gemini-3.1-flash-lite`):** Stopped Groq background task after `1,350` items completed cleanly and resumed `pipeline/filter.py` on `gemini-3.1-flash-lite` (same prompt, same 25-item batching, same v2.1 safety net; model ID recorded per item in `data/processed/filtered.jsonl` for full provenance).

### Stage 3 Full Run Complete (`8,666 / 8,666` items — 100% Finished)
- **Models used:** `openai/gpt-oss-20b` (`1,350` items) + `gemini-3.1-flash-lite` (`7,316` items; `998,285` tokens, `136.3` tokens/item).
- **Final Stage 3 Funnel (`data/processed/filtered.jsonl`):**

| Source | Total Clean | `yes` | `unsure` | `no` | Kept for Stage 4 (`yes + unsure`) | Keep % |
|---|---:|---:|---:|---:|---:|---:|
| `play_store` | 7,190 | 3,563 | 855 | 2,772 | **4,418** | 61.4% |
| `app_store` | 136 | 43 | 21 | 72 | **64** | 47.1% |
| `reddit` | 698 | 429 | 58 | 211 | **487** | 69.8% |
| `youtube` | 642 | 116 | 25 | 501 | **141** | 22.0% |
| **Total** | **8,666** | **4,151** | **959** | **3,556** | **5,110** | **59.0%** |

- **Safety net rescues (`rescued_by_rule = true`):** `403` items (plus `556` model-native `unsure` labels — proving the filter actively used `unsure` instead of silently dropping borderline cases).
- **YouTube noise removal confirmed:** `501` of `642` YouTube comments (`78%`) were cleanly removed as off-topic/US/entertainment Shorts while keeping `141` genuine Indian home-service discussions.


---

## Stage 4: Structured AI Tagging Pilot (`pipeline/tag.py` — `openai/gpt-oss-120b`)

### Decision 10 — Stage 4 Pilot v1 Rejected & Fixed in Pilot v2 (50 Stratified Items, Seed 42)
- **Date:** 2026-11-25
- **Model:** `openai/gpt-oss-120b` (Groq free tier, separate daily quota from 20b; `20` items/batch; fixed `codebook.yaml` short-code system prompt; auto-split + missing-item retry + `rapidfuzz` quote repair).
- **Pilot v1 Result (REJECTED):**
  - `40/50` valid, **`10/50` failed** (`10` retried — every single failure was `severity: invalid value 'u'`).
  - `20/40` valid rows were wrongly marked `is_relevant="no"` and completely blanked out with `"unclear"` on every field (including first-time Reddit renter questions and Play Store app/AI-chatbot UX complaints), inflating `% unclear` to `50.0%–57.5%` across core fields and yielding only `3` `customer_side_product` items (`351.0` tokens/item).
- **Changes made for Pilot v2:**
  - Clarified that inputs already passed Stage 3 filtering; explicitly marked first-time advice questions, moving/rental inquiries, app/chatbot UX complaints, and local comparisons as `r="y"`.
  - Forbade blanking fields to `"u"` and explicitly restricted `severity` to `"1" | "2" | "3"`.
  - Tightened `stopped_despite_fine_experience` (`out="ok"` only) vs. `stopped_after_bad_experience`, mapped maid/househelp to `instahelp_excluded` (not `salon_grooming`), and added 2 new worked examples (`[4]` Reddit first-time renter, `[5]` AI chatbot/Help UX).
- **Pilot v2 Result (Exact Same 50 Items):**
  - **`50/50` valid (`0` failed, `0` retried!)** — `15,678` tokens (**`313.6` tokens/item**).
  - `% unclear` dropped sharply across core fields: `service_category` `52.5% → 8.0%`, `booking_outcome` `50.0% → 8.0%`, `repeat_behavior` `57.5% → 12.0%`, `scope_controllability` `57.5% → 8.0%`, `repeat_barriers_or_drivers` `57.5% → 8.0%`, `unmet_need` & `emotion` `50.0% → 8.0%`.
  - `segment_proxy` (`80.0%` unclear) and `external_alternative` (`78.0%` unclear) remain high as expected because public reviews rarely volunteer household demographics or outside-app habits (validated later via Part 3 interviews).
  - Quote verification (`rapidfuzz`): **`47` exact (94%) · `1` repaired (2%) · `2` hidden < 80 (4%)**; `3` lexical grounding flags.
  - Scope split: `supply_partner_execution` `23` · **`customer_side_product` `22`** · `unclear` `4` · `pricing_or_discount_only` `1`.
  - Estimated runtime at Groq 200K TPD (`gpt-oss-120b` alone): `4,329` (`2021+`) items ≈ `6.8` days; all `5,110` items ≈ `8.0` days (faster with parallel second tagger in Prompt 6).

### Decision 11 — Inter-Model Agreement Check (`openai/gpt-oss-120b` vs. `qwen/qwen3.8-27b`) & Parallel Sharded Launch
- **Date:** 2026-11-25
- **Setup:** Ran `qwen/qwen3.8-27b` on the exact same `30` stratified items (12 Play Store, 9 Reddit, 5 App Store, 4 YouTube) already tagged by `openai/gpt-oss-120b` in Pilot v2. Saved raw comparison to `data/processed/inter_model_check.json`.
- **Qwen validation & quote quality:** `30/30` valid (`0` failed), `30/30` (**100%**) exact verbatim quotes, `9,351` tokens (`311.7` tokens/item).
- **Field-by-Field Agreement (30 Paired Items — `76.1%` Average Core-Field Agreement):**
  - `is_relevant`: **90.0%** (27/30)
  - `segment_proxy`: **90.0%** (27/30)
  - `external_alternative`: **83.3%** (25/30)
  - `booking_outcome`: **80.0%** (24/30)
  - `emotion`: **80.0%** (24/30)
  - `repeat_behavior`: **76.7%** (23/30)
  - `repeat_barriers_or_drivers` (≥1 overlap): **76.7%** (23/30; avg Jaccard `53.9%`)
  - `service_category`: **73.3%** (22/30)
  - `severity`: **73.3%** (22/30)
  - `need_trigger`: **66.7%** (20/30)
  - `scope_controllability`: **56.7%** (17/30 — disagreements concentrated on `mixed` vs. `customer_side_product`/`supply_partner_execution`; dashboard will show per-tagger splits and Stage 8 human review audits borderline scope tags)
  - `brief_questions`: **20.0%** (6/30) — **Root cause caught & fixed before full run:** `gpt-oss-120b` left `qs: []` empty on `20/30` items while `qwen/qwen3.8-27b` populated `24/30`. Added explicit instruction requiring `1–3` `qs` codes on every `r="y"` post before launching the full parallel shards.
- **Methodological Note on Segments (Required):** **Segment fields (`segment_proxy`: household type, city tier, tenure) are almost always unclear in public posts (~80% unclear in pilot; 90% inter-model agreement on `"unclear"`), so public-data segment comparisons use explicit proxies (e.g., subreddit/city mentions, explicit "bachelor"/"elderly"/"first time" mentions) and MUST be validated through Part 3 primary user interviews.**
- **Parallel Production Launch (Disjoint Shards, Priority Items First):**
  - Shard `0/2` (`2,555` items): `openai/gpt-oss-120b` → `data/processed/tagged_gpt120b.jsonl` (`--auto-resume`)
  - Shard `1/2` (`2,555` items): `qwen/qwen3.8-27b` → `data/processed/tagged_qwen27b.jsonl` (`--auto-resume`)
  - Every record stores `"tagged_by"`, priority brief items (`2021+` repeat/rebooking/alternative/scope/trigger signals, Reddit/App Store/YouTube, and 3★–5★ Play Store) run first, and shards are strictly disjoint (`index % 2 == 0` vs `1`) so no item is ever tagged twice.
- **Stage 4 Provider Switch at `472 / 5,110` (Groq → `gemini-3.1-flash-lite`):** Stopped both Groq parallel taggers after `openai/gpt-oss-120b` completed `320` clean `ok` items (`0` failed) and `qwen/qwen3.8-27b` completed `152` `ok` items + `88` `failed` items (due to Groq TPM/JSON batch splitting). Preserved all `472` clean `ok` rows in `data/processed/tagged.jsonl`, dropped the `88` failed Qwen rows so they are cleanly re-tagged, and resumed `pipeline/tag.py` on `gemini-3.1-flash-lite` (`4,638` pending items, priority items first, storing `"tagged_by"` per record).

### Stage 4 Full Tagging Complete (`5,110 / 5,110` — 100% Valid, `0` Failed)
- **File:** `data/processed/tagged.jsonl` (`5,110` unique IDs · `5,110` `ok` · `0` `failed` · `0` duplicates).
- **By tagger (`tagged_by`):** `gemini-3.1-flash-lite`: `4,638` · `openai/gpt-oss-120b`: `320` · `qwen/qwen3.8-27b`: `152`.
- **Relevance flag (`is_relevant`):** `yes`: `5,067` (99.2%) · `no`: `43` (0.8%).
- **Quote verification (`rapidfuzz`):** **`4,837` exact (94.7%) · `209` repaired (4.1%) · `64` hidden < 80 (1.3%)** (`98.7%` verified verbatim quotes).
- **Scope controllability:** `customer_side_product`: `2,828` (55.3%) · `supply_partner_execution`: `1,718` (33.6%) · `pricing_or_discount_only`: `419` (8.2%) · `mixed`: `77` (1.5%) · `unclear`: `68` (1.3%).
- **Booking outcome:** `service_failed_or_cancelled`: `1,622` · `completed_with_friction`: `1,357` · `never_booked_uc`: `1,209` · `completed_fine`: `815` · `unclear`: `107`.
- **Repeat behavior:** `stopped_after_bad_experience`: `2,054` · `active_repeater`: `1,051` · `considering_or_asking`: `1,026` · `switched_off_platform`: `492` · `unclear`: `223` · `stopped_despite_fine_experience`: `185` · `single_category_or_rare_user`: `79`.
- **Top customer-side barriers (`is_relevant = yes`):** `booking_or_slot_scheduling_friction` (`2,012`), `unclear_scope_or_surprise_addons` (`829`), `no_post_service_followup_or_care` (`437`), `trust_safety_or_home_privacy` (`397`), `cannot_rebook_same_trusted_pro` (`204`), `single_category_mental_model` (`86`). *(Out-of-scope supply/price: `service_quality_or_partner_failure` `1,674`, `perceived_high_price_vs_local` `866`.)*


---

## Stage 6: Dashboard Export & Hybrid Retrieval Audit (`pipeline/export.py`, `app/streamlit_app.py`)

### Decision 12 — Re-Export at `4,992` Tagged Items & Hybrid Retrieval Fix (Before/After Benchmark)
- **Date:** 2026-11-25
- **Full Export Volumes (`data/export/`):** `4,992` valid tagged items (`0` failed; `320` `gpt-oss-120b` + `152` `qwen3.8-27b` + `4,520` `gemini-3.1-flash-lite`) → **`4,808` relevant posts** (`141` residual InstaHelp excluded at Stage 4 on top of `1,031` at Stage 1 = `1,172` InstaHelp excluded total).
- **Embedding Model Evaluation (`gemini-embedding-2` vs. local `fastembed`):** Tested `gemini-embedding-2` (`batchEmbedContents`, 768-d) live — confirmed Google Free Tier enforces a `100` docs/min `embed_content_free_tier_requests` cap (~47+ min per full export and daily free-tier risk). Kept zero-rate-limit local `fastembed` (`BAAI/bge-small-en-v1.5`) and fixed the actual retrieval root causes instead.
- **Retrieval Bug Caught & Fixed (Q1–Q8 Benchmark):**
  - *Before (v1 dense search on 320-char boilerplate docs):* Pure cosine search fell for literal keyword traps (Q1 matched *"app showed booking complete"*; Q2 matched *"Technician decides Pricing"*; Q5 matched *"pls arrange whatsapp service"*) and crowded 14/16 top hits into Play Store alone.
  - *Fix Applied (v2):* (1) Removed repetitive `"Source: play_store..."` boilerplate and expanded embedded text to `600` chars + plain-English structured tags (`need_trigger`, `external_alternative`, `repeat_behavior`, `segment_proxy`, `brief_questions`); (2) added hybrid structured-tag reranking (`+0.10` matching `brief_questions`, `+0.06–0.08` non-unclear trigger/alternative/segment, `-0.04` pure supply-only when query isn't about failures) + source-diversity cap (`max 4 of 8` per source).
  - *After (v2 on `4,808` relevant posts):* Every question Q1–Q8 now mixes 2–4 sources in top-8 retrieval and pulls directly relevant evidence (Q1: `1,689` posts; Q2: `319`; Q3: `420`; Q4: `2,750`; Q5: `948`; Q6: `1,509`; Q7: `626`; Q8: `3,295`).


### Decision 13 — Pre-Publish Quality Audit, Suspicious-Tag Rule Fixes, Sub-Themes & Manual Corrections (Prompt 9)
- **Date:** 2026-11-25
- **Full Corpus Exported (`5,110 / 5,110` tagged — 100% of Stage 3 advancing items):** `4,925` relevant posts (`142` residual InstaHelp excluded at Stage 4 on top of `1,031` at Stage 1 = `1,173` total InstaHelp excluded; `43` tagged `is_relevant == "no"`).
- **Suspicious-Tag 20-Post & Schema Audit (`pipeline/export.py` deterministic fixes):**
  1. **`stopped_despite_fine_experience` on failed/friction bookings (`111` rows mis-tagged across all tagged items):** Deterministic rule in `apply_audit_rule_fixes()` reclassifies any row with `repeat_behavior == "stopped_despite_fine_experience"` AND `booking_outcome in {"service_failed_or_cancelled", "completed_with_friction"}` → `stopped_after_bad_experience`.
  2. **`customer_side_product` on pure supply-only barriers (`40` posts mis-tagged):** Deterministic rule reclassifies `scope_controllability == "customer_side_product"` when `repeat_barriers_or_drivers == ["service_quality_or_partner_failure"]` → `supply_partner_execution`.
  3. **20-post random audit of `booking_or_slot_scheduling_friction` (`1,919` posts):** Confirmed it bundled 6 distinct issues (AI chatbot dead-ends, involuntary rescheduling/cancel fees, app UX/cart minimums, slot unavailability/unserved areas, general scheduling/inquiry, and partner no-shows). Added deterministic `booking_subtheme` and `scope_subtheme` decomposition in `pipeline/export.py` and surfaced both on Page 2 (Opportunity Ranking).
- **Platform Staff Reply Verification:** Stage 2 (`pipeline/clean.py`) excluded `1` official staff reply. Audited all `4,925` relevant posts — the `6` regex hits were all customer letters starting with *"Dear Urban Company Team..."* (0 false-positive drops). Added defensive brand-side `is_platform_staff_reply()` check in `pipeline/export.py` (`0` additional staff replies found).
- **Human Corrections (`data/manual/corrections.csv`):** Created `data/manual/corrections.csv` with `9` verified field-level human overrides across `5` audited posts (e.g., partner lateness mis-tagged as `customer_side_product`, Diwali trial intent mis-tagged as `unclear_scope_or_surprise_addons`, summer AC inquiry mis-tagged as `booking_or_slot_scheduling_friction`), loaded automatically by `pipeline/export.py` and displayed on Page 8 (Validation & Limitations).
- **Dashboard Consistency & Polish (`app/streamlit_app.py`):** Enforced one consistent `tagged` / `relevant` count across all 8 pages (including dynamic updates when `"Exclude curated Reddit"` is toggled), added explicit by-source splits directly inside every source-mix-dependent headline (Pages 1, 2, 3, 4, and 5), and collapsed all long expanders (`expanded=False`).

---

## Stage 8: 20-Item Blind Golden Set Agreement (`pipeline/golden_set.py`)

### Decision 14 — Blind Human vs. AI Agreement (20/20 items labelled · Raw: 41.1% · Codebook-Normalized: 46.4%)
- **Date:** 2026-11-25 22:07 UTC
- **Sample composition:** {'reddit': 6, 'play_store': 7, 'app_store': 3, 'youtube': 4} · Hinglish/Hindi: 4 · Hard cases: 10.
- **Field-by-field agreement (Raw vs. Codebook-Normalized for 5 Apple Numbers custom labels + 1 shifted cell):**
  - `is_relevant` (`is_relevant`): **75.0% raw** (15/20 exact) · **75.0% codebook-normalized** (15/20)
  - `anchor_category` (`service_category`): **50.0% raw** (8/16 exact) · **75.0% codebook-normalized** (12/16)
  - `repeat_action` (`repeat_behavior`): **31.2% raw** (5/16 exact) · **31.2% codebook-normalized** (5/16)
  - `non_repeat_barrier` (`repeat_barriers_or_drivers`): **25.0% raw** (4/16 overlap ≥1) · **25.0% codebook-normalized** (4/16)
  - `stated_triggers (multi)` (`need_trigger`): **56.2% raw** (9/16 overlap ≥1) · **56.2% codebook-normalized** (9/16)
  - `emotion` (`emotion`): **31.2% raw** (5/16 exact) · **37.5% codebook-normalized** (6/16)
  - `customer_type` (`segment_proxy`): **18.8% raw** (3/16 exact) · **25.0% codebook-normalized** (4/16)
- **Total raw disagreements logged:** `67` (full table in `data/processed/golden_set_results.json` and Page 8 Validation & Limitations).
- **Concrete pipeline & AI bugs caught by the blind golden set and fixed immediately:**
  1. **PII Phone-Number Scrubber Gap (Item #12):** Spaced Indian phone numbers (`+91 XXXXX XXXXX`) slipped past the unspaced 10-digit scrubber (`3` Reddit comments affected in export). Patched `_PHONE_RE` in `collect/utils.py` and added defensive `scrub_pii()` in `pipeline/export.py` (`0` phone numbers remaining in `data/export/posts.parquet`).
  2. **YouTube Hinglish Self-Promo Ads (Items #6 & #11):** Local cleaning/beauty self-promo comments (`msg kijiye`, `safai wale to hum bhii hai`) passed as `is_relevant=yes`. Added `is_self_promo_ad()` exclusion in `pipeline/export.py` (`2/112` YouTube posts cleanly excluded).
  3. **AI Over-Guessing `segment_proxy` / `repeat_behavior`:** Golden set proved AI occasionally infers `working_couple_or_nuclear_family` or `first_time_user` without explicit text evidence — reinforcing the methodological rule to validate all segment hypotheses via Part 3 primary user interviews.
