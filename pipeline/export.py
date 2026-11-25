"""Stage 6 Export Pipeline: builds public-safe data/export/ for app/streamlit_app.py.

The Streamlit app reads ONLY from data/export/. This script:
1. Merges all Stage 4 tagged outputs (tagged.jsonl + any shard files) with zero duplicates.
2. Applies human corrections from data/manual/corrections.csv (if present).
3. Enforces ONE single consistent definition everywhere:
   - `tagged`: unique valid tagged items (`tag_status == "ok"`).
   - `relevant`: subset of `tagged` with `is_relevant == "yes"` AND `service_category != "instahelp_excluded"`.
4. Computes metric-tree branch and voluntary/nothing-went-wrong vs. involuntary/failure splits.
5. Precomputes fastembed (`BAAI/bge-small-en-v1.5`) embeddings for fast, low-memory Ask-the-Data RAG.
6. Writes anonymized, public-safe artifacts to data/export/:
   - data/export/posts.parquet
   - data/export/summary_stats.json
   - data/export/filings_export.json
   - data/export/embeddings.npz + data/export/embed_ids.json

Usage:
    python -m pipeline.export
    python -m pipeline.export --wait-for-ai 30
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

from collect.utils import ROOT_DIR, scrub_pii

PROCESSED_DIR = ROOT_DIR / "data/processed"
EXPORT_DIR = ROOT_DIR / "data/export"
MANUAL_CORRECTIONS_PATH = ROOT_DIR / "data/manual/corrections.csv"
GOLDEN_SET_RESULTS_PATH = ROOT_DIR / "data/processed/golden_set_results.json"

EMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384

QUESTION_PLAIN: dict[str, str] = {
    "Q1_post_booking_expectations": "post-booking expectations and follow-up after service completion",
    "Q2_how_when_need_decided": "how and when household decides a home service is needed",
    "Q3_who_turned_to_next": "who customer turns to next time for same or different service",
    "Q4_why_rebooked_or_not": "why customer did or did not book again",
    "Q5_outside_app_alternatives": "outside-app alternatives like local technicians, maids, neighbours, WhatsApp, DIY",
    "Q6_returners_vs_non_returners": "what separates repeat customers from non-returners",
    "Q7_segment_category_city_diff": "differences by category, city, household type, or first discovery",
    "Q8_consistent_unmet_needs": "unmet customer needs in home services",
}

# Map every barrier/driver + scope to one of 5 Urban Company 90-day repeat metric-tree branches
BARRIER_TO_BRANCH: dict[str, str] = {
    "no_post_service_followup_or_care": "1. Post-Booking Care & Expectation Closure",
    "unclear_scope_or_surprise_addons": "1. Post-Booking Care & Expectation Closure",
    "low_perceived_frequency_or_no_trigger": "2. Need Recognition & Repeat Trigger",
    "single_category_mental_model": "2. Need Recognition & Repeat Trigger",
    "prefers_local_provider_or_maid": "3. Platform Recall vs. Off-Platform Leakage",
    "hired_uc_partner_directly_offline": "3. Platform Recall vs. Off-Platform Leakage",
    "society_whatsapp_or_neighbour_referral": "3. Platform Recall vs. Off-Platform Leakage",
    "cannot_rebook_same_trusted_pro": "4. Discovery, Trust & Rebooking Friction",
    "booking_or_slot_scheduling_friction": "4. Discovery, Trust & Rebooking Friction",
    "trust_safety_or_home_privacy": "4. Discovery, Trust & Rebooking Friction",
    "service_quality_or_partner_failure": "5. Delivery Quality & Price (Out of Consumer-PM Scope)",
    "perceived_high_price_vs_local": "5. Delivery Quality & Price (Out of Consumer-PM Scope)",
    "convenience_standardization_driver": "Positive Repeat Driver (Retention Anchor)",
    "hygiene_and_professionalism_driver": "Positive Repeat Driver (Retention Anchor)",
    "unclear": "Unclassified / General",
}

IN_SCOPE_BARRIERS = {
    "single_category_mental_model",
    "low_perceived_frequency_or_no_trigger",
    "prefers_local_provider_or_maid",
    "hired_uc_partner_directly_offline",
    "society_whatsapp_or_neighbour_referral",
    "cannot_rebook_same_trusted_pro",
    "unclear_scope_or_surprise_addons",
    "booking_or_slot_scheduling_friction",
    "no_post_service_followup_or_care",
    "trust_safety_or_home_privacy",
}

OUT_OF_SCOPE_OR_DRIVER = {
    "service_quality_or_partner_failure",
    "perceived_high_price_vs_local",
    "convenience_standardization_driver",
    "hygiene_and_professionalism_driver",
}

SEVERITY_NUM = {"1_low": 1.0, "2_medium": 2.0, "3_high": 3.0}

# Brand-side platform staff reply detector (careful NOT to match customer reviews saying "Dear Urban Company Team")
_STAFF_APOLOGY_RE = re.compile(
    r"(we\s+(deeply\s+)?apologize\s+for\s+the\s+inconvenience|we\s+are\s+sorry\s+for\s+the\s+inconvenience|"
    r"we\s+regret\s+the\s+inconvenience|thank\s+you\s+for\s+sharing\s+your\s+(valuable\s+)?feedback|"
    r"thank\s+you\s+for\s+reaching\s+out\s+to\s+us)",
    re.I,
)
_STAFF_CTA_RE = re.compile(
    r"(please\s+(share|dm|send|mail|write)\s+(your\s+)?(registered|booking|order|contact|phone|email)|"
    r"reach\s+out\s+to\s+us\s+at|write\s+to\s+us\s+at|regards,?\s*team\s+urban\s*(company|clap)|"
    r"urban\s*company\s+support\s+team|our\s+team\s+will\s+(get\s+in\s+touch|look\s+into|assist\s+you))",
    re.I,
)


def is_platform_staff_reply(text: str) -> bool:
    """Defensive brand-side staff reply detector (ignores customer letters like 'Dear Urban Company Team')."""
    if not text:
        return False
    low = text.strip().lower()
    if low.startswith(("dear urban", "hi urban", "hello urban")):
        return False
    return bool(_STAFF_APOLOGY_RE.search(text) and _STAFF_CTA_RE.search(text))


_SELF_PROMO_AD_RE = re.compile(
    r"(msg\s+kijiye|message\s+kijiye|dm\s+kijiye|only\s+for\s+ladies.*msg|"
    r"hum\s+kham\s+to\s+top-?notch|safai\s+wale\s+to\s+hum\s+bhii?\s+hai)",
    re.I,
)


def is_self_promo_ad(text: str, source: str) -> bool:
    """Exclude local vendor / competitor self-promotion comments caught in Stage 8 golden-set audit."""
    if source != "youtube" or not text:
        return False
    return bool(_SELF_PROMO_AD_RE.search(text))


def apply_audit_rule_fixes(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Deterministic post-tag audit fixes caught during Prompt 9 suspicious-tag inspection."""
    fixes = {
        "stopped_fine_on_failed_reclassified": 0,
        "customer_scope_on_pure_supply_reclassified": 0,
    }
    for r in rows:
        if r.get("tag_status") != "ok":
            continue
        # Fix 1: stopped_despite_fine_experience requires completed_fine/unclear, NOT failed or friction outcomes
        if (
            r.get("repeat_behavior") == "stopped_despite_fine_experience"
            and r.get("booking_outcome") in {"service_failed_or_cancelled", "completed_with_friction"}
        ):
            r["repeat_behavior"] = "stopped_after_bad_experience"
            r["rule_fixed"] = True
            fixes["stopped_fine_on_failed_reclassified"] += 1

        # Fix 2: customer_side_product when the ONLY barrier is service_quality_or_partner_failure -> supply_partner_execution
        bars = r.get("repeat_barriers_or_drivers") or []
        if (
            r.get("scope_controllability") == "customer_side_product"
            and bars == ["service_quality_or_partner_failure"]
        ):
            r["scope_controllability"] = "supply_partner_execution"
            r["rule_fixed"] = True
            fixes["customer_scope_on_pure_supply_reclassified"] += 1
    return rows, fixes


