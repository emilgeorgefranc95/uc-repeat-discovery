"""Urban Company 90-Day Repeat Discovery Engine — 8-Page Streamlit Dashboard.

Reads ONLY from data/export/ (built by pipeline/export.py).
Run locally:
    streamlit run app/streamlit_app.py
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
EXPORT_DIR = ROOT_DIR / "data/export"
EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

# Intent cues for hybrid tag-aware retrieval on Ask the Data
QUERY_INTENT_RULES: list[tuple[str, tuple[str, ...], str | None]] = [
    ("Q1_post_booking_expectations", ("after booking", "booking is complete", "once a booking", "post-booking", "post booking", "post service", "after service", "follow up", "followup", "warranty", "expect once"), None),
    ("Q2_how_when_need_decided", ("how do they decide", "when", "trigger", "seasonal", "diwali", "summer", "monsoon", "festival", "routine", "emergency", "shifting", "moving", "needed"), "need_trigger"),
    ("Q3_who_turned_to_next", ("turn to", "next time", "same kind of service", "different service", "another service", "same professional", "rebook"), None),
    ("Q4_why_rebooked_or_not", ("why they did or did not", "book again", "rebook", "stopped using", "return", "didn't book", "did not book"), None),
    ("Q5_outside_app_alternatives", ("outside the app", "local", "neighbour", "neighbor", "family", "whatsapp", "maid", "parlour", "parlor", "diy", "alternative", "competitor", "yes madam", "nobroker"), "external_alternative"),
    ("Q6_returners_vs_non_returners", ("separates", "returners", "who return", "regular", "loyal", "repeat customer", "come back"), None),
    ("Q7_segment_category_city_diff", ("differ by", "category", "city", "cities", "household", "bachelor", "elderly", "tier", "first found", "segment"), "segment_proxy"),
    ("Q8_consistent_unmet_needs", ("unmet need", "missing", "wish", "feature", "gap", "consistently"), None),
]

# Targeted barrier-level intent boosts so specific theme queries retrieve tagged matches rather than lexical near-misses
BARRIER_INTENT_RULES: list[tuple[tuple[str, ...], tuple[str, ...], float]] = [
    (("directly offline", "hire directly", "call directly", "book directly", "outside uc", "bypass", "cut out", "without app"), ("hired_uc_partner_directly_offline",), 0.16),
    (("one service", "single service", "single category", "only one", "other services", "different service", "cross-sell", "another category"), ("single_category_mental_model",), 0.16),
    (("same professional", "same person", "same pro", "same technician", "same beautician", "favourite", "favorite", "trusted pro"), ("cannot_rebook_same_trusted_pro",), 0.16),
    (("scope", "exclusion", "add-on", "addon", "extra charge", "hidden charge", "surprise", "rate card", "upsell"), ("unclear_scope_or_surprise_addons",), 0.14),
    (("post-booking", "post booking", "after service", "follow-up", "follow up", "warranty", "once a booking is complete"), ("no_post_service_followup_or_care", "unclear_scope_or_surprise_addons"), 0.12),
    (("whatsapp", "neighbour", "neighbor", "society", "mygate", "referral"), ("society_whatsapp_or_neighbour_referral",), 0.16),
    (("chatbot", "chat bot", "human support", "customer care", "customer support", "helpline"), ("no_post_service_followup_or_care", "booking_or_slot_scheduling_friction"), 0.12),
    (("rarely", "infrequent", "no trigger", "forget", "don't need often"), ("low_perceived_frequency_or_no_trigger",), 0.14),
]

# Colour-blind-safe Okabe-Ito palette
CB_BLUE = "#0072B2"
CB_ORANGE = "#E69F00"
CB_GREEN = "#009E73"
CB_PURPLE = "#CC79A7"
CB_SKY = "#56B4E9"
CB_VERMILION = "#D55E00"
CB_YELLOW = "#F0E442"
CB_GREY = "#6C757D"
CB_PALETTE = [CB_BLUE, CB_ORANGE, CB_GREEN, CB_PURPLE, CB_SKY, CB_VERMILION, CB_GREY, CB_YELLOW]

PAGES = [
    ("overview", "1. Overview & How It Works"),
    ("opportunities", "2. Opportunity Ranking"),
    ("questions", "3. The Brief's 8 Questions"),
    ("metric_tree", "4. Metric Tree"),
    ("segments", "5. Segments & Sources"),
    ("explorer", "6. Evidence Explorer"),
    ("ask", "7. Ask the Data"),
    ("validation", "8. Validation & Limitations"),
]

BARRIER_LABELS: dict[str, str] = {
    "single_category_mental_model": "Single-category mental model",
    "low_perceived_frequency_or_no_trigger": "Low perceived frequency / no repeat trigger",
    "prefers_local_provider_or_maid": "Prefers local technician, parlour, or maid",
    "hired_uc_partner_directly_offline": "Hired UC partner directly offline",
    "society_whatsapp_or_neighbour_referral": "Uses society WhatsApp / neighbour referral",
    "cannot_rebook_same_trusted_pro": "Cannot rebook same trusted professional",
    "unclear_scope_or_surprise_addons": "Unclear scope, exclusions, or surprise add-ons",
    "booking_or_slot_scheduling_friction": "Slot availability, rescheduling, or app UX friction",
    "no_post_service_followup_or_care": "No post-service follow-up / dead-end AI support",
    "trust_safety_or_home_privacy": "Trust, safety, or first-time quality uncertainty",
    "perceived_high_price_vs_local": "Perceived high price vs. local alternatives",
    "service_quality_or_partner_failure": "Service execution / partner reliability failure",
    "convenience_standardization_driver": "Convenience & fast booking (repeat driver)",
    "hygiene_and_professionalism_driver": "Hygiene & professionalism (repeat driver)",
    "unclear": "Unclear / unspecified",
}

BOOKING_SUBTHEME_LABELS: dict[str, str] = {
    "ai_chatbot_and_support_dead_ends": "AI chatbot & support dead-ends (Customer UX)",
    "involuntary_rescheduling_and_cancel_fees": "Involuntary rescheduling & cancel fees (Policy/UX)",
    "app_ux_cart_minimums_and_booking_limits": "App UX, cart minimums & booking limits (Product)",
    "slot_unavailability_and_unserved_areas": "Slot unavailability & unserved areas (Supply/Discovery)",
    "other_booking_friction": "General booking / pre-booking friction",
    "partner_cancellation_or_no_show_supply": "Partner no-show / late arrival (Supply — Out of Scope)",
}

SCOPE_SUBTHEME_LABELS: dict[str, str] = {
    "surprise_addons_hidden_fees_and_upselling": "Surprise add-ons, hidden fees & upselling",
    "unclear_inclusions_exclusions_and_warranty": "Unclear inclusions, exclusions & warranty",
    "other_scope_clarity": "General pre-booking scope clarity",
}

SOURCE_SHORT: dict[str, str] = {
    "play_store": "Play Store",
    "app_store": "App Store",
    "reddit": "Reddit",
    "youtube": "YouTube",
}


def format_source_rate_split(df_rel: pd.DataFrame, mask: pd.Series) -> str:
    """Format per-source % prevalence for headlines that depend on source mix."""
    parts = []
    for src in ("play_store", "app_store", "reddit", "youtube"):
        sub_src = df_rel[df_rel["source"] == src]
        if sub_src.empty:
            continue
        pct = round(float(mask.loc[sub_src.index].mean()) * 100, 1)
        parts.append(f"{SOURCE_SHORT[src]} {pct}%")
    return " · ".join(parts)


def format_source_share_split(df_sub: pd.DataFrame) -> str:
    """Format per-source composition (%) of a subset of posts."""
    if df_sub.empty:
        return "no posts"
    total = len(df_sub)
    vc = df_sub["source"].value_counts()
    parts = []
    for src in ("play_store", "reddit", "youtube", "app_store"):
        if src in vc and vc[src] > 0:
            pct = round(vc[src] / total * 100, 1)
            parts.append(f"{SOURCE_SHORT[src]} {pct}%")
    return " · ".join(parts)

BARRIER_META: dict[str, dict[str, Any]] = {
    "booking_or_slot_scheduling_friction": {"group": "in_scope", "branch": "4. Discovery, Trust & Rebooking Friction", "fixable": "Yes (Customer-Side)", "weight": 1.0},
    "unclear_scope_or_surprise_addons": {"group": "in_scope", "branch": "1. Post-Booking Care & Expectation Closure", "fixable": "Yes (Customer-Side)", "weight": 1.0},
    "no_post_service_followup_or_care": {"group": "in_scope", "branch": "1. Post-Booking Care & Expectation Closure", "fixable": "Yes (Customer-Side)", "weight": 1.0},
    "cannot_rebook_same_trusted_pro": {"group": "in_scope", "branch": "4. Discovery, Trust & Rebooking Friction", "fixable": "Yes (Customer-Side)", "weight": 1.0},
    "trust_safety_or_home_privacy": {"group": "in_scope", "branch": "4. Discovery, Trust & Rebooking Friction", "fixable": "Yes (Customer-Side)", "weight": 0.9},
    "prefers_local_provider_or_maid": {"group": "in_scope", "branch": "3. Platform Recall vs. Off-Platform Leakage", "fixable": "Partial (Value/Recall)", "weight": 0.8},
    "single_category_mental_model": {"group": "in_scope", "branch": "2. Need Recognition & Repeat Trigger", "fixable": "Yes (Customer-Side)", "weight": 1.0},
    "low_perceived_frequency_or_no_trigger": {"group": "in_scope", "branch": "2. Need Recognition & Repeat Trigger", "fixable": "Yes (Customer-Side)", "weight": 0.9},
    "society_whatsapp_or_neighbour_referral": {"group": "in_scope", "branch": "3. Platform Recall vs. Off-Platform Leakage", "fixable": "Partial (Trust/Recall)", "weight": 0.8},
    "hired_uc_partner_directly_offline": {"group": "in_scope", "branch": "3. Platform Recall vs. Off-Platform Leakage", "fixable": "Partial (Rebook Loop)", "weight": 0.7},
    "service_quality_or_partner_failure": {"group": "out_of_scope", "branch": "5. Delivery Quality & Price (Out of Scope)", "fixable": "No (Supply/Ops Constraint)", "weight": 0.0},
    "perceived_high_price_vs_local": {"group": "out_of_scope", "branch": "5. Delivery Quality & Price (Out of Scope)", "fixable": "No (Pricing Constraint)", "weight": 0.0},
    "convenience_standardization_driver": {"group": "out_of_scope", "branch": "Positive Repeat Driver", "fixable": "Strength to Amplify", "weight": 0.5},
    "hygiene_and_professionalism_driver": {"group": "out_of_scope", "branch": "Positive Repeat Driver", "fixable": "Strength to Amplify", "weight": 0.5},
}

BRIEF_QUESTIONS = [
    ("Q1_post_booking_expectations", "Q1: What do customers expect once a booking is complete?"),
    ("Q2_how_when_need_decided", "Q2: How do they decide that a home service is needed, and when?"),
    ("Q3_who_turned_to_next", "Q3: Who or what do they turn to when they need the same or a different service?"),
    ("Q4_why_rebooked_or_not", "Q4: How do customers describe why they did or did not book again?"),
    ("Q5_outside_app_alternatives", "Q5: What do they do outside the app (local providers, neighbours, family, WhatsApp)?"),
    ("Q6_returners_vs_non_returners", "Q6: What separates customers who return from customers who do not?"),
    ("Q7_segment_category_city_diff", "Q7: How do patterns differ by service category, city, household type & discovery?"),
    ("Q8_consistent_unmet_needs", "Q8: What unmet needs emerge consistently across conversations?"),
]


st.set_page_config(
    page_title="Urban Company 90-Day Repeat Discovery Engine",
    page_icon="🏠",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
      .banner-bar {
        background: #F4F7FA;
        color: #0F172A !important;
        border-left: 5px solid #0072B2;
        padding: 10px 14px;
        border-radius: 6px;
        margin-bottom: 14px;
        font-size: 0.92rem;
      }
      .banner-bar b, .banner-bar span, .banner-bar code {
        color: #0F172A;
      }
      .banner-bar code {
        background: #E2E8F0;
        padding: 1px 4px;
        border-radius: 3px;
      }
      .pipeline-box {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        align-items: center;
        margin: 10px 0 18px 0;
      }
      .pipe-step {
        background: #F8FAFC;
        border: 1px solid #CBD5E1;
        border-top: 4px solid #0072B2;
        border-radius: 8px;
        padding: 10px 12px;
        min-width: 145px;
        flex: 1;
      }
      .pipe-step h4 { margin: 0 0 4px 0; font-size: 0.9rem; color: #0F172A; }
      .pipe-step p { margin: 0; font-size: 0.8rem; color: #334155; }
      .pipe-arrow { font-size: 1.2rem; font-weight: bold; color: #64748B; }
      .quote-card {
        background: #F8FAFC;
        color: #0F172A !important;
        border-left: 3px solid #009E73;
        padding: 8px 12px;
        margin: 6px 0;
        border-radius: 4px;
        font-size: 0.88rem;
      }
      .low-conf { color: #D55E00; font-weight: 600; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def load_exports() -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    df = pd.read_parquet(EXPORT_DIR / "posts.parquet")
    summary = json.loads((EXPORT_DIR / "summary_stats.json").read_text(encoding="utf-8"))
    filings = json.loads((EXPORT_DIR / "filings_export.json").read_text(encoding="utf-8"))
    # Ensure list columns are Python lists (parquet loads numpy arrays)
    for col in ("repeat_barriers_or_drivers", "brief_questions", "grounding_flags"):
        if col in df.columns:
            df[col] = df[col].apply(lambda x: list(x) if isinstance(x, (list, np.ndarray)) else [])
    return df, summary, filings


@st.cache_resource(show_spinner=False)
def load_retrieval_assets() -> tuple[np.ndarray, list[str], Any]:
    from fastembed import TextEmbedding

    arr = np.load(EXPORT_DIR / "embeddings.npz")["embeddings"].astype(np.float32)
    ids = json.loads((EXPORT_DIR / "embed_ids.json").read_text(encoding="utf-8"))
    model = TextEmbedding(model_name=EMBED_MODEL_NAME)
    return arr, ids, model


def hybrid_retrieve_posts(
    query: str,
    df_rel: pd.DataFrame,
    vecs: np.ndarray,
    embed_ids: list[str],
    emb_model: Any,
    k: int = 8,
    max_per_source: int = 4,
) -> list[tuple[float, str, pd.Series]]:
    """Hybrid retrieval: fastembed cosine + structured codebook tag boost + source diversity."""
    q_low = query.lower()
    matched_qs: set[str] = set()
    matched_fields: set[str] = set()
    for qcode, cues, struct_field in QUERY_INTENT_RULES:
        if any(c in q_low for c in cues):
            matched_qs.add(qcode)
            if struct_field:
                matched_fields.add(struct_field)

    asks_about_failures = any(w in q_low for w in ("cancel", "rude", "damage", "late", "bad experience", "refund", "scam"))
    asks_about_price = any(w in q_low for w in ("price", "pricing", "cost", "expensive", "cheap", "fee", "charge", "overpriced"))
    asks_about_fine = any(
        w in q_low
        for w in ("fine booking", "fine experience", "good experience", "nothing went wrong", "satisfied", "completed fine", "happy customer", "smooth booking", "satisfactory booking", "smooth, satisfactory")
    )

    # Disambiguate "fine booking" -> "smooth satisfactory completed booking" before embedding
    # so dense vectors don't match monetary "fines" or cancellation charges.
    embed_query = query
    if asks_about_fine:
        embed_query = (
            query.replace("fine booking", "smooth satisfactory completed booking with no issues")
            .replace("fine experience", "smooth satisfactory completed experience with no issues")
        )

    if len(vecs) > 0 and emb_model is not None:
        q_vec = np.asarray(list(emb_model.embed([embed_query])), dtype=np.float32)[0]
        q_vec = q_vec / max(1e-9, float(np.linalg.norm(q_vec)))
        base_scores = vecs @ q_vec
    else:
        tokens = [t for t in q_low.split() if len(t) >= 3]
        rel_map_txt = df_rel.set_index("id")["text_snippet"].str.lower().to_dict()
        base_scores = np.array(
            [sum(1 for t in tokens if t in rel_map_txt.get(rid, "")) / max(1, len(tokens)) for rid in embed_ids],
            dtype=np.float32,
        )

    rel_by_id = df_rel.set_index("id")
    allowed_ids = set(rel_by_id.index.tolist())

    scored: list[tuple[float, str, pd.Series]] = []
    for idx, rid in enumerate(embed_ids):
        if rid not in allowed_ids:
            continue
        row = rel_by_id.loc[rid]
        score = float(base_scores[idx])

        # Structured tag boost when post's codebook tags directly answer the query's dimension
        if matched_qs and any(q in row["brief_questions"] for q in matched_qs):
            score += 0.10
        if "need_trigger" in matched_fields and row["need_trigger"] not in {"unclear", "routine_maintenance"}:
            score += 0.06
        elif "need_trigger" in matched_fields and row["need_trigger"] == "routine_maintenance":
            score += 0.03
        if "external_alternative" in matched_fields and row["external_alternative"] not in {"unclear", "none_uses_uc"}:
            score += 0.08
        if "segment_proxy" in matched_fields and row["segment_proxy"] != "unclear":
            score += 0.06
        for cues, target_bars, boost in BARRIER_INTENT_RULES:
            if any(c in q_low for c in cues) and any(b in row["repeat_barriers_or_drivers"] for b in target_bars):
                score += boost
        if "Q6_returners_vs_non_returners" in matched_qs and row["repeat_behavior"] in {"active_repeater", "stopped_despite_fine_experience", "single_category_or_rare_user"}:
            score += 0.07
        if any(c in q_low for c in ("one service", "single service", "single category", "only one")) and row["repeat_behavior"] == "single_category_or_rare_user":
            score += 0.12
        if any(c in q_low for c in ("directly offline", "hire directly", "call directly", "bypass")) and row["external_alternative"] == "direct_uc_partner_offline":
            score += 0.12
        if row["quote_status"] in {"exact", "repaired"} and len(row["evidence_quote"]) >= 25:
            score += 0.015
        if not asks_about_failures and row["scope_controllability"] == "supply_partner_execution":
            score -= 0.04
        if asks_about_fine:
            if row["booking_outcome"] == "completed_fine":
                score += 0.14
            if row["repeat_behavior"] in {"stopped_despite_fine_experience", "single_category_or_rare_user", "switched_off_platform"}:
                score += 0.10
            if any(
                b in row["repeat_barriers_or_drivers"]
                for b in (
                    "cannot_rebook_same_trusted_pro",
                    "low_perceived_frequency_or_no_trigger",
                    "single_category_mental_model",
                    "prefers_local_provider_or_maid",
                    "hired_uc_partner_directly_offline",
                    "society_whatsapp_or_neighbour_referral",
                )
            ):
                score += 0.09
            if row["booking_outcome"] == "service_failed_or_cancelled":
                score -= 0.12
            if not asks_about_price and row["scope_controllability"] == "pricing_or_discount_only":
                score -= 0.08
            if "denied service" in row["text_snippet"].lower() or "cancellation fee" in row["text_snippet"].lower():
                score -= 0.10

        scored.append((score, rid, row))

    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return []

    # Enforce source diversity so Play Store never crowds out Reddit/YouTube stories
    top_score = scored[0][0]
    picked: list[tuple[float, str, pd.Series]] = []
    src_counts: Counter[str] = Counter()
    overflow: list[tuple[float, str, pd.Series]] = []

    for item in scored:
        sc, rid, row = item
        src = str(row["source"])
        if src_counts[src] < max_per_source:
            picked.append(item)
            src_counts[src] += 1
        elif sc >= top_score - 0.14:
            overflow.append(item)
        if len(picked) >= k:
            break

    while len(picked) < k and overflow:
        picked.append(overflow.pop(0))

    return picked[:k]


@st.cache_resource
def get_daily_chat_counter() -> dict[str, int]:
    """Process-wide daily query cap counter for Ask the Data."""
    return {}


def get_secret_or_env(key: str) -> str | None:
    try:
        if key in st.secrets:
            return str(st.secrets[key])
    except Exception:
        pass
    load_dotenv(ROOT_DIR / ".env")
    return os.getenv(key)


def clean_label(val: str) -> str:
    return BARRIER_LABELS.get(val, str(val).replace("_", " ").title())


def render_banner(summary: dict[str, Any], df_tagged: pd.DataFrame, df_rel: pd.DataFrame, exclude_reddit: bool) -> None:
    tag_n = len(df_tagged)
    rel_n = len(df_rel)
    note = " · <b>Curated Reddit excluded</b>" if exclude_reddit else ""
    st.markdown(
        f"""
        <div class="banner-bar">
          <b>Data as of {summary['data_as_of_date']}</b> &nbsp;|&nbsp;
          <b>{tag_n:,} tagged</b> &nbsp;|&nbsp;
          <b>{rel_n:,} relevant</b>{note}<br/>
          <span style="color:#475569; font-size:0.82rem;">
            <b>Consistent definitions:</b> <i>Tagged</i> = unique posts with valid Stage 4 codebook output (<code>tag_status == ok</code>).
            <i>Relevant</i> = tagged posts with <code>is_relevant == yes</code> excluding InstaHelp (<code>service_category != instahelp_excluded</code>).
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def pick_verified_quotes(df_sub: pd.DataFrame, k: int = 2) -> list[dict[str, Any]]:
    """Select up to k verified quotes (exact/repaired), preferring distinct sources and higher severity."""
    valid = df_sub[df_sub["quote_status"].isin(["exact", "repaired"]) & (df_sub["evidence_quote"].str.len() >= 20)].copy()
    if valid.empty:
        return []
    valid = valid.sort_values(["severity_num", "quote_score", "year"], ascending=[False, False, False])
    picked: list[dict[str, Any]] = []
    seen_quotes: set[str] = set()
    seen_sources: set[str] = set()

    # First pass: try diverse sources
    for row in valid.itertuples(index=False):
        q = row.evidence_quote.strip()
        if q.lower() in seen_quotes:
            continue
        if row.source not in seen_sources or len(picked) >= k:
            picked.append({"source": row.source, "year": row.year, "rating": row.rating, "quote": q, "status": row.quote_status})
            seen_quotes.add(q.lower())
            seen_sources.add(row.source)
        if len(picked) >= k:
            break

    # Second pass if fewer than k found
    if len(picked) < k:
        for row in valid.itertuples(index=False):
            q = row.evidence_quote.strip()
            if q.lower() in seen_quotes:
                continue
            picked.append({"source": row.source, "year": row.year, "rating": row.rating, "quote": q, "status": row.quote_status})
            seen_quotes.add(q.lower())
            if len(picked) >= k:
                break
    return picked


