# AI Evidence Log — Urban Company 90-Day Repeat Discovery Engine

> **Live Interactive Discovery Engine:** https://uc-repeat-discovery.streamlit.app/  
> **North-Star Business Metric:** Share of customers who complete another valid booking within 90 days of a completed core-category anchor booking (excluding InstaHelp, redos, warranty claims, and refunded bookings).  
> **Scope Constraints:** Customer-side product solutions only — no discounts, coupons, memberships, pricing changes, or partner payout/management changes. Public, permitted sources only; zero personal data or logged-in scraping.

---

## 1. Sources & Volumes Analysed

All customer feedback was collected from permitted public sources at **$0.00 paid spend** and PII-scrubbed before storage (removing emails, unspaced and spaced Indian phone numbers, and social/Reddit handles). Urban Company's 8 public quarterly filings were processed in a strictly separate context pipeline so management commentary never mixes into customer review counts.

| Source | Raw Pulled | Stage 1 Kept (After Keyword & InstaHelp Filter) | Stage 2 Clean & Deduped | Stage 3 Advancing (`yes` / `unsure`) | Stage 4 Tagged (Valid) | Final Relevant Core Posts | Date Coverage | Bias Mitigation Role |
|---|---:|---:|---:|---:|---:|---:|---|---|
| **Google Play Store** (`com.urbanclap.urbanclap`) | 30,000 | 7,193 | 7,190 | 4,418 | 4,418 | **4,289** | 2018–2026 | Balanced **6,000/star (1★–5★)** pull to counter angry-review bias (Bias #1); 937 InstaHelp excluded |
| **Apple App Store** (India RSS, `id1032480595`) | 500 | 136 | 136 | 64 | 64 | **63** | Oct–Nov 2026 | iOS metro cohort; 54 InstaHelp excluded |
| **Reddit** (11 Indian city & household subs) | 480 hits *(338 threads)* | 700 *(306 posts, 394 comments)* | 698 | 487 | 487 | **461** | 2021–2026 | Captures off-platform habits, DIY & local alternatives (counters silent non-returner Bias #2); 18 InstaHelp excluded |
| **YouTube Comments** (42 videos, 8 queries) | 1,789 | 673 | 642 | 141 | 139 *(2 self-promo ads excluded)* | **110** | 2021–2026 | Captures Hinglish & vernacular household discussions (counters Bias #2); 22 InstaHelp excluded |
| **Trustpilot** | 0 | 0 | 0 | 0 | 0 | **0** | — | Blocked by HTTP 403 anti-bot gate — stopped immediately without bypassing |
| **Total Customer Posts** | **32,769** | **8,702** | **8,666** | **5,110** | **5,108** | **4,923** | **2018–2026** | **1,173 InstaHelp excluded** (1,031 Stage 1 + 142 Stage 4); **0** failed tags |
| *Context Only: UC Public Filings* | *8 PDFs (243 pages)* | *401 passages* | *401* | *Separate* | *Separate* | *Context only* | *Q3 FY26–Q2 FY27* | *Official benchmarks (8.6M ATU, 2.6× intl. frequency gap, ₹963 Cr Q2 FY27 Core NTV, <10% Top-8 penetration)* |

### How the Workflow Accounts for Both Structural Biases
1. **Bias #1 — Angry customers over-represented in public reviews:**
   - Balanced 6,000/star pulling across 1★–5★ on Play Store.
   - Explicit `scope_controllability` tagging separates out-of-scope supply/partner failures and pricing complaints into **Table 2** on the Opportunity Ranking page so they never hijack customer-side product priorities.
   - Every source-dependent headline displays the **by-source split**, revealing that voluntary/pre-booking posture flips dramatically by channel (**Play Store 33.7% · App Store 27.0% vs. Reddit 83.7% · YouTube 82.1%**).
2. **Bias #2 — Silent non-returners leave zero trace:**
   - Collected non-UC home-service discussions on Indian city/household subreddits (`r/bangalore`, `r/mumbai`, `r/delhi`, `r/gurgaon`, `r/hyderabad`, `r/pune`, `r/TwoXIndia`, etc.) and YouTube comments to observe what households actually do outside the app (local technicians, maids, WhatsApp/MyGate referrals, DIY).
   - Added a live **"Exclude curated Reddit"** toggle on the dashboard so evaluators can verify every headline holds with and without Reddit.
   - Explicitly flagged sparse demographic proxies (`n < 15` = Low confidence; `52.9%` `segment_proxy == unclear`) as hypotheses to validate through **Part 3 primary user interviews**.

---

## 2. Pipeline Architecture & Quality Controls

1. **Stage 1 — Collect:** Python collectors with keyword matching, mandatory InstaHelp exclusion, Native hardware flagging (`99` posts), and PII scrubbing.
2. **Stage 2 — Deterministic Clean (`8,702 → 8,666`):** Plain-code exact + near-duplicate removal (`rapidfuzz >= 92%`), platform staff reply filter, language tagging (`en` / `hi` / `hinglish` / `other`), and context attachment (`36` dropped with logged reasons).
3. **Stage 3 — AI Relevance Filter (`8,666 → 5,110`):** `openai/gpt-oss-20b` (`1,350`) + `gemini-3.1-flash-lite` (`7,316`) using a tightened 3-way prompt (`yes` / `no` / `unsure`) + deterministic Hinglish/edge-case safety net (`403` rescues).
4. **Stage 4 — Structured Tagging (`5,110 → 5,108` after 2 Stage-8 ad removals):** Fixed `codebook.yaml` schema run across `gemini-3.1-flash-lite` (`4,636`), `openai/gpt-oss-120b` (`320`), and `qwen/qwen3.8-27b` (`152`).
   - **Free quote verification (`rapidfuzz`):** **98.7%** verified verbatim substrings (`94.7%` exact, `4.1%` repaired to closest verbatim span, `1.2%` scoring `<80` automatically hidden).
   - **Inter-model agreement check (`n = 30` paired items, `gpt-oss-120b` vs. `qwen3.8-27b`):** **76.1%** average core-field agreement (`is_relevant` 90%, `segment_proxy` 90%, `external_alternative` 83.3%, `booking_outcome` 80%, `emotion` 80%, `repeat_behavior` 76.7%, `repeat_barriers_or_drivers` 76.7%).
5. **Stage 6–7 — Export, Hybrid RAG & Live Dashboard:** Public-safe `data/export/` (`439.7 MB` peak RAM on clean clone), sub-theme decomposition of broad buckets, hybrid `fastembed` (`BAAI/bge-small-en-v1.5`) + codebook-tag reranking + source-diversity cap (`max 4/8` per source), and grounded Gemini synthesis with inline citations and mandatory bias check.

---

## 3. 20-Item Hand-Labelled Blind Golden Set (Human vs. AI Agreement & Failures)

- **Design:** 20 unseen relevant tagged posts (excluding rows already in `data/manual/corrections.csv`), stratified across all **4 live sources** (**7 Play Store, 6 Reddit, 4 YouTube, 3 App Store**), covering all **14 / 14** distinct barriers/drivers, all **5 / 5** need triggers, **4 Hinglish** posts, **10 hard cases** (repaired quotes, `unsure` filter, mixed scope, rule-fixed, fine-experience stops), and all **3** Stage 4 models.
- **Blind protocol:** Labelled from a clean sheet showing **only** `item_no`, full `post text`, and `context` — zero AI tags, zero quotes, zero source/rating hints.

### Field-by-Field Agreement (`20 / 20` Items Labelled)

*(4 items were judged `is_relevant = No` during blind review, so the remaining 6 fields are scored on the `16` items labelled across all columns.)*

| Field | Codebook Field | Raw Blind Agreement | Codebook-Normalized Agreement* | Agreed / Total | Metric |
|---|---|---:|---:|---:|---|
| **`is_relevant`** | `is_relevant` | **75.0%** | **75.0%** | 15 / 20 | Exact |
| **`anchor_category`** | `service_category` | **50.0%** | **75.0%** | 8 / 16 *(12 / 16 norm)* | Exact |
| **`stated_triggers (multi)`** | `need_trigger` | **56.2%** | **56.2%** | 9 / 16 | Overlap ≥ 1 |
| **`emotion`** | `emotion` | **31.2%** | **37.5%** | 5 / 16 *(6 / 16 norm)* | Exact |
| **`repeat_action`** | `repeat_behavior` | **31.2%** | **31.2%** | 5 / 16 | Exact |
| **`non_repeat_barrier`** | `repeat_barriers_or_drivers` | **25.0%** | **25.0%** | 4 / 16 | Overlap ≥ 1 |
| **`customer_type`** | `segment_proxy` | **18.8%** | **25.0%** | 3 / 16 *(4 / 16 norm)* | Exact |
| **Overall Mean** | **All 7 fields** | **41.1%** | **46.4%** | **67 raw disagreements** | — |

*\*Codebook-Normalized adjusts ONLY 5 free-text Apple Numbers category/segment strings (e.g. `AC_shifting` → `ac_appliance_repair`, `Spa_welness` → `salon_grooming`, `water_purifier_service` → `ac_appliance_repair`, `home_cleaning/painting` → `pest_control_painting`, `Woman` → `unclear`) and 1 shifted `delighted` cell on Item #15. Zero genuine human-vs-AI disagreements were altered.*

### Where the Engine Failed (Key Findings & Immediate Fixes)

1. **YouTube Hinglish Self-Promo Ads Leaked Through (`2 / 4` YouTube golden-set items — FIXED):**
   - Item #6 (competitor cleaning ad *"safai wale to hum bhii hai... charge bhi kamm karte hai"*) and Item #11 (local Navi Mumbai beautician promo *"beauty home service chahiye to msg kijiye only for ladies"*) were falsely tagged `is_relevant = yes`.
   - **Fix applied:** Audited all `112` relevant YouTube posts — confirmed these exact `2` were the only self-promo ads — and excluded them deterministically via `is_self_promo_ad()` in `pipeline/export.py`.
2. **Spaced Indian Phone-Number PII Leak (`Item #12` — FIXED):**
   - Item #12 (`r/kolhapur` local electrician/plumber contacts) contained spaced phone numbers (`+91 XXXXX XXXXX`) that bypassed the unspaced 10-digit scrubber.
   - **Fix applied:** Scanned the entire public export (`3` Reddit comments affected), expanded `_PHONE_RE` in `collect/utils.py`, added defensive `scrub_pii()` in `pipeline/export.py`, and verified **`0` phone leaks** remaining in `data/export/posts.parquet`.
3. **AI Over-Guessing `segment_proxy` & `repeat_behavior` Without Explicit Evidence (`Items #5, #8, #10, #18`):**
   - Example: On Item #18 (YouTube dining chair cleaning cancellation fee), AI guessed `working_couple_or_nuclear_family` with zero household clues; on Item #10 (painting), AI guessed `first_time_user`; on Item #8 (₹15k AC technician theft), AI invented `tier1_metro` and missed `trust_safety_or_home_privacy`.
   - **Methodological impact:** Confirmed that AI is reliable on coarse relevance (`75%`) and category (`75%` normalized) but over-eager on fine demographic/repeat-intent inference (`18.8–31.2%`) — directly reinforcing why all segment and behavioral root-cause hypotheses must be validated in **Part 3 primary user interviews**.
4. **Structural Rubric Differences Revealed:**
   - **Threaded Reddit comments (`Item #1`):** AI tagged the responder's praise while pulling city/trigger from the asker's `context`; a human reader naturally noticed the asker's hesitation too.
   - **Subreddit-as-city-proxy (`Items #1, #16, #17`):** AI inferred `tier1_metro` from `[r/mumbai]`, `[r/delhi]`, `[r/gurgaon]` in `context`, while blind human labelling required explicit mention in the post body.
   - **Hidden source in blind sheet (`Item #7`):** A Play Store Hinglish massage review didn't mention "Urban Company" by name (because it was posted on UC's Play Store page), causing the blind reviewer to mark `is_relevant = No`.

---

## 4. Concrete Places Where AI Output Was Rejected or Corrected (and Why)

| # | Stage | AI Output Rejected / Error Caught | Why It Mattered | Exact Fix & Verified Impact |
|---|---|---|---|---|
| **1** | **Public Filings Synthesis** (`gpt-oss-120b`) | Expanded `NTV` as *"New-to-vehicle (NTV)"* and reported Q4 FY26 p.13 *"30–33% monthly retention / 2.5 orders/month"* as if it were core Urban Company retention. | **InstaHelp is explicitly excluded** from the brief's core 90-day metric; mixing InstaHelp retention into Part 2 would invalidate the business baseline. | Verified against verbatim page-cited passages; corrected acronym to **Net Transaction Value** and relabelled Q4 p.13 strictly as **InstaHelp (excluded category)**. |
| **2** | **Stage 3 Relevance Filter** (`gpt-oss-20b`, Pilot v1 → v2.1) | Pilot v1 (`n = 100`) returned `0` `"unsure"` labels, passed `98%` of Play Store reviews as `"yes"` (including pure hardware defects), and wrongly dropped **3 Hinglish YouTube comparison comments** as `"no"`. | Passing 98% of angry Play Store reviews would flood Stage 4 with out-of-scope noise while losing authentic Hinglish household comparisons. | Tightened prompt with *"mentioning UC is not enough"* + 6 worked examples + deterministic Hinglish/warranty/scope safety net. Keep rate dropped `85% → 43%`, all 3 Hinglish YouTube items rescued, and **403 borderline posts** were safely upgraded to `unsure` in the full `8,666`-item run. |
| **3** | **Stage 4 Structured Tagger** (`gpt-oss-120b`, Pilot v1 → v2) | Pilot v1 (`n = 50`) failed `10/50` rows with invalid `severity = "u"` and blanked `20/40` valid posts to `"unclear"` across every field (`50–57.5%` unclear on core fields). | Blanking first-time Reddit inquiries and AI-chatbot UX complaints would erase half of our customer-side product signals. | Forbade blanking fields when `is_relevant = "no"`, restricted `severity` strictly to `1/2/3`, and added Reddit renter & chatbot worked examples. Validation jumped `40/50 → 50/50` (`0` fails) and core `% unclear` dropped **`52.5% → 8.0%`**. |
| **4** | **Inter-Model Check** (`gpt-oss-120b` vs. `qwen3.8-27b`) | `gpt-oss-120b` left `brief_questions` completely empty on `20/30` items (`20%` overlap with Qwen). | Page 3 (The Brief's 8 Questions) requires quotes strictly from posts tagged for each question. | Added mandatory `1–3` `brief_questions` rule before launching production shards → **`0` empty `brief_questions`** across all `5,110` tagged rows. |
| **5** | **Pre-Publish Suspicious-Tag Audit & Human Overrides** (`Stage 6`) | Auditing `booking_or_slot_scheduling_friction` (20 random posts) and `stopped_despite_fine_experience` revealed **`111` posts** tagged `stopped_despite_fine_experience` on failed/friction bookings, **`40` posts** tagged `customer_side_product` whose only barrier was pure partner failure, and overly broad buckets. | Inflating *"stopped despite fine experience"* with failed bookings would distort the central question of Part 4 (*"why customers don't return even when nothing went wrong"*). | Added deterministic post-tag audit rules (**151 tags corrected**), unpacked the two largest buckets into concrete sub-themes (revealing **AI chatbot dead-ends = 29.9%** of booking friction and **surprise add-ons/upsells = 56.4%** of scope friction), and applied **9 row-level human overrides** via `data/manual/corrections.csv`. |
| **6** | **Ask-the-Data RAG Retrieval** (`fastembed` + Gemini) | Pure dense search fell for literal keyword traps (e.g., interpreting *"fine booking"* as a **monetary cancellation fine**, matching *"Technician decides Pricing"* on Q2, and crowding `14/16` hits into Play Store). | Uncorrected RAG would cite pricing/penalty rants when asked why satisfied customers don't return. | Upgraded to clean 600-char docs + hybrid codebook-tag & barrier-intent reranking + `"fine booking"` query disambiguation + source-diversity cap (`max 4/8` per source). Verified `8/8` `completed_fine` results on smooth-booking queries across Play Store & Reddit. |
| **7** | **Stage 8 Blind Golden Set Audit** (`20` items) | Caught **3 spaced Indian phone numbers** in Reddit comments and **2 YouTube Hinglish self-promo ads** that slipped past automated filters. | Protects public-data privacy and removes competitor spam from customer counts. | Patched `_PHONE_RE` + export scrubber (`0` phone leaks verified) and excluded the `2` YouTube self-promo ads cleanly. |