def classify_booking_subtheme(rec: dict[str, Any], full_text: str) -> str:
    """Break broad booking_or_slot_scheduling_friction bucket into concrete sub-themes."""
    bars = rec.get("repeat_barriers_or_drivers") or []
    if "booking_or_slot_scheduling_friction" not in bars:
        return "not_applicable"
    t = f"{full_text} {rec.get('unmet_need') or ''} {rec.get('evidence_quote') or ''}".lower()
    if re.search(
        r"(chatbot|chat\s*bot|ai\s+bot|ai\s+for\s+call|ai\s+support|ai\s+chat|automated|bot\s+repl|no\s+human|"
        r"customer\s+(care|support|service)|help\s*center|helpdesk|toll.?free|no\s+call|can.?t\s+call|cannot\s+call|"
        r"no\s+phone|unable\s+to\s+contact|contact\s+support|contact\s+number|helpline|support\s+team|complaint|"
        r"grievance|no\s+response|no\s+reply|no\s+support)",
        t,
    ):
        return "ai_chatbot_and_support_dead_ends"
    if re.search(
        r"(reschedul|re-schedul|cancel\w*\s+(fee|charge|penalty|money|amount|policy)|auto.?cancel|"
        r"cancel\w*\s+by|cancel\w*\s+my|cancel\w*\s+the|cancel\w*\s+booking|cancel\w*\s+appointment|"
        r"cancel\w*\s+at\s+last|last\s+minute\s+cancel|kept\s+postpon|postpon|pushed\s+to|slot\s+changed|booking\s+cancel)",
        t,
    ):
        return "involuntary_rescheduling_and_cancel_fees"
    if re.search(
        r"(no\s+slot|slot\w*\s+not\s+avail|slot\w*\s+unavail|slot\w*\s+issue|no\s+availab|not\s+available|unavailab|"
        r"no\s+professional|no\s+partner|no\s+technician|no\s+service\s+provider|area\s+not|pincode|pin\s*code|"
        r"unserviceable|doesn.?t\s+serve|not\s+serv|fully\s+booked|waiting\s+list|same\s+day|urgent|immediate|slots?\b)",
        t,
    ):
        return "slot_unavailability_and_unserved_areas"
    if re.search(
        r"(cart|minimum|min\s+order|advance|coupon|voucher|pack|subscription|membership|payment|upi|wallet|refund|"
        r"glitch|crash|bug|hang|freeze|login|otp|address|location|ui\b|ux\b|interface|navigate|confusing|update|"
        r"app\s+(not|is|keeps|doesn|has|issue|problem|worst|useless|slow|pathetic)|book\w*\s+process|notification|"
        r"notified|reminder|message|chat\s+option|filter|search|checkout|booking\s+app)",
        t,
    ):
        return "app_ux_cart_minimums_and_booking_limits"
    if re.search(
        r"(didn.?t\s+show|did\s+not\s+show|no\s+show|no-show|late|delay|never\s+arrived|didn.?t\s+come|"
        r"did\s+not\s+come|no\s+one\s+came|didn.?t\s+turn\s+up|rude|untrained|unprofessional|poor\s+service|"
        r"bad\s+service|time\s+waste|wasted\s+my\s+time)",
        t,
    ):
        return "partner_cancellation_or_no_show_supply"
    return "other_booking_friction"