def fmt_quote_inline(q: dict[str, Any]) -> str:
    r_part = f" · {q['rating']}★" if pd.notna(q.get("rating")) else ""
    yr_part = f" · {int(q['year'])}" if pd.notna(q.get("year")) else ""
    return f"\"{q['quote']}\" ({q['source']}{r_part}{yr_part})"


def build_opportunity_table(df_rel: pd.DataFrame, group_name: str) -> pd.DataFrame:
    total_rel = max(1, len(df_rel))
    rows = []
    for code, meta in BARRIER_META.items():
        if meta["group"] != group_name:
            continue
        mask = df_rel["repeat_barriers_or_drivers"].apply(lambda lst: code in lst)
        sub = df_rel[mask]
        cnt = int(len(sub))
        if cnt == 0:
            continue
        pct = round(cnt / total_rel * 100, 1)
        avg_sev = round(float(sub["severity_num"].mean()), 2)
        score = round(pct * avg_sev * meta["weight"], 1)
        quotes = pick_verified_quotes(sub, k=2)
        rows.append(
            {
                "code": code,
                "Theme": clean_label(code),
                "Posts": cnt,
                "% of Relevant": pct,
                "Avg Severity (1–3)": avg_sev,
                "Metric-Tree Branch": meta["branch"],
                "Fixable Within Constraints": meta["fixable"],
                "Opportunity Score": score,
                "Quote 1 (Verified)": fmt_quote_inline(quotes[0]) if len(quotes) > 0 else "—",
                "Quote 2 (Verified)": fmt_quote_inline(quotes[1]) if len(quotes) > 1 else "—",
            }
        )
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["Opportunity Score", "Posts"], ascending=[False, False]).reset_index(drop=True)
    return out


# ==============================================================================
# PAGE RENDERERS
# ==============================================================================

def render_page_overview(
    df_tagged: pd.DataFrame,
    df_rel: pd.DataFrame,
    summary: dict[str, Any],
    filings: dict[str, Any],
    exclude_reddit: bool = False,
) -> None:
    counts = summary["counts"]
    src_rows_all = summary["sources_stage1"]
    active_sources = [r for r in src_rows_all if not (exclude_reddit and r["source"] == "reddit")]
    pulled_active = sum(r["pulled"] for r in active_sources if r["source"] != "uc_filings")
    stage1_active = sum(r["stage1_kept"] for r in active_sources if r["source"] != "uc_filings")
    clean_active = sum(r["clean"] for r in active_sources if r["source"] != "uc_filings")
    filt_active = len(df_tagged)  # 100% of filtered yes/unsure items are now tagged in Stage 4

    rel_by_src = df_rel["source"].value_counts().to_dict()
    src_count_parts = [
        f"{SOURCE_SHORT[s]} {rel_by_src[s]:,}"
        for s in ("play_store", "reddit", "youtube", "app_store")
        if rel_by_src.get(s, 0) > 0
    ]
    st.title(
        f"From {pulled_active:,} public posts to {len(df_rel):,} relevant signals ({' · '.join(src_count_parts)})"
    )
    st.caption("North-Star Metric: Share of customers completing another valid booking within 90 days of a core-category anchor booking (excluding InstaHelp, redos, warranty claims, and refunds).")

    # Pipeline visual diagram
    st.subheader("End-to-End Discovery Pipeline (Zero-Cost Public Stack)")
    st.markdown(
        f"""
        <div class="pipeline-box">
          <div class="pipe-step"><h4>1. Collect</h4><p>{'3 live sources (Reddit excluded)' if exclude_reddit else '4 live public sources'} + 8 UC filings PDFs ({pulled_active:,} raw; 1,031 InstaHelp excluded)</p></div>
          <div class="pipe-arrow">→</div>
          <div class="pipe-step"><h4>2. Clean</h4><p>Plain code dedupe (rapidfuzz), staff/spam filter, language & context ({clean_active:,} clean)</p></div>
          <div class="pipe-arrow">→</div>
          <div class="pipe-step"><h4>3. AI Filter</h4><p>gpt-oss-20b + gemini-3.1-flash-lite + Hinglish safety net ({filt_active:,} advancing)</p></div>
          <div class="pipe-arrow">→</div>
          <div class="pipe-step"><h4>4. Structured Tag</h4><p>Fixed codebook.yaml, parallel models, rapidfuzz quote repair ({len(df_tagged):,} tagged / {len(df_rel):,} relevant)</p></div>
          <div class="pipe-arrow">→</div>
          <div class="pipe-step"><h4>5. Export & Audit</h4><p>Public-safe data/export/, rule & human fixes, sub-themes, fastembed RAG</p></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2 = st.columns([1.15, 1])
    with c1:
        funnel_df = pd.DataFrame(
            [
                ("1. Pulled (Stage 1 Raw)", pulled_active),
                ("2. Kept After Keyword + InstaHelp Filter", stage1_active),
                ("3. Clean & Deduplicated (Stage 2)", clean_active),
                ("4. Relevant / Unsure (Stage 3 Filter)", filt_active),
                ("5. Structured Tagged (Stage 4 Valid)", len(df_tagged)),
                ("6. Relevant Core Posts (Excl. InstaHelp)", len(df_rel)),
            ],
            columns=["Stage", "Posts"],
        )
        fig = px.bar(
            funnel_df,
            x="Posts",
            y="Stage",
            orientation="h",
            text="Posts",
            color_discrete_sequence=[CB_BLUE],
            title=f"Filtering removes {round((1 - filt_active/max(1, pulled_active))*100, 1)}% noise while preserving {len(df_rel):,} relevant stories",
        )
        fig.update_traces(texttemplate="%{text:,}", textposition="outside", cliponaxis=False)
        fig.update_layout(yaxis=dict(autorange="reversed"), xaxis=dict(range=[0, max(1, pulled_active) * 1.22]), margin=dict(l=10, r=70, t=50, b=10), height=340)
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        yr_src = df_rel.dropna(subset=["year"]).groupby(["year", "source"]).size().reset_index(name="Posts")
        if not yr_src.empty:
            pct_25_26 = round(df_rel["year"].isin([2025, 2026]).mean() * 100, 1)
            n_src_live = df_rel["source"].nunique()
            fig_yr = px.bar(
                yr_src,
                x="year",
                y="Posts",
                color="source",
                color_discrete_sequence=CB_PALETTE,
                title=f"{pct_25_26}% of relevant posts are from 2025–2026 ({n_src_live} sources)",
            )
            fig_yr.update_layout(margin=dict(l=10, r=10, t=50, b=10), height=340, legend_title="Source")
            st.plotly_chart(fig_yr, use_container_width=True)

    st.subheader("Sources & Volumes")
    src_df = pd.DataFrame(active_sources)
    tagged_by_src = df_tagged["source"].value_counts().to_dict()
    src_df["tagged"] = src_df["source"].map(lambda s: tagged_by_src.get(s, "Context PDF" if s == "uc_filings" else 0))
    src_df["relevant"] = src_df["source"].map(lambda s: rel_by_src.get(s, "Context PDF" if s == "uc_filings" else 0))
    st.dataframe(
        src_df[["label", "pulled", "stage1_kept", "clean", "tagged", "relevant", "date_range", "notes"]].rename(
            columns={"label": "Source", "pulled": "Pulled", "stage1_kept": "Stage 1 Kept", "clean": "Clean", "tagged": "Tagged", "relevant": "Relevant", "date_range": "Date Coverage", "notes": "Bias / Method Notes"}
        ),
        use_container_width=True,
        hide_index=True,
    )

    m_col1, m_col2 = st.columns(2)
    with m_col1:
        st.subheader("Models Used Across Stages")
        model_rows = []
        for m, cnt in summary["models_used"]["stage3_filter"].items():
            model_rows.append({"Stage": "Stage 3: Relevance Filter", "Model": m, "Items": cnt})
        for m, cnt in df_tagged["tagged_by"].value_counts().items():
            model_rows.append({"Stage": "Stage 4: Structured Tagger", "Model": m, "Items": int(cnt)})
        model_rows.append({"Stage": "Filings Synthesis (Q3 FY26–Q2 FY27)", "Model": "openai/gpt-oss-120b", "Items": "8 PDFs (401 passages)"})
        model_rows.append({"Stage": "Stage 6: Semantic RAG Retrieval", "Model": EMBED_MODEL_NAME, "Items": f"{len(df_rel):,} embedded"})
        st.dataframe(pd.DataFrame(model_rows), use_container_width=True, hide_index=True)

    with m_col2:
        st.subheader("Public Filings Context (Separate From Customer Counts)")
        st.info(
            "**Verified from 8 UC Shareholder Letters & Transcripts (Q3 FY26–Q2 FY27):**\n"
            "- **Core India Services (Ex-InstaHelp):** ₹963 Cr NTV in Q2 FY27 (+26.3% YoY); ₹1,056 Cr in Q1 FY27 (+29% YoY).\n"
            "- **Annual Transacting Users:** ~8.6M in Q2 FY27 (+22.4% YoY); growth driven primarily by **higher transaction frequency** (AOV stable).\n"
            "- **Frequency Gap:** International users transact at **2.6x** India consumer services annual frequency (Q4 FY26 Letter, p.10).\n"
            "- **Seasonality & Tier-2:** Core peaks in **Q1 (summer)** & **Q3 (Diwali)**; Tier-2 growing **~37% YoY** while Top-8 penetration remains **<10%**."
        )


def render_page_opportunities(df_rel: pd.DataFrame) -> None:
    t_in = build_opportunity_table(df_rel, "in_scope")
    t_out = build_opportunity_table(df_rel, "out_of_scope")

    top_code = t_in.iloc[0]["code"] if not t_in.empty else ""
    top_theme = t_in.iloc[0]["Theme"] if not t_in.empty else "Customer-side friction"
    top_pct = t_in.iloc[0]["% of Relevant"] if not t_in.empty else 0.0
    top_mask = df_rel["repeat_barriers_or_drivers"].apply(lambda lst: top_code in lst) if top_code else pd.Series(False, index=df_rel.index)
    top_src_split = format_source_rate_split(df_rel, top_mask)

    st.title(f"'{top_theme}' leads fixable barriers ({top_pct}% overall — {top_src_split})")
    st.caption("Opportunity Score = (% of Relevant Posts) × (Average Severity 1–3) × (Consumer-Side Controllability Weight). Out-of-scope supply failures & pricing are strictly separated in Table 2.")

    if not t_in.empty:
        fig = px.bar(
            t_in.head(10),
            x="Opportunity Score",
            y="Theme",
            orientation="h",
            color="Fixable Within Constraints",
            text="Posts",
            color_discrete_map={"Yes (Customer-Side)": CB_BLUE, "Partial (Value/Recall)": CB_SKY, "Partial (Trust/Recall)": CB_SKY, "Partial (Rebook Loop)": CB_SKY},
            title="Ranked Consumer-Side Repeat Barriers Fixable Within Brief Constraints",
        )
        fig.update_traces(texttemplate="%{text:,} posts", textposition="outside", cliponaxis=False)
        fig.update_layout(yaxis=dict(autorange="reversed"), xaxis=dict(range=[0, max(1.0, float(t_in["Opportunity Score"].max())) * 1.24]), height=400, margin=dict(l=10, r=90, t=50, b=10))
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Table 1: Customer-Side Repeat Barriers & Drop-off Causes (In-Scope / Fixable)")
    if not t_in.empty:
        st.dataframe(
            t_in[["Theme", "Posts", "% of Relevant", "Avg Severity (1–3)", "Metric-Tree Branch", "Fixable Within Constraints", "Opportunity Score"]],
            use_container_width=True,
            hide_index=True,
        )
        with st.expander("View 2 Verified Verbatim Quotes Per In-Scope Opportunity", expanded=False):
            for row in t_in.itertuples(index=False):
                st.markdown(f"**{row.Theme}** (`{row.Posts}` posts · `{row._3}%` · Severity `{row._4}` · Score `{row._7}`)")
                st.markdown(f"<div class='quote-card'>1. {row._8}</div>", unsafe_allow_html=True)
                if row._9 != "—":
                    st.markdown(f"<div class='quote-card'>2. {row._9}</div>", unsafe_allow_html=True)

    st.divider()
    st.subheader("Decomposing Broad Customer-Side Buckets Into Concrete Sub-Themes")
    st.caption("Pre-publish audit broke the two largest broad buckets ('Slot/booking/app UX friction' and 'Unclear scope/surprise add-ons') into specific, actionable product sub-themes.")
    sc1, sc2 = st.columns(2)
    with sc1:
        if "booking_subtheme" in df_rel.columns:
            b_sub = df_rel[df_rel["booking_subtheme"] != "not_applicable"]
            if not b_sub.empty:
                b_vc = b_sub["booking_subtheme"].value_counts().reset_index()
                b_vc.columns = ["Code", "Posts"]
                b_vc["Sub-Theme"] = b_vc["Code"].map(lambda k: BOOKING_SUBTHEME_LABELS.get(k, clean_label(k)))
                b_vc["% of Bucket"] = (b_vc["Posts"] / max(1, len(b_sub)) * 100).round(1)
                top_bs = b_vc.iloc[0]
                fig_bs = px.bar(
                    b_vc,
                    x="Posts",
                    y="Sub-Theme",
                    orientation="h",
                    text=b_vc.apply(lambda r: f"{r['Posts']:,} ({r['% of Bucket']}%)", axis=1),
                    color_discrete_sequence=[CB_BLUE],
                    title=f"Booking/UX ({len(b_sub):,} posts): AI chatbot is #1 ({top_bs['% of Bucket']}%)",
                )
                fig_bs.update_traces(textposition="outside", cliponaxis=False)
                fig_bs.update_layout(yaxis=dict(autorange="reversed"), xaxis=dict(range=[0, max(1, int(b_vc["Posts"].max())) * 1.38]), height=340, margin=dict(l=10, r=100, t=50, b=10))
                st.plotly_chart(fig_bs, use_container_width=True)

    with sc2:
        if "scope_subtheme" in df_rel.columns:
            u_sub = df_rel[df_rel["scope_subtheme"] != "not_applicable"]
            if not u_sub.empty:
                u_vc = u_sub["scope_subtheme"].value_counts().reset_index()
                u_vc.columns = ["Code", "Posts"]
                u_vc["Sub-Theme"] = u_vc["Code"].map(lambda k: SCOPE_SUBTHEME_LABELS.get(k, clean_label(k)))
                u_vc["% of Bucket"] = (u_vc["Posts"] / max(1, len(u_sub)) * 100).round(1)
                top_us = u_vc.iloc[0]
                fig_us = px.bar(
                    u_vc,
                    x="Posts",
                    y="Sub-Theme",
                    orientation="h",
                    text=u_vc.apply(lambda r: f"{r['Posts']:,} ({r['% of Bucket']}%)", axis=1),
                    color_discrete_sequence=[CB_ORANGE],
                    title=f"Scope/Add-Ons ({len(u_sub):,} posts): Upsells/fees #1 ({top_us['% of Bucket']}%)",
                )
                fig_us.update_traces(textposition="outside", cliponaxis=False)
                fig_us.update_layout(yaxis=dict(autorange="reversed"), xaxis=dict(range=[0, max(1, int(u_vc["Posts"].max())) * 1.38]), height=340, margin=dict(l=10, r=100, t=50, b=10))
                st.plotly_chart(fig_us, use_container_width=True)

    with st.expander("View Sub-Theme Severity & Source-Split Table", expanded=False):
        st_rows = []
        if "booking_subtheme" in df_rel.columns:
            for code, grp in df_rel[df_rel["booking_subtheme"] != "not_applicable"].groupby("booking_subtheme"):
                st_rows.append(
                    {
                        "Parent Bucket": "Slot, Rescheduling & App UX",
                        "Sub-Theme": BOOKING_SUBTHEME_LABELS.get(code, clean_label(code)),
                        "Posts": len(grp),
                        "Avg Severity": round(float(grp["severity_num"].mean()), 2),
                        "Source Split": format_source_share_split(grp),
                    }
                )
        if "scope_subtheme" in df_rel.columns:
            for code, grp in df_rel[df_rel["scope_subtheme"] != "not_applicable"].groupby("scope_subtheme"):
                st_rows.append(
                    {
                        "Parent Bucket": "Unclear Scope & Surprise Add-Ons",
                        "Sub-Theme": SCOPE_SUBTHEME_LABELS.get(code, clean_label(code)),
                        "Posts": len(grp),
                        "Avg Severity": round(float(grp["severity_num"].mean()), 2),
                        "Source Split": format_source_share_split(grp),
                    }
                )
        if st_rows:
            st.dataframe(pd.DataFrame(st_rows).sort_values(["Parent Bucket", "Posts"], ascending=[True, False]), use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Table 2: Out-of-Scope Friction (Supply Execution & Price) and Positive Repeat Drivers")
    st.caption("Separated per brief constraints: consumer PMs cannot change pricing, discounts, memberships, or partner payouts/management — so supply/price complaints must not hijack the opportunity ranking.")
    if not t_out.empty:
        st.dataframe(
            t_out[["Theme", "Posts", "% of Relevant", "Avg Severity (1–3)", "Metric-Tree Branch", "Fixable Within Constraints", "Opportunity Score"]],
            use_container_width=True,
            hide_index=True,
        )
        with st.expander("View 2 Verified Quotes Per Out-of-Scope Theme / Positive Driver", expanded=False):
            for row in t_out.itertuples(index=False):
                st.markdown(f"**{row.Theme}** (`{row.Posts}` posts · `{row._3}%` · Severity `{row._4}`)")
                st.markdown(f"<div class='quote-card'>1. {row._8}</div>", unsafe_allow_html=True)
                if row._9 != "—":
                    st.markdown(f"<div class='quote-card'>2. {row._9}</div>", unsafe_allow_html=True)


def render_page_questions(df_rel: pd.DataFrame) -> None:
    q_counts = {qcode: int(df_rel["brief_questions"].apply(lambda lst: qcode in lst).sum()) for qcode, _ in BRIEF_QUESTIONS}
    top_q = max(q_counts, key=q_counts.get)
    top_q_sub = df_rel[df_rel["brief_questions"].apply(lambda lst: top_q in lst)]
    top_q_src = format_source_share_split(top_q_sub)
    st.title(f"All 8 brief questions have tagged evidence — {top_q.split('_')[0]} leads ({q_counts[top_q]:,} posts: {top_q_src})")
    st.caption("Urban Company's brief specifies 8 discovery questions (NL_Proj_Urban_company_brief.md, lines 51–58). Every chart and quote below is filtered strictly to posts explicitly tagged for that question.")

    # Overview bar across Q1-Q8
    q_overview = pd.DataFrame(
        [{"Question": label.split(":")[0], "Full Question": label, "Tagged Posts": q_counts[code]} for code, label in BRIEF_QUESTIONS]
    )
    fig_all = px.bar(
        q_overview,
        x="Question",
        y="Tagged Posts",
        text="Tagged Posts",
        color_discrete_sequence=[CB_BLUE],
        title="Coverage across all 8 brief questions (multi-label per post)",
    )
    fig_all.update_traces(texttemplate="%{text:,}", textposition="outside")
    fig_all.update_layout(height=280, margin=dict(l=10, r=10, t=45, b=10))
    st.plotly_chart(fig_all, use_container_width=True)

    # Chart field pairing per question
    q_chart_specs = {
        "Q1_post_booking_expectations": ("booking_outcome", "Booking outcomes among Q1 posts"),
        "Q2_how_when_need_decided": ("need_trigger", "Need triggers cited in Q2 posts"),
        "Q3_who_turned_to_next": ("repeat_behavior", "Next-step repeat posture in Q3 posts"),
        "Q4_why_rebooked_or_not": ("scope_controllability", "Controllability of stated Q4 reasons"),
        "Q5_outside_app_alternatives": ("external_alternative", "Outside-app alternatives named in Q5 posts"),
        "Q6_returners_vs_non_returners": ("repeat_behavior", "Returner vs. non-returner split in Q6 posts"),
        "Q7_segment_category_city_diff": ("service_category", "Service category breakdown in Q7 posts"),
        "Q8_consistent_unmet_needs": ("metric_branch", "Metric-tree branches of Q8 unmet needs"),
    }

    for qcode, qlabel in BRIEF_QUESTIONS:
        sub = df_rel[df_rel["brief_questions"].apply(lambda lst: qcode in lst)]
        st.divider()
        st.subheader(qlabel)
        n_sub = len(sub)
        if n_sub == 0:
            st.warning("No tagged posts for this question under current filter.")
            continue

        col_l, col_r = st.columns([1.1, 1])
        field, subtitle = q_chart_specs[qcode]
        vc = sub[field].value_counts().reset_index()
        vc.columns = ["Value", "Posts"]
        vc["Label"] = vc["Value"].apply(clean_label)
        top_val = vc.iloc[0]["Label"]
        top_share = round(vc.iloc[0]["Posts"] / n_sub * 100, 1)

        with col_l:
            k1, k2, k3 = st.columns(3)
            k1.metric("Tagged Posts", f"{n_sub:,}")
            k2.metric("Top Signal", top_val[:26])
            k3.metric("Top Signal Share", f"{top_share}%")
            fig_q = px.bar(
                vc.head(6),
                x="Posts",
                y="Label",
                orientation="h",
                text="Posts",
                color_discrete_sequence=[CB_GREEN if qcode in ("Q2_how_when_need_decided", "Q5_outside_app_alternatives") else CB_BLUE],
                title=f"{top_val} accounts for {top_share}% of {qlabel.split(':')[0]} posts",
            )
            fig_q.update_traces(textposition="outside")
            fig_q.update_layout(yaxis=dict(autorange="reversed"), height=260, margin=dict(l=10, r=40, t=45, b=10))
            st.plotly_chart(fig_q, use_container_width=True)

        with col_r:
            st.markdown(f"**Verified Quotes (strictly from `{qcode}` posts):**")
            quotes = pick_verified_quotes(sub, k=3)
            if not quotes:
                st.caption("No quote passed ≥80 rapidfuzz verification for this subset.")
            for q in quotes:
                st.markdown(f"<div class='quote-card'>{fmt_quote_inline(q)}</div>", unsafe_allow_html=True)


def render_page_metric_tree(df_rel: pd.DataFrame) -> None:
    branch_counts = df_rel["metric_branch"].value_counts().reset_index()
    branch_counts.columns = ["Branch", "Posts"]
    branch_counts["%"] = (branch_counts["Posts"] / max(1, len(df_rel)) * 100).round(1)
    top_b = branch_counts.iloc[0]["Branch"] if not branch_counts.empty else "—"
    top_bp = branch_counts.iloc[0]["%"] if not branch_counts.empty else 0.0
    vol_mask = df_rel["voluntary_split"].isin(["Voluntary / Nothing-Went-Wrong", "Pre-Booking / Hesitant Inquiry"])
    vol_src_split = format_source_rate_split(df_rel, vol_mask)

    st.title(f"'{top_b}' leads ({top_bp}%), while voluntary/pre-booking share flips by source ({vol_src_split})")
    st.caption("Decomposing Urban Company's 90-day repeat rate into consumer-journey branches, checking whether findings depend on WHERE we listened (source bias) or WHICH model tagged them.")

    fig_b = px.bar(
        branch_counts,
        x="Posts",
        y="Branch",
        orientation="h",
        text=branch_counts.apply(lambda r: f"{r['Posts']:,} ({r['%']}%)", axis=1),
        color_discrete_sequence=[CB_BLUE],
        title="Posts mapped to each branch of the 90-day repeat metric tree",
    )
    fig_b.update_traces(textposition="outside", cliponaxis=False)
    fig_b.update_layout(yaxis=dict(autorange="reversed"), xaxis=dict(range=[0, max(1, int(branch_counts["Posts"].max())) * 1.24]), height=330, margin=dict(l=10, r=110, t=45, b=10))
    st.plotly_chart(fig_b, use_container_width=True)

    c1, c2 = st.columns(2)
    vol_colors = {
        "Voluntary / Nothing-Went-Wrong": CB_GREEN,
        "Pre-Booking / Hesitant Inquiry": CB_SKY,
        "Friction-Driven (Completed With Friction)": CB_ORANGE,
        "Involuntary (Service Failure / Cancellation)": CB_VERMILION,
        "Unclear": CB_GREY,
    }

    with c1:
        by_src = df_rel.groupby(["source", "voluntary_split"]).size().reset_index(name="Posts")
        by_src["Share %"] = by_src.groupby("source")["Posts"].transform(lambda s: (s / s.sum() * 100).round(1))
        fig_s = px.bar(
            by_src,
            x="source",
            y="Share %",
            color="voluntary_split",
            text="Share %",
            color_discrete_map=vol_colors,
            title="Voluntary vs. Involuntary Split BY SOURCE",
        )
        fig_s.update_traces(texttemplate="%{text:.0f}%", textposition="inside")
        fig_s.update_layout(barmode="stack", height=390, legend_title="Posture", margin=dict(l=10, r=10, t=55, b=10))
        st.plotly_chart(fig_s, use_container_width=True)

    with c2:
        by_tag = df_rel.groupby(["tagged_by", "voluntary_split"]).size().reset_index(name="Posts")
        by_tag["Share %"] = by_tag.groupby("tagged_by")["Posts"].transform(lambda s: (s / s.sum() * 100).round(1))
        fig_t = px.bar(
            by_tag,
            x="tagged_by",
            y="Share %",
            color="voluntary_split",
            text="Share %",
            color_discrete_map=vol_colors,
            title="Voluntary vs. Involuntary Split PER TAGGER",
        )
        fig_t.update_traces(texttemplate="%{text:.0f}%", textposition="inside")
        fig_t.update_layout(barmode="stack", height=390, legend_title="Posture", margin=dict(l=10, r=10, t=55, b=10))
        st.plotly_chart(fig_t, use_container_width=True)

    with st.expander("Exact Counts Table (By Source & By Tagger)", expanded=False):
        st.dataframe(pd.crosstab(df_rel["source"], df_rel["voluntary_split"], margins=True), use_container_width=True)
        st.dataframe(pd.crosstab(df_rel["tagged_by"], df_rel["scope_controllability"], margins=True), use_container_width=True)


def render_page_segments(df_rel: pd.DataFrame, filings: dict[str, Any]) -> None:
    seg_unclear_pct = round((df_rel["segment_proxy"] == "unclear").mean() * 100, 1)
    cat_top = df_rel[df_rel["service_category"] != "unclear"]["service_category"].value_counts()
    top_cat_code = cat_top.index[0] if not cat_top.empty else ""
    top_cat_lbl = clean_label(top_cat_code) if top_cat_code else "Home Services"
    top_cat_n = int(cat_top.iloc[0]) if not cat_top.empty else 0
    cat_src_split = format_source_share_split(df_rel[df_rel["service_category"] == top_cat_code]) if top_cat_code else ""

    st.title(f"'{top_cat_lbl}' leads ({top_cat_n:,} posts: {cat_src_split}), while {seg_unclear_pct}% omit household demographics")
    st.warning(
        f"**Plain methodological disclosure:** Household type, city tier, and acquisition channel are rarely volunteered in public reviews "
        f"(**{seg_unclear_pct}%** `segment_proxy == unclear`). Groups below **15 posts** are explicitly flagged **Low confidence (n < 15)** and must be validated in **Part 3 user interviews**."
    )

    c1, c2 = st.columns(2)
    with c1:
        cat_df = df_rel.groupby(["service_category", "scope_controllability"]).size().reset_index(name="Posts")
        cat_df["Category"] = cat_df["service_category"].apply(clean_label)
        cat_df["Scope"] = cat_df["scope_controllability"].apply(clean_label)
        fig_cat = px.bar(
            cat_df,
            x="Posts",
            y="Category",
            color="Scope",
            orientation="h",
            color_discrete_sequence=CB_PALETTE,
            title="Service Category × Scope Controllability",
        )
        fig_cat.update_layout(yaxis=dict(autorange="reversed"), height=400, margin=dict(l=10, r=10, t=50, b=10))
        st.plotly_chart(fig_cat, use_container_width=True)

    with c2:
        seg_rows = []
        for seg, grp in df_rel.groupby("segment_proxy"):
            n = len(grp)
            cs_pct = round((grp["scope_controllability"].isin(["customer_side_product", "mixed"])).mean() * 100, 1)
            vol_pct = round((grp["voluntary_split"].isin(["Voluntary / Nothing-Went-Wrong", "Pre-Booking / Hesitant Inquiry"])).mean() * 100, 1)
            seg_rows.append(
                {
                    "Segment Proxy": clean_label(seg),
                    "Posts": n,
                    "Confidence": "⚠️ Low confidence (n < 15)" if n < 15 else "Adequate proxy",
                    "% Customer-Side / Mixed": cs_pct,
                    "% Voluntary / Pre-Booking": vol_pct,
                    "Avg Severity": round(float(grp["severity_num"].mean()), 2),
                }
            )
        seg_table = pd.DataFrame(seg_rows).sort_values("Posts", ascending=False)
        st.subheader("Household, City & Tenure Proxy Comparison")
        st.dataframe(seg_table, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Cross-Check: Urban Company Public Filings vs. Customer Voice")
    fc1, fc2, fc3 = st.columns(3)
    fc1.metric("Tier-2 NTV Growth (Q2 FY27)", "~37% YoY", "Outpacing Top-10 Metros")
    fc2.metric("Top-8 Category Penetration", "< 10%", "Massive repeat headroom")
    fc3.metric("India vs. International Frequency", "1x vs. 2.6x", "Filings Q4 FY26 p.10")
    st.info(
        "**Key Management vs. Customer Contradiction Found:** Q1 FY27 Shareholder Letter (p.6) highlights *'AI-led savings in customer and partner support'* as a key margin driver — "
        "while our tagged Play Store & App Store reviews repeatedly cite rigid AI chatbot loops blocking issue resolution and eroding post-booking trust."
    )


def render_page_explorer(df_rel: pd.DataFrame) -> None:
    st.title(f"Evidence Explorer: Filter and audit {len(df_rel):,} relevant posts with verified verbatim quotes")
    st.caption("Defaults to 2021+ posts sorted most severe first (3_high → 1_low), showing only verified quotes when toggled.")

    f1, f2, f3, f4 = st.columns(4)
    sources_opt = sorted(df_rel["source"].dropna().unique().tolist())
    sel_sources = f1.multiselect("Source", sources_opt, default=sources_opt)

    years_opt = sorted([int(y) for y in df_rel["year"].dropna().unique().tolist()])
    default_years = [y for y in years_opt if y >= 2021] or years_opt
    sel_years = f2.multiselect("Year (default 2021+)", years_opt, default=default_years)

    cats_opt = sorted(df_rel["service_category"].dropna().unique().tolist())
    sel_cats = f3.multiselect("Service Category", cats_opt, default=[])

    scopes_opt = sorted(df_rel["scope_controllability"].dropna().unique().tolist())
    sel_scopes = f4.multiselect("Scope Controllability", scopes_opt, default=[])

    f5, f6, f7, f8 = st.columns(4)
    all_bars = sorted({b for lst in df_rel["repeat_barriers_or_drivers"] for b in lst})
    sel_bar = f5.selectbox("Barrier / Driver", ["All"] + all_bars)
    sel_sev = f6.multiselect("Severity", ["3_high", "2_medium", "1_low"], default=["3_high", "2_medium", "1_low"])
    verified_only = f7.checkbox("Verified quotes only (exact/repaired)", value=True)
    kw_search = f8.text_input("Search quote or text", placeholder="e.g. same person, exclusion, local...")

    view = df_rel.copy()
    if sel_sources:
        view = view[view["source"].isin(sel_sources)]
    if sel_years:
        view = view[view["year"].isin(sel_years)]
    if sel_cats:
        view = view[view["service_category"].isin(sel_cats)]
    if sel_scopes:
        view = view[view["scope_controllability"].isin(sel_scopes)]
    if sel_bar != "All":
        view = view[view["repeat_barriers_or_drivers"].apply(lambda lst: sel_bar in lst)]
    if sel_sev:
        view = view[view["severity"].isin(sel_sev)]
    if verified_only:
        view = view[view["quote_status"].isin(["exact", "repaired"]) & (view["evidence_quote"].str.len() > 0)]
    if kw_search.strip():
        q = kw_search.strip().lower()
        view = view[
            view["evidence_quote"].str.lower().str.contains(q, na=False)
            | view["text_snippet"].str.lower().str.contains(q, na=False)
            | view["unmet_need"].str.lower().str.contains(q, na=False)
        ]

    view = view.sort_values(["severity_num", "year", "quote_score"], ascending=[False, False, False])
    st.markdown(f"**Showing `{len(view):,}` matching posts** *(sorted most severe first, then newest)*")

    disp = view[
        ["source", "year", "rating", "severity", "service_category", "booking_outcome", "scope_controllability", "unmet_need", "evidence_quote", "quote_status", "tagged_by"]
    ].head(250).copy()
    disp["service_category"] = disp["service_category"].apply(clean_label)
    disp["booking_outcome"] = disp["booking_outcome"].apply(clean_label)
    disp["scope_controllability"] = disp["scope_controllability"].apply(clean_label)
    st.dataframe(
        disp.rename(
            columns={
                "source": "Source",
                "year": "Year",
                "rating": "Rating",
                "severity": "Severity",
                "service_category": "Category",
                "booking_outcome": "Outcome",
                "scope_controllability": "Scope",
                "unmet_need": "Unmet Need",
                "evidence_quote": "Verified Quote",
                "quote_status": "Quote Status",
                "tagged_by": "Tagger",
            }
        ),
        use_container_width=True,
        hide_index=True,
        height=520,
    )


def render_page_ask(df_rel: pd.DataFrame) -> None:
    st.title("Ask the Data: Grounded semantic search over verified posts with strict bias guardrails")
    st.caption(
        "Retrieves the top 8 source-diverse posts using hybrid fastembed (BAAI/bge-small-en-v1.5) + codebook tag reranking, and answers ONLY from those posts with numbered citations. "
        "Accounts explicitly for both angry-review over-representation and silent non-returner zero-trace bias. Limits: 10 questions/session, 100/day."
    )

    if "ask_count" not in st.session_state:
        st.session_state.ask_count = 0
    if "ask_history" not in st.session_state:
        st.session_state.ask_history = []

    today_key = date.today().isoformat()
    daily_counter = get_daily_chat_counter()
    daily_used = daily_counter.get(today_key, 0)

    c1, c2 = st.columns(2)
    c1.metric("Session Questions Remaining", f"{max(0, 10 - st.session_state.ask_count)} / 10")
    c2.metric("Daily Quota Remaining", f"{max(0, 100 - daily_used)} / 100")

    sample_qs = [
        "Why do customers who had a smooth, satisfactory booking still not book Urban Company again?",
        "What outside-app alternatives (local technicians, WhatsApp, competitors) do people use?",
        "What post-booking scope or exclusion surprises frustrate customers most?",
        "How do customers feel about trying to rebook the same trusted professional?",
    ]
    with st.expander("Example questions to try", expanded=False):
        for sq in sample_qs:
            st.markdown(f"- *{sq}*")

    query = st.text_input("Ask a research question about Urban Company repeat behavior:", placeholder="e.g., Why do satisfied customers switch to local technicians?")
    ask_btn = st.button("Search & Answer", type="primary")

    if ask_btn and query.strip():
        if st.session_state.ask_count >= 10:
            st.error("Session limit reached (10 questions per session). Refresh the session to continue.")
            return
        if daily_used >= 100:
            st.error("Daily query cap reached (100/day). Please try again tomorrow.")
            return

        # Refuse out-of-scope pricing/discount/supply-ops advice
        q_low = query.lower()
        out_of_scope_ask = any(
            phrase in q_low
            for phrase in ("what discount", "give coupon", "how much discount", "change pricing", "partner payout", "pay technicians", "cut prices", "offer cashback")
        )
        if out_of_scope_ask:
            st.warning(
                "**Out of scope per brief constraints:** This discovery engine strictly forbids recommending discounts, coupons, memberships, pricing changes, or partner payout/management changes. "
                "Try asking what **customer-side** friction or behavioral reasons appear in the collected posts instead."
            )
            return

        gemini_key = get_secret_or_env("GEMINI_API_KEY")

        with st.spinner("Retrieving relevant, source-diverse posts via fastembed + hybrid tag reranking..."):
            vecs, embed_ids, emb_model = load_retrieval_assets()
            top_records = hybrid_retrieve_posts(query.strip(), df_rel, vecs, embed_ids, emb_model, k=8, max_per_source=4)

        if not top_records:
            st.info("No matching posts were found in the current exported dataset.")
            return

        context_lines = []
        for i, (sim, rid, row) in enumerate(top_records, 1):
            q_txt = row["evidence_quote"] if row["evidence_quote"] else row["text_snippet"][:260]
            context_lines.append(
                f"[{i}] ({row['source']}, year={row['year']}, rating={row['rating']}, outcome={row['booking_outcome']}, "
                f"scope={row['scope_controllability']}, trigger={row['need_trigger']}, alt={row['external_alternative']}, "
                f"barriers={row['repeat_barriers_or_drivers']}) "
                f"Quote: \"{q_txt}\" | Full snippet: {row['text_snippet'][:380]}"
            )
        ctx_blob = "\n".join(context_lines)

        rag_system = (
            "You are a grounded research assistant for Urban Company's 90-day repeat-booking Discovery Engine.\n"
            "STRICT RULES:\n"
            "1. Answer ONLY using the numbered posts [1]..[8] provided below. Cite claims inline like [1], [3].\n"
            "2. Domain terminology: 'a fine booking' or 'fine experience' means a booking that completed smoothly with no service failure (booking_outcome=completed_fine) — NEVER a monetary fine or penalty.\n"
            "3. If the provided posts do not cover the user's question, state plainly: 'The collected posts do not directly cover this.'\n"
            "4. NEVER recommend discounts, coupons, memberships, pricing changes, or partner payout/management changes.\n"
            "5. End with a 1-sentence Bias Check reminding the reader how public-review angry-customer bias and silent non-returner zero-trace bias limit what these posts alone can prove."
        )

        answer_text = ""

        with st.spinner("Generating cited answer with Gemini..."):
            if gemini_key:
                import requests

                last_err = ""
                for gemini_model in ("gemini-3.1-flash-lite", "gemini-2.5-flash"):
                    try:
                        r = requests.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent",
                            headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key},
                            json={
                                "systemInstruction": {"parts": [{"text": rag_system}]},
                                "contents": [{"role": "user", "parts": [{"text": f"Question: {query.strip()}\n\nRetrieved posts:\n{ctx_blob}"}]}],
                                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 900},
                            },
                            timeout=45,
                        )
                        if r.status_code in (429, 503):
                            last_err = f"{gemini_model} HTTP {r.status_code}"
                            continue
                        r.raise_for_status()
                        parts = ((r.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                        answer_text = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
                        if answer_text:
                            break
                    except Exception as e:
                        last_err = str(e)[:120]
                if not answer_text:
                    answer_text = f"*(Gemini synthesis unavailable right now: {last_err}. Showing retrieved evidence below.)*"
            else:
                answer_text = "*(GEMINI_API_KEY is not configured in st.secrets or .env; showing top semantically matched evidence below.)*"

        st.session_state.ask_count += 1
        daily_counter[today_key] = daily_used + 1
        st.session_state.ask_history.insert(0, {"q": query.strip(), "a": answer_text, "sources": context_lines})
        st.rerun()

    for item in st.session_state.ask_history:
        st.markdown(f"### Q: {item['q']}")
        st.markdown(item["a"])
        with st.expander("View numbered retrieved posts ([1]–[8])", expanded=False):
            for line in item["sources"]:
                st.markdown(f"<div class='quote-card'>{line}</div>", unsafe_allow_html=True)


def render_page_validation(df_tagged: pd.DataFrame, summary: dict[str, Any]) -> None:
    inter = summary.get("inter_model_check") or {}
    core_agree = inter.get("avg_core_field_agreement_pct", 0.0)
    q_stats = df_tagged["quote_status"].value_counts().to_dict() if not df_tagged.empty else {}
    q_tot = max(1, sum(q_stats.values()))
    q_ver_pct = round((q_stats.get("exact", 0) + q_stats.get("repaired", 0)) / q_tot * 100, 1)
    rule_fixes = summary.get("rule_fixes_applied") or {}
    total_rule_fixes = sum(int(v) for v in rule_fixes.values())
    hc_applied = int(summary["counts"].get("human_corrections_applied", 0))

    st.title(f"Validation: {core_agree}% inter-model agreement, {q_ver_pct}% verified quotes, {total_rule_fixes} rule fixes & {hc_applied} human overrides")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Inter-Model Core Agreement", f"{core_agree}%", "gpt-oss-120b vs qwen3.8-27b (n=30)")
    k2.metric("Verified Verbatim Quotes", f"{q_ver_pct}%", f"{q_stats.get('exact',0):,} exact · {q_stats.get('repaired',0):,} repaired")
    k3.metric("Rule & Human Fixes", f"{total_rule_fixes} / {hc_applied}", "rule fixes / CSV overrides")
    gs = summary.get("golden_set") or {}
    gs_lbl = f"{gs.get('overall_agreement_pct')}%" if gs.get("overall_agreement_pct") is not None else "Stage 8"
    k4.metric("20-Item Blind Golden Set", gs_lbl, f"{gs.get('items_labelled', 0)} / 20 labelled")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Inter-Model Agreement (30 Paired Items)")
        fbf = inter.get("field_by_field_agreement") or {}
        if fbf:
            rows = []
            for fld, vals in fbf.items():
                pct = vals.get("pct") if "pct" in vals else vals.get("overlap_pct", 0.0)
                rows.append({"Field": clean_label(fld), "Agreement %": pct})
            im_df = pd.DataFrame(rows).sort_values("Agreement %", ascending=True)
            fig_im = px.bar(
                im_df,
                x="Agreement %",
                y="Field",
                orientation="h",
                text="Agreement %",
                color_discrete_sequence=[CB_BLUE],
                title=f"Two independent Groq models agree on {core_agree}% of core fields (brief_questions bug caught & fixed)",
            )
            fig_im.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
            fig_im.update_layout(xaxis=dict(range=[0, 108]), height=380, margin=dict(l=10, r=30, t=50, b=10))
            st.plotly_chart(fig_im, use_container_width=True)

    with c2:
        st.subheader("Free Quote Verification Audit (rapidfuzz)")
        q_df = pd.DataFrame([{"Status": k, "Posts": v} for k, v in q_stats.items()])
        if not q_df.empty:
            fig_q = px.pie(
                q_df,
                names="Status",
                values="Posts",
                color="Status",
                color_discrete_map={"exact": CB_GREEN, "repaired": CB_ORANGE, "hidden_low_score": CB_VERMILION},
                title=f"{q_ver_pct}% verified substrings (<80 hidden)",
            )
            fig_q.update_layout(height=380, margin=dict(l=10, r=10, t=50, b=10))
            st.plotly_chart(fig_q, use_container_width=True)

    st.subheader("Prompt Versions & Documented AI Corrections (Audit Trail)")
    staff_s2 = summary["counts"].get("staff_replies_excluded_stage2", 1)
    staff_exp = summary["counts"].get("staff_replies_excluded_export", 0)
    rf_fine = rule_fixes.get("stopped_fine_on_failed_reclassified", 0)
    rf_sup = rule_fixes.get("customer_scope_on_pure_supply_reclassified", 0)
    corrections_log = pd.DataFrame(
        [
            {"Stage": "Stage 1 Keywords", "Issue Caught in Audit": "Standalone 'plus', 'safe', 'direct', 'maid' pulled taxes, an Uber review, & InstaHelp", "Fix Applied": "Replaced with precise phrases + expanded InstaHelp exclusion (1,031 excluded)", "Impact": "Play Store 5/5 sample relevant; 937 InstaHelp cleanly excluded"},
            {"Stage": "Public Filings PDFs", "Issue Caught in Audit": "AI expanded NTV as 'New-to-vehicle' & misattributed InstaHelp 30–33% retention to core", "Fix Applied": "Corrected acronym to Net Transaction Value & relabelled Q4 p.13 strictly as InstaHelp", "Impact": "Prevented excluded-category metric from polluting Part 2"},
            {"Stage": "Stage 3 Filter (v1→v2.1)", "Issue Caught in Audit": "Pilot v1 returned 0 'unsure', passed 98% Play Store as 'yes', and dropped 3 Hinglish YouTube comparisons", "Fix Applied": "Tightened 'yes/no/unsure' rules + 6 worked examples + Hinglish safety net", "Impact": "Keep rate 85%→43%; all 3 wrong YouTube 'no' fixed; 403 safety-net rescues in full run"},
            {"Stage": "Stage 4 Tagger (v1→v2)", "Issue Caught in Audit": "Pilot v1 blanked 20/40 good posts as 'unclear' & failed 10/50 with severity='u'", "Fix Applied": "Forbade blanking fields, restricted severity to 1/2/3, added Reddit & chatbot examples", "Impact": "Validation 40/50 → 50/50 (0 fails); core % unclear 52% → 8%"},
            {"Stage": "Stage 4 Inter-Model", "Issue Caught in Audit": "gpt-oss-120b left brief_questions empty on 20/30 items (vs Qwen 6/30)", "Fix Applied": "Added mandatory 1–3 brief_questions rule before full parallel launch", "Impact": "0 empty brief_questions across all 5,110 production tagged items"},
            {"Stage": "Stage 6 Pre-Publish Audit", "Issue Caught in Audit": f"20-post & schema audit caught {rf_fine} 'stopped_despite_fine_experience' on failed/friction bookings, {rf_sup} 'customer_side_product' on pure supply barriers, and overly broad booking/scope buckets", "Fix Applied": f"Added deterministic rule fixes + sub-theme breakdown + defensive staff-reply filter ({staff_s2} excluded Stage 2, {staff_exp} at export)", "Impact": f"{total_rule_fixes} tags cleanly corrected; broad buckets unpacked into concrete product sub-themes"},
        ]
    )
    st.dataframe(corrections_log, use_container_width=True, hide_index=True)

    st.subheader("How This Workflow Accounts for Both Biases & Known Limitations")
    st.markdown(
        """
| Bias / Limitation | How Our Workflow Accounts for It | What Public Data Still Cannot Prove (Part 3 Interviews) |
|---|---|---|
| **Bias #1: Angry-customer over-representation** | Pulled **balanced 6,000/star (1★–5★)** on Play Store; explicitly classified `scope_controllability` and separated out-of-scope supply/price complaints into Table 2 on Opportunity Ranking; showed **Source-by-Source** voluntary vs. involuntary splits. | True company-wide prevalence of bad vs. smooth bookings across silent satisfied customers. |
| **Bias #2: Silent non-returners leave zero trace** | Added **non-UC home-service discussions** from Indian city/household Reddit (`r/bangalore`, `r/mumbai`, `r/delhi`, `r/TwoXIndia`, etc.) and YouTube comments to observe off-platform habits (local pros, maids, WhatsApp, DIY). | Why specific households with a completely fine UC booking quietly never opened the app again within 90 days. |
| **Segment sparsity (~80% unclear)** | Flagged all segment groups with `n < 15` as **Low confidence**; avoided inventing demographics. | Exact household type, city tier, total booking count, and first-discovery channel — collected directly in Part 3 interviews. |
| **Curated Reddit / Source Mix** | Added live **"Exclude curated Reddit"** toggle in the sidebar so evaluators can test whether every headline finding holds without Reddit. | Trustpilot blocked by HTTP 403 anti-bot gate (stopped without bypassing). |
        """
    )

    st.subheader("Human Corrections (`data/manual/corrections.csv`) & 20-Item Golden Set")
    hc = summary.get("human_corrections") or []
    if hc:
        st.dataframe(pd.DataFrame(hc), use_container_width=True, hide_index=True)
    else:
        st.info("No human corrections uploaded yet (`data/manual/corrections.csv` will populate automatically in Stage 8 when `pipeline/review_export.py` and `pipeline/import_review.py` run).")


# ==============================================================================
# MAIN APP NAVIGATION
# ==============================================================================

def main() -> None:
    df_all, summary, filings = load_exports()

    # Query param support (?page=overview..validation) for clean navigation & bookmarking
    qp_page = st.query_params.get("page", "overview")
    page_keys = [k for k, _ in PAGES]
    page_labels = [lbl for _, lbl in PAGES]
    default_idx = page_keys.index(qp_page) if qp_page in page_keys else 0

    with st.sidebar:
        st.markdown("## 🏠 UC Repeat Discovery")
        st.caption("Consumer Growth · Part 1 Engine")
        selected_label = st.radio("Navigate", page_labels, index=default_idx)
        selected_key = page_keys[page_labels.index(selected_label)]
        st.divider()
        exclude_reddit = st.toggle("Exclude curated Reddit", value=False, help="Check whether every finding holds without Reddit threads.")
        st.caption(f"Exported: `{summary['data_as_of']}`")
        st.caption("Reads strictly from `data/export/`.")

    df_tagged = df_all[df_all["source"] != "reddit"].copy() if exclude_reddit else df_all.copy()
    df_rel = df_tagged[df_tagged["is_relevant_final"]].copy()

    render_banner(summary, df_tagged, df_rel, exclude_reddit)

    if selected_key == "overview":
        render_page_overview(df_tagged, df_rel, summary, filings, exclude_reddit=exclude_reddit)
    elif selected_key == "opportunities":
        render_page_opportunities(df_rel)
    elif selected_key == "questions":
        render_page_questions(df_rel)
    elif selected_key == "metric_tree":
        render_page_metric_tree(df_rel)
    elif selected_key == "segments":
        render_page_segments(df_rel, filings)
    elif selected_key == "explorer":
        render_page_explorer(df_rel)
    elif selected_key == "ask":
        render_page_ask(df_rel)
    elif selected_key == "validation":
        render_page_validation(df_tagged, summary)


if __name__ == "__main__":
    main()