def classify_scope_subtheme(rec: dict[str, Any], full_text: str) -> str:
    """Break broad unclear_scope_or_surprise_addons bucket into concrete sub-themes."""
    bars = rec.get("repeat_barriers_or_drivers") or []
    if "unclear_scope_or_surprise_addons" not in bars:
        return "not_applicable"
    t = f"{full_text} {rec.get('unmet_need') or ''} {rec.get('evidence_quote') or ''}".lower()
    if re.search(
        r"(upsell|add.?on|extra\s+(charge|money|cost|amount|fee|pay|rs|rupee|\d)|hidden\s+(charge|fee|cost)|"
        r"spare\s*part|gas\s*(refill|fill|charge)|overcharg|inflat|cart|minimum|convenience\s+fee|visitation|"
        r"visiting\s+charge|inspection\s+(fee|charge)|surcharge|surge|bill|invoice|tax|gst|price\s+hike|"
        r"charged\s+extra|more\s+money|money\s+loot|loot|scam|fraud|charge\w*|cost\w*|fee\b|fees\b|expensive|"
        r"overpriced|pricing|price\b)",
        t,
    ):
        return "surprise_addons_hidden_fees_and_upselling"
    if re.search(
        r"(scope|exclus|includ|rate\s*card|warranty|guarantee|terms|condition|false\s+promise|mislead|"
        r"not\s+mentioned|checklist|what\s+is\s+covered|stain|incomplete|half\s+done|skipped|didn.?t\s+clean|"
        r"not\s+clean|promise|package|detail|explain|clarity|transparent|expect|work\s+done|proper|thorough)",
        t,
    ):
        return "unclear_inclusions_exclusions_and_warranty"
    return "other_scope_clarity"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def load_merged_tagged() -> list[dict[str, Any]]:
    """Combine tagged.jsonl + shard files with zero duplicate IDs (preferring tag_status=='ok')."""
    seen: dict[str, dict[str, Any]] = {}
    for fn in ("tagged_gpt120b.jsonl", "tagged_qwen27b.jsonl", "tagged.jsonl"):
        for r in load_jsonl(PROCESSED_DIR / fn):
            rid = r.get("id")
            if not rid:
                continue
            if rid not in seen or (seen[rid].get("tag_status") != "ok" and r.get("tag_status") == "ok"):
                seen[rid] = r
    return list(seen.values())


def load_human_corrections() -> list[dict[str, Any]]:
    """Load data/manual/corrections.csv if it exists."""
    if not MANUAL_CORRECTIONS_PATH.exists():
        return []
    corrections = []
    with MANUAL_CORRECTIONS_PATH.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("id") and row.get("field"):
                corrections.append(dict(row))
    return corrections


def apply_human_corrections(rows: list[dict[str, Any]], corrections: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Apply human overrides from corrections.csv onto matching tagged rows."""
    if not corrections:
        return rows, 0
    by_id = {r["id"]: r for r in rows}
    applied = 0
    for c in corrections:
        rec = by_id.get(c["id"])
        if not rec:
            continue
        field = c["field"].strip()
        new_val = c.get("correct_value", "").strip()
        if not field or not new_val:
            continue
        if field in {"repeat_barriers_or_drivers", "brief_questions"}:
            rec[field] = [v.strip() for v in new_val.split(",") if v.strip()]
        else:
            rec[field] = new_val
        rec["human_corrected"] = True
        applied += 1
    return rows, applied


def assign_metric_branch(rec: dict[str, Any]) -> str:
    """Assign primary metric-tree branch for a tagged post."""
    bars = rec.get("repeat_barriers_or_drivers") or []
    # Prefer in-scope customer-side barrier if present
    for b in bars:
        if b in IN_SCOPE_BARRIERS:
            return BARRIER_TO_BRANCH[b]
    for b in bars:
        if b in BARRIER_TO_BRANCH and b != "unclear":
            return BARRIER_TO_BRANCH[b]
    scope = rec.get("scope_controllability", "unclear")
    if scope in {"supply_partner_execution", "pricing_or_discount_only"}:
        return "5. Delivery Quality & Price (Out of Consumer-PM Scope)"
    if scope == "customer_side_product":
        return "4. Discovery, Trust & Rebooking Friction"
    return "Unclassified / General"


def assign_voluntary_split(rec: dict[str, Any]) -> str:
    """Classify repeat/non-repeat posture into Voluntary/Nothing-Went-Wrong vs Involuntary/Friction-Failure."""
    out = rec.get("booking_outcome", "unclear")
    rb = rec.get("repeat_behavior", "unclear")
    if out == "service_failed_or_cancelled" or rb == "stopped_after_bad_experience":
        return "Involuntary (Service Failure / Cancellation)"
    if out == "completed_with_friction":
        return "Friction-Driven (Completed With Friction)"
    if rb in {"stopped_despite_fine_experience", "single_category_or_rare_user", "switched_off_platform"} or out == "completed_fine":
        return "Voluntary / Nothing-Went-Wrong"
    if out == "never_booked_uc" or rb == "considering_or_asking":
        return "Pre-Booking / Hesitant Inquiry"
    return "Unclear"


def _build_semantic_doc(row: Any) -> str:
    """Build clean, boilerplate-free semantic text for gemini-embedding-2 RETRIEVAL_DOCUMENT."""
    parts = [row.text_snippet]
    if row.context_snippet and row.source in {"reddit", "youtube"}:
        parts.append(f"Thread context: {row.context_snippet}")
    if row.unmet_need and row.unmet_need != "unclear":
        parts.append(f"Unmet need: {row.unmet_need}.")
    clean_bars = [b.replace("_", " ") for b in row.repeat_barriers_or_drivers if b != "unclear"]
    if clean_bars:
        parts.append(f"Themes: {', '.join(clean_bars)}.")
    if row.external_alternative and row.external_alternative not in {"unclear", "none_uses_uc"}:
        parts.append(f"Outside-app alternative: {row.external_alternative.replace('_', ' ')}.")
    if row.need_trigger and row.need_trigger != "unclear":
        parts.append(f"Need trigger: {row.need_trigger.replace('_', ' ')}.")
    if row.repeat_behavior and row.repeat_behavior != "unclear":
        parts.append(f"Repeat posture: {row.repeat_behavior.replace('_', ' ')}.")
    if row.segment_proxy and row.segment_proxy != "unclear":
        parts.append(f"Customer segment: {row.segment_proxy.replace('_', ' ')}.")
    q_desc = [QUESTION_PLAIN[q] for q in row.brief_questions if q in QUESTION_PLAIN]
    if q_desc:
        parts.append(f"Answers: {'; '.join(q_desc)}.")
    return " ".join(p.strip() for p in parts if p and p.strip())


def compute_embeddings(df_rel: pd.DataFrame, force_rebuild: bool = False, changed_ids: set[str] | None = None) -> None:
    """Precompute normalized fastembed vectors (reusing unchanged cached vectors when safe)."""
    from fastembed import TextEmbedding

    if df_rel.empty:
        np.savez_compressed(EXPORT_DIR / "embeddings.npz", embeddings=np.zeros((0, EMBED_DIM), dtype=np.float16))
        (EXPORT_DIR / "embed_ids.json").write_text("[]", encoding="utf-8")
        return

    ids = df_rel["id"].tolist()
    docs = [_build_semantic_doc(row) for row in df_rel.itertuples(index=False)]
    changed_ids = changed_ids or set()

    # Fast incremental path: reuse existing vectors for unchanged IDs when model matches
    cached_vecs_path = EXPORT_DIR / "embeddings.npz"
    cached_ids_path = EXPORT_DIR / "embed_ids.json"
    cached_meta_path = EXPORT_DIR / "embed_meta.json"
    cached_map: dict[str, np.ndarray] = {}
    if not force_rebuild and cached_vecs_path.exists() and cached_ids_path.exists() and cached_meta_path.exists():
        meta = json.loads(cached_meta_path.read_text(encoding="utf-8"))
        if meta.get("model") == EMBED_MODEL_NAME and int(meta.get("dim", 0)) == EMBED_DIM:
            old_vecs = np.load(cached_vecs_path)["embeddings"]
            old_ids = json.loads(cached_ids_path.read_text(encoding="utf-8"))
            if len(old_ids) == len(old_vecs):
                cached_map = {oid: old_vecs[i] for i, oid in enumerate(old_ids) if oid not in changed_ids}

    to_embed_idx = [i for i, rid in enumerate(ids) if rid not in cached_map]
    new_vecs_map: dict[str, np.ndarray] = {}
    if to_embed_idx:
        model = TextEmbedding(model_name=EMBED_MODEL_NAME)
        sub_docs = [docs[i] for i in to_embed_idx]
        sub_vecs = np.asarray(list(model.embed(sub_docs)), dtype=np.float32)
        norms = np.linalg.norm(sub_vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1.0, norms)
        sub_vecs = (sub_vecs / norms).astype(np.float16)
        for pos, idx in enumerate(to_embed_idx):
            new_vecs_map[ids[idx]] = sub_vecs[pos]

    vecs = np.stack([cached_map[rid] if rid in cached_map else new_vecs_map[rid] for rid in ids]).astype(np.float16)

    np.savez_compressed(EXPORT_DIR / "embeddings.npz", embeddings=vecs)
    (EXPORT_DIR / "embed_ids.json").write_text(json.dumps(ids), encoding="utf-8")
    (EXPORT_DIR / "embed_meta.json").write_text(
        json.dumps({"model": EMBED_MODEL_NAME, "dim": EMBED_DIM, "count": len(ids)}, indent=2),
        encoding="utf-8",
    )


def run_export(skip_embeddings: bool = False) -> dict[str, Any]:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)

    clean_rows = load_jsonl(PROCESSED_DIR / "clean.jsonl")
    clean_map = {r["id"]: r for r in clean_rows}
    dropped_rows = load_jsonl(PROCESSED_DIR / "dropped.jsonl")
    filtered_rows = load_jsonl(PROCESSED_DIR / "filtered.jsonl")
    tagged_raw = load_merged_tagged()

    # Apply deterministic Prompt 9 audit fixes BEFORE human overrides (so human corrections always win)
    tagged_audited, rule_fixes = apply_audit_rule_fixes(tagged_raw)

    corrections = load_human_corrections()
    tagged_all, corrections_applied = apply_human_corrections(tagged_audited, corrections)

    # Defensive platform staff reply & YouTube self-promo ad exclusion check
    staff_excluded_export = 0
    promo_excluded_export = 0
    pii_scrubbed_export = 0
    changed_ids: set[str] = set()
    ok_rows = []
    failed_count = 0
    for r in tagged_all:
        if r.get("tag_status") != "ok":
            failed_count += 1
            continue
        src_rec = clean_map.get(r["id"], {})
        raw_txt = str(src_rec.get("text") or "")
        src_name = str(r.get("source") or src_rec.get("source") or "unknown")
        if is_platform_staff_reply(raw_txt):
            staff_excluded_export += 1
            continue
        if is_self_promo_ad(raw_txt, src_name):
            promo_excluded_export += 1
            continue
        ok_rows.append(r)

    # ONE consistent definition everywhere:
    # tagged = tag_status == "ok" (excluding any platform staff replies / self-promo ads)
    # relevant = tagged AND is_relevant == "yes" AND service_category != "instahelp_excluded"
    export_records: list[dict[str, Any]] = []
    for r in ok_rows:
        src_rec = clean_map.get(r["id"], {})
        raw_text = str(src_rec.get("text") or "")
        raw_ctx = str(src_rec.get("context") or "")
        raw_quote = str(r.get("evidence_quote") or "")
        full_text = scrub_pii(raw_text)
        ctx = scrub_pii(raw_ctx)
        clean_quote = scrub_pii(raw_quote)
        if full_text != raw_text or ctx != raw_ctx or clean_quote != raw_quote:
            pii_scrubbed_export += 1
            changed_ids.add(r["id"])
        is_rel_final = (r.get("is_relevant") == "yes") and (r.get("service_category") != "instahelp_excluded")

        sev_str = str(r.get("severity") or "2_medium")
        export_records.append(
            {
                "id": r["id"],
                "source": str(r.get("source") or src_rec.get("source") or "unknown"),
                "year": int(r["year"]) if r.get("year") is not None else None,
                "rating": int(r["rating"]) if r.get("rating") is not None else None,
                "language": str(r.get("language") or "en"),
                "is_native_hardware": bool(r.get("is_native_hardware", False)),
                "tagged_by": str(r.get("tagged_by") or "unknown"),
                "is_relevant_raw": str(r.get("is_relevant") or "unclear"),
                "is_relevant_final": bool(is_rel_final),
                "service_category": str(r.get("service_category") or "unclear"),
                "booking_outcome": str(r.get("booking_outcome") or "unclear"),
                "repeat_behavior": str(r.get("repeat_behavior") or "unclear"),
                "scope_controllability": str(r.get("scope_controllability") or "unclear"),
                "repeat_barriers_or_drivers": list(r.get("repeat_barriers_or_drivers") or ["unclear"]),
                "booking_subtheme": classify_booking_subtheme(r, full_text),
                "scope_subtheme": classify_scope_subtheme(r, full_text),
                "external_alternative": str(r.get("external_alternative") or "unclear"),
                "need_trigger": str(r.get("need_trigger") or "unclear"),
                "unmet_need": str(r.get("unmet_need") or "unclear"),
                "emotion": str(r.get("emotion") or "unclear"),
                "severity": sev_str,
                "severity_num": SEVERITY_NUM.get(sev_str, 2.0),
                "segment_proxy": str(r.get("segment_proxy") or "unclear"),
                "brief_questions": list(r.get("brief_questions") or []),
                "evidence_quote": clean_quote,
                "quote_status": str(r.get("quote_status") or "hidden_low_score"),
                "quote_score": float(r.get("quote_score") or 0.0),
                "grounding_flags": list(r.get("grounding_flags") or []),
                "rule_fixed": bool(r.get("rule_fixed", False)),
                "human_corrected": bool(r.get("human_corrected", False)),
                "metric_branch": assign_metric_branch(r),
                "voluntary_split": assign_voluntary_split(r),
                # Public-safe anonymized snippet (PII-scrubbed including spaced Indian phone numbers)
                "text_snippet": full_text[:600],
                "context_snippet": ctx[:200],
                "subreddit": str(src_rec.get("subreddit") or ""),
            }
        )

    df = pd.DataFrame(export_records)
    df.to_parquet(EXPORT_DIR / "posts.parquet", index=False)

    df_rel = df[df["is_relevant_final"]].copy() if not df.empty else df
    if not skip_embeddings:
        compute_embeddings(df_rel, changed_ids=changed_ids)

    # Copy clean filings summary into data/export/
    filings_src = PROCESSED_DIR / "filings_insights.json"
    filings_data = json.loads(filings_src.read_text(encoding="utf-8")) if filings_src.exists() else {}
    (EXPORT_DIR / "filings_export.json").write_text(
        json.dumps(filings_data, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Load inter-model check & optional golden set results
    inter_model_path = PROCESSED_DIR / "inter_model_check.json"
    inter_model = json.loads(inter_model_path.read_text(encoding="utf-8")) if inter_model_path.exists() else {}
    golden_set = (
        json.loads(GOLDEN_SET_RESULTS_PATH.read_text(encoding="utf-8"))
        if GOLDEN_SET_RESULTS_PATH.exists()
        else {"status": "pending_stage_8", "items_labelled": 0}
    )

    # Funnel & metadata summary
    filt_eligible = sum(1 for r in filtered_rows if r.get("relevant") in {"yes", "unsure"})
    filt_by_model = dict(Counter(r.get("model", "unknown") for r in filtered_rows))
    tag_by_model = dict(Counter(r["tagged_by"] for r in export_records))
    quote_stats = dict(Counter(r["quote_status"] for r in export_records))

    # Date coverage by source
    date_cov = {}
    for src in sorted(set(r.get("source", "unknown") for r in clean_rows)):
        yrs = sorted(int(r["year"]) for r in clean_rows if r.get("source") == src and r.get("year"))
        date_cov[src] = f"{yrs[0]}–{yrs[-1]}" if yrs else "unknown"

    now_utc = datetime.now(timezone.utc)
    summary = {
        "data_as_of": now_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "data_as_of_date": now_utc.strftime("%d %b %Y"),
        "definitions": {
            "tagged": "Unique posts with valid Stage 4 codebook schema output (tag_status == 'ok').",
            "relevant": "Tagged posts marked is_relevant == 'yes' AND excluding InstaHelp (service_category != 'instahelp_excluded').",
        },
        "counts": {
            "pulled_raw_total": 32769,
            "stage1_kept_total": 8702,
            "instahelp_excluded_stage1": 1031,
            "clean_total": len(clean_rows),
            "dropped_stage2": len(dropped_rows),
            "filtered_total": len(filtered_rows),
            "filtered_yes_or_unsure": filt_eligible,
            "tagged_valid": len(export_records),
            "tagged_failed": failed_count,
            "relevant_final": int(df_rel.shape[0]),
            "instahelp_excluded_stage4": int(((df["is_relevant_raw"] == "yes") & (df["service_category"] == "instahelp_excluded")).sum()) if not df.empty else 0,
            "staff_replies_excluded_stage2": sum(1 for r in dropped_rows if "staff" in str(r.get("drop_reason", "")).lower()),
            "staff_replies_excluded_export": staff_excluded_export,
            "self_promo_excluded_export": promo_excluded_export,
            "pii_scrubbed_export": pii_scrubbed_export,
            "human_corrections_applied": corrections_applied,
        },
        "rule_fixes_applied": rule_fixes,
        "subtheme_counts": {
            "booking_or_slot_scheduling_friction": dict(
                Counter(r["booking_subtheme"] for r in export_records if r["is_relevant_final"] and r["booking_subtheme"] != "not_applicable")
            ),
            "unclear_scope_or_surprise_addons": dict(
                Counter(r["scope_subtheme"] for r in export_records if r["is_relevant_final"] and r["scope_subtheme"] != "not_applicable")
            ),
        },
        "sources_stage1": [
            {"source": "play_store", "label": "Google Play Store (Urban Company)", "pulled": 30000, "stage1_kept": 7193, "clean": sum(1 for r in clean_rows if r["source"] == "play_store"), "date_range": date_cov.get("play_store", ""), "notes": "Balanced 6,000/star (1★–5★) to counter Bias #1; 937 InstaHelp excluded"},
            {"source": "app_store", "label": "Apple App Store (India RSS)", "pulled": 500, "stage1_kept": 136, "clean": sum(1 for r in clean_rows if r["source"] == "app_store"), "date_range": date_cov.get("app_store", ""), "notes": "Official iTunes RSS feed; 54 InstaHelp excluded"},
            {"source": "youtube", "label": "YouTube Comments (UC & Home Services)", "pulled": 1789, "stage1_kept": 673, "clean": sum(1 for r in clean_rows if r["source"] == "youtube"), "date_range": date_cov.get("youtube", ""), "notes": "42 videos across 8 queries (8.42% daily quota); counters Bias #2"},
            {"source": "reddit", "label": "Reddit (Indian City & Household Subs)", "pulled": 480, "stage1_kept": 700, "clean": sum(1 for r in clean_rows if r["source"] == "reddit"), "date_range": date_cov.get("reddit", ""), "notes": "Open-source Agent Reach ($0.00); 338 threads, 0 username leaks; counters Bias #2"},
            {"source": "trustpilot", "label": "Trustpilot (Review Forum)", "pulled": 0, "stage1_kept": 0, "clean": 0, "date_range": "—", "notes": "Blocked by HTTP 403 anti-bot gate; stopped without bypassing"},
            {"source": "uc_filings", "label": "UC Shareholder Letters & Transcripts", "pulled": 8, "stage1_kept": 401, "clean": 401, "date_range": "Q3 FY26–Q2 FY27", "notes": "8 PDFs (243 pages) kept separate from customer review counts"},
        ],
        "models_used": {
            "stage3_filter": filt_by_model,
            "stage4_tagger": tag_by_model,
            "filings_synthesis": {"openai/gpt-oss-120b": 4},
            "embeddings": EMBED_MODEL_NAME,
        },
        "quote_verification": quote_stats,
        "inter_model_check": inter_model,
        "golden_set": golden_set,
        "human_corrections": corrections[:50],
    }

    (EXPORT_DIR / "summary_stats.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps({"status": "ok", "export_dir": str(EXPORT_DIR), "counts": summary["counts"], "tag_by_model": tag_by_model}, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build public-safe data/export/ for Streamlit dashboard")
    parser.add_argument("--wait-for-ai", type=int, default=0, help="Optional seconds to wait before exporting")
    parser.add_argument("--skip-embeddings", action="store_true", help="Skip fastembed vector rebuild")
    args = parser.parse_args()
    if args.wait_for_ai > 0:
        time.sleep(args.wait_for_ai)
    run_export(skip_embeddings=args.skip_embeddings)


if __name__ == "__main__":
    main()
