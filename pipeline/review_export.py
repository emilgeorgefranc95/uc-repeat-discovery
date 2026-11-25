"""Stage 8 Review Workbook Exporter: builds data/review/review_workbook.xlsx (git-ignored).

Creates a formatted Excel workbook for human review and tag auditing with tabs:
1. Summary: per-source funnel counts (pulled, cleaned, quick-filtered, tagged, relevant) + instructions.
2. All tagged posts: 26 plain-English columns + highlighted REVIEW columns with codebook.yaml dropdowns,
   sorted by source, severity (high -> low), and year (newest -> oldest).
3. Not yet tagged: any Stage 3 yes/unsure posts not yet tagged in Stage 4.
4. Dropped: all Stage 2 dropped posts with their drop reason.
5. Codebook: allowed values loaded directly from codebook.yaml (powers Excel Data Validation dropdowns).

Usage:
    python -m pipeline.review_export
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openpyxl
import yaml
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from collect.utils import ROOT_DIR
from pipeline.export import (
    apply_audit_rule_fixes,
    apply_human_corrections,
    load_human_corrections,
    load_jsonl,
    load_merged_tagged,
)

PROCESSED_DIR = ROOT_DIR / "data/processed"
EXPORT_DIR = ROOT_DIR / "data/export"
REVIEW_DIR = ROOT_DIR / "data/review"
CODEBOOK_PATH = ROOT_DIR / "codebook.yaml"
DEFAULT_OUT_PATH = REVIEW_DIR / "review_workbook.xlsx"

# Plain-English label mappings (shared with pipeline/import_review.py)
VALUE_LABELS: dict[str, dict[str, str]] = {
    "is_relevant": {
        "yes": "Yes",
        "no": "No",
        "unsure": "Unsure",
    },
    "service_category": {
        "salon_grooming": "Salon & Grooming",
        "home_cleaning": "Home Cleaning",
        "ac_appliance_repair": "AC & Appliance Repair",
        "plumbing_electrical_carpentry": "Plumbing, Electrical & Carpentry",
        "pest_control_painting": "Pest Control & Painting",
        "multiple_categories": "Multiple Categories",
        "instahelp_excluded": "InstaHelp (Excluded)",
        "general_home_services": "General Home Services",
        "unclear": "Unclear",
    },
    "booking_outcome": {
        "completed_fine": "Completed smoothly",
        "completed_with_friction": "Completed with friction",
        "service_failed_or_cancelled": "Service failed / cancelled",
        "never_booked_uc": "Never booked UC",
        "unclear": "Unclear",
    },
    "repeat_behavior": {
        "active_repeater": "Active repeater",
        "stopped_despite_fine_experience": "Stopped despite fine experience",
        "stopped_after_bad_experience": "Stopped after bad experience",
        "single_category_or_rare_user": "Single-category / rare user",
        "switched_off_platform": "Switched off-platform",
        "considering_or_asking": "Considering / asking",
        "unclear": "Unclear",
    },
    "scope_controllability": {
        "customer_side_product": "Customer-side product (In scope)",
        "supply_partner_execution": "Supply / partner execution (Out of scope)",
        "pricing_or_discount_only": "Pricing / discount only (Out of scope)",
        "mixed": "Mixed (Product + Supply/Price)",
        "unclear": "Unclear",
    },
    "repeat_barriers_or_drivers": {
        "single_category_mental_model": "Single-category mental model",
        "low_perceived_frequency_or_no_trigger": "Low perceived frequency / no trigger",
        "prefers_local_provider_or_maid": "Prefers local provider or maid",
        "hired_uc_partner_directly_offline": "Hired UC partner directly offline",
        "society_whatsapp_or_neighbour_referral": "Society WhatsApp / neighbour referral",
        "cannot_rebook_same_trusted_pro": "Cannot rebook same trusted pro",
        "unclear_scope_or_surprise_addons": "Unclear scope or surprise add-ons",
        "booking_or_slot_scheduling_friction": "Booking / slot / app UX friction",
        "no_post_service_followup_or_care": "No post-service follow-up / care",
        "trust_safety_or_home_privacy": "Trust / safety / home privacy",
        "perceived_high_price_vs_local": "Perceived high price vs. local",
        "service_quality_or_partner_failure": "Service quality / partner failure",
        "convenience_standardization_driver": "Convenience & standardization (Driver)",
        "hygiene_and_professionalism_driver": "Hygiene & professionalism (Driver)",
        "unclear": "Unclear",
    },
    "external_alternative": {
        "local_technician_or_shop": "Local technician / shop",
        "domestic_help_or_maid": "Domestic help / regular maid",
        "society_whatsapp_or_neighbour": "Society WhatsApp / neighbour",
        "direct_uc_partner_offline": "Direct UC partner offline",
        "family_or_diy": "Family or DIY",
        "competitor_app": "Competitor app",
        "none_uses_uc": "None (uses UC)",
        "unclear": "Unclear",
    },
    "need_trigger": {
        "emergency_breakdown": "Emergency / breakdown",
        "seasonal_or_festival": "Seasonal / festival",
        "routine_maintenance": "Routine maintenance",
        "special_occasion_or_guests": "Special occasion / guests",
        "moving_or_renovation": "Moving / renovation",
        "unclear": "Unclear",
    },
    "emotion": {
        "delighted": "Delighted",
        "satisfied": "Satisfied",
        "neutral_pragmatic": "Neutral / pragmatic",
        "confused_or_hesitant": "Confused / hesitant",
        "frustrated": "Frustrated",
        "distrustful": "Distrustful",
        "unclear": "Unclear",
    },
    "severity": {
        "3_high": "3 - High",
        "2_medium": "2 - Medium",
        "1_low": "1 - Low",
    },
    "segment_proxy": {
        "bachelor_or_shared_flat": "Bachelor / shared flat",
        "working_couple_or_nuclear_family": "Working couple / nuclear family",
        "family_with_elders_or_kids": "Family with elders or kids",
        "tier1_metro": "Tier-1 metro",
        "tier2_city": "Tier-2 city",
        "first_time_user": "First-time user",
        "multi_booking_user": "Multi-booking user",
        "unclear": "Unclear",
    },
}

QUOTE_STATUS_LABELS: dict[str, str] = {
    "exact": "Yes (Exact)",
    "repaired": "Yes (Repaired)",
    "hidden_low_score": "No (Hidden <80)",
}

DROP_REASON_LABELS: dict[str, str] = {
    "platform_staff_reply": "Platform staff / brand reply",
    "exact_duplicate": "Exact duplicate text",
    "near_duplicate": "Near-duplicate (>=92% match)",
    "too_short_no_keyword": "Too short (<6 words, no UC keyword)",
    "instahelp_excluded": "InstaHelp (excluded by brief)",
    "spam_or_promo": "Spam / promotional link",
    "empty_text": "Empty text",
}

SEVERITY_SORT_RANK = {"3_high": 0, "2_medium": 1, "1_low": 2}
SOURCE_LABELS = {
    "play_store": "Google Play Store",
    "app_store": "Apple App Store",
    "reddit": "Reddit",
    "youtube": "YouTube",
    "trustpilot": "Trustpilot (403 blocked)",
}

# Excel cell styling (clean, high-contrast, colour-blind-safe)
HEADER_FILL = PatternFill("solid", fgColor="1E3A5F")
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
REVIEW_HEADER_FILL = PatternFill("solid", fgColor="B45309")
REVIEW_CELL_FILL = PatternFill("solid", fgColor="FEF9C3")
SUBHEADER_FILL = PatternFill("solid", fgColor="E2E8F0")
BOLD_FONT = Font(name="Calibri", size=11, bold=True, color="0F172A")
TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="0F172A")
BODY_FONT = Font(name="Calibri", size=10, color="0F172A")
THIN_BORDER = Border(
    left=Side(style="thin", color="CBD5E1"),
    right=Side(style="thin", color="CBD5E1"),
    top=Side(style="thin", color="CBD5E1"),
    bottom=Side(style="thin", color="CBD5E1"),
)
WRAP_TOP = Alignment(vertical="top", wrap_text=True)
CENTER_TOP = Alignment(horizontal="center", vertical="top", wrap_text=True)


def sanitize_excel_str(val: Any) -> str:
    """Strip illegal XML control characters that openpyxl rejects."""
    if val is None:
        return ""
    s = str(val)
    return "".join(ch for ch in s if ch in ("\n", "\r", "\t") or ord(ch) >= 32)


def label_val(field: str, val: Any) -> str:
    if isinstance(val, list):
        return ", ".join(VALUE_LABELS.get(field, {}).get(str(v), str(v)) for v in val)
    return VALUE_LABELS.get(field, {}).get(str(val), str(val or "Unclear"))


def infer_first_time_customer(rec: dict[str, Any]) -> str:
    seg = rec.get("segment_proxy", "unclear")
    out = rec.get("booking_outcome", "unclear")
    rb = rec.get("repeat_behavior", "unclear")
    if seg == "first_time_user" or out == "never_booked_uc":
        return "Yes"
    if seg == "multi_booking_user" or rb == "active_repeater":
        return "No"
    return "Unclear"


def compute_needs_review(rec: dict[str, Any]) -> str:
    reasons = []
    if rec.get("quote_status") == "hidden_low_score":
        reasons.append("quote<80")
    elif rec.get("quote_status") == "repaired":
        reasons.append("quote_repaired")
    if rec.get("grounding_flags"):
        reasons.append("grounding_flag")
    if rec.get("rule_fixed"):
        reasons.append("rule_fixed")
    if rec.get("retried"):
        reasons.append("retried")
    if rec.get("filter_relevant") == "unsure":
        reasons.append("filter_unsure")
    if rec.get("human_corrected"):
        reasons.append("human_corrected")
    return f"Yes ({', '.join(reasons)})" if reasons else "No"


def load_codebook() -> dict[str, Any]:
    return yaml.safe_load(CODEBOOK_PATH.read_text(encoding="utf-8"))


def build_workbook(out_path: Path = DEFAULT_OUT_PATH) -> dict[str, Any]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    codebook = load_codebook()
    cb_fields: dict[str, Any] = codebook.get("fields", {})

    clean_rows = load_jsonl(PROCESSED_DIR / "clean.jsonl")
    clean_map = {r["id"]: r for r in clean_rows}
    filtered_rows = load_jsonl(PROCESSED_DIR / "filtered.jsonl")
    dropped_rows = load_jsonl(PROCESSED_DIR / "dropped.jsonl")
    tagged_raw = load_merged_tagged()

    # Apply the exact same rule fixes + human overrides used in pipeline/export.py
    tagged_audited, rule_fixes = apply_audit_rule_fixes(tagged_raw)
    corrections = load_human_corrections()
    tagged_all, corrections_applied = apply_human_corrections(tagged_audited, corrections)

    ok_tagged = [r for r in tagged_all if r.get("tag_status") == "ok"]
    tagged_ids = {r["id"] for r in ok_tagged}

    # Not yet tagged: Stage 3 yes/unsure rows not present in ok_tagged
    not_yet_tagged = [
        r for r in filtered_rows if r.get("relevant") in {"yes", "unsure"} and r["id"] not in tagged_ids
    ]

    # Stage 1 pulled counts from summary_stats.json (fallback to defaults if absent)
    pulled_by_src = {"play_store": 30000, "app_store": 500, "youtube": 1789, "reddit": 480, "trustpilot": 0}
    summary_json = EXPORT_DIR / "summary_stats.json"
    if summary_json.exists():
        sj = json.loads(summary_json.read_text(encoding="utf-8"))
        for row in sj.get("sources_stage1", []):
            if row["source"] != "uc_filings":
                pulled_by_src[row["source"]] = int(row.get("pulled", 0))

    wb = openpyxl.Workbook()

    # ==========================================================================
    # TAB 5 (created early so DataValidation formulas can reference it): Codebook
    # ==========================================================================
    ws_cb = wb.create_sheet("Codebook")
    ws_cb.append(["codebook_field", "allowed_values_raw", "allowed_values_plain_english"])
    for col_idx in range(1, 4):
        cell = ws_cb.cell(1, col_idx)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = WRAP_TOP

    editable_fields = [f for f in cb_fields.keys() if f != "evidence_quote"]
    for row_i, fname in enumerate(editable_fields, start=2):
        vals = cb_fields[fname].get("values") or []
        raw_str = ", ".join(vals) if vals else "(free text <= 12 words)"
        plain_str = ", ".join(label_val(fname, v) for v in vals) if vals else "(free text <= 12 words)"
        ws_cb.append([fname, raw_str, plain_str])
        for c in range(1, 4):
            ws_cb.cell(row_i, c).font = BODY_FONT
            ws_cb.cell(row_i, c).alignment = WRAP_TOP
            ws_cb.cell(row_i, c).border = THIN_BORDER

    # Store compact Y/N list in column E of Codebook tab for clean Excel list validation
    ws_cb.cell(1, 5, "yes_no_list").font = HEADER_FONT
    ws_cb.cell(1, 5).fill = HEADER_FILL
    ws_cb.cell(2, 5, "Y").font = BODY_FONT
    ws_cb.cell(3, 5, "N").font = BODY_FONT
    ws_cb.column_dimensions["A"].width = 30
    ws_cb.column_dimensions["B"].width = 65
    ws_cb.column_dimensions["C"].width = 65
    ws_cb.column_dimensions["E"].width = 14
    ws_cb.freeze_panes = "A2"

    # ==========================================================================
    # TAB 1: Summary
    # ==========================================================================
    ws_sum = wb.active
    ws_sum.title = "Summary"

    ws_sum["A1"] = "Urban Company 90-Day Repeat Discovery Engine — Human Review Workbook"
    ws_sum["A1"].font = TITLE_FONT
    ws_sum["A2"] = f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | Active overrides in corrections.csv: {corrections_applied} | Rule fixes applied: {sum(rule_fixes.values())}"
    ws_sum["A2"].font = Font(name="Calibri", size=10, italic=True, color="475569")

    sum_headers = ["Source", "Pulled (Stage 1)", "Cleaned (Stage 2)", "Quick-Filtered Yes/Unsure (Stage 3)", "Tagged Valid (Stage 4)", "Relevant Core (Excl. InstaHelp)"]
    header_row = 4
    for col_idx, h in enumerate(sum_headers, start=1):
        c = ws_sum.cell(header_row, col_idx, h)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER_TOP
        c.border = THIN_BORDER

    clean_by_src = Counter(r.get("source", "unknown") for r in clean_rows)
    filt_by_src = Counter(r.get("source", "unknown") for r in filtered_rows if r.get("relevant") in {"yes", "unsure"})
    tag_by_src = Counter(r.get("source", "unknown") for r in ok_tagged)
    rel_by_src = Counter(
        r.get("source", "unknown")
        for r in ok_tagged
        if r.get("is_relevant") == "yes" and r.get("service_category") != "instahelp_excluded"
    )

    sources_order = ["play_store", "app_store", "reddit", "youtube", "trustpilot"]
    tot_pulled = tot_clean = tot_filt = tot_tag = tot_rel = 0
    r_idx = header_row + 1
    for src in sources_order:
        p_n = pulled_by_src.get(src, 0)
        c_n = clean_by_src.get(src, 0)
        f_n = filt_by_src.get(src, 0)
        t_n = tag_by_src.get(src, 0)
        r_n = rel_by_src.get(src, 0)
        tot_pulled += p_n
        tot_clean += c_n
        tot_filt += f_n
        tot_tag += t_n
        tot_rel += r_n
        vals = [SOURCE_LABELS.get(src, src), p_n, c_n, f_n, t_n, r_n]
        for col_idx, v in enumerate(vals, start=1):
            cell = ws_sum.cell(r_idx, col_idx, v)
            cell.font = BODY_FONT
            cell.border = THIN_BORDER
            if col_idx > 1:
                cell.number_format = "#,##0"
                cell.alignment = Alignment(horizontal="right", vertical="center")
        r_idx += 1

    # Totals row
    tot_vals = ["TOTAL", tot_pulled, tot_clean, tot_filt, tot_tag, tot_rel]
    for col_idx, v in enumerate(tot_vals, start=1):
        cell = ws_sum.cell(r_idx, col_idx, v)
        cell.font = BOLD_FONT
        cell.fill = SUBHEADER_FILL
        cell.border = THIN_BORDER
        if col_idx > 1:
            cell.number_format = "#,##0"
            cell.alignment = Alignment(horizontal="right", vertical="center")

    # Instructions block
    inst_start = r_idx + 2
    ws_sum.cell(inst_start, 1, "How to Use This Review Workbook").font = Font(name="Calibri", size=12, bold=True, color="0F172A")
    instructions = [
        "1. Open the 'All tagged posts' tab. Start with rows where 'needs review' begins with 'Yes' (filter column V) or review a stratified sample across all 4 sources.",
        "2. Read the FULL 'post text' and 'context' (never judge solely from the short quote).",
        "3. In the highlighted yellow REVIEW columns (W–Z):",
        "   • REVIEW: correct? (Y/N): pick 'Y' if the AI tags look right, or 'N' if a field should be corrected.",
        "   • REVIEW: field to fix: choose the codebook field name from the dropdown (see 'Codebook' tab for every allowed value).",
        "   • REVIEW: correct value: enter the corrected value (either exact codebook code or plain-English label; comma-separate multi-select values for repeat_barriers_or_drivers or brief_questions).",
        "   • REVIEW: note: write a brief 1-sentence reason explaining why the AI tag was wrong (this becomes the audit reason in data/manual/corrections.csv).",
        "4. Save the workbook, then import your corrections into the pipeline and rebuild the public dashboard export:",
        "   python -m pipeline.import_review",
        "   python -m pipeline.export",
        "5. Commit data/manual/corrections.csv and data/export/ and push to GitHub to update https://uc-repeat-discovery.streamlit.app/ automatically.",
        "Note: data/review/ is git-ignored so this workbook stays strictly local on your laptop.",
    ]
    for offset, line in enumerate(instructions, start=1):
        c = ws_sum.cell(inst_start + offset, 1, line)
        c.font = Font(name="Calibri", size=10, bold=line.startswith(("1.", "2.", "3.", "4.", "5.")), color="0F172A")
        ws_sum.merge_cells(start_row=inst_start + offset, start_column=1, end_row=inst_start + offset, end_column=6)
        c.alignment = WRAP_TOP

    for col_idx, w in enumerate([32, 18, 18, 28, 22, 26], start=1):
        ws_sum.column_dimensions[get_column_letter(col_idx)].width = w

    # ==========================================================================
    # TAB 2: All tagged posts
    # ==========================================================================
    ws_all = wb.create_sheet("All tagged posts")
    headers_all = [
        "id",
        "source",
        "year",
        "link",
        "post text",
        "context",
        "relevant",
        "anchor category",
        "journey stage",
        "repeat action",
        "non-repeat barrier",
        "stated triggers",
        "deeper reason",
        "unmet need",
        "emotion",
        "severity",
        "first-time customer",
        "segment signals",
        "evidence quote",
        "quote verified?",
        "tagged by",
        "needs review",
        "REVIEW: correct? (Y/N)",
        "REVIEW: field to fix",
        "REVIEW: correct value",
        "REVIEW: note",
    ]
    ws_all.append(headers_all)
    for col_idx in range(1, len(headers_all) + 1):
        cell = ws_all.cell(1, col_idx)
        cell.font = HEADER_FONT
        cell.fill = REVIEW_HEADER_FILL if col_idx >= 23 else HEADER_FILL
        cell.alignment = CENTER_TOP
        cell.border = THIN_BORDER
    ws_all.row_dimensions[1].height = 30

    # Sort by source (asc), severity (3_high -> 1_low), year (desc)
    ok_tagged_sorted = sorted(
        ok_tagged,
        key=lambda r: (
            str(r.get("source") or "zzz"),
            SEVERITY_SORT_RANK.get(str(r.get("severity") or "2_medium"), 1),
            -(int(r["year"]) if r.get("year") is not None else 0),
            str(r.get("id") or ""),
        ),
    )

    for row_i, r in enumerate(ok_tagged_sorted, start=2):
        src_rec = clean_map.get(r["id"], {})
        is_insta = r.get("service_category") == "instahelp_excluded"
        rel_lbl = "No (InstaHelp excluded)" if is_insta else label_val("is_relevant", r.get("is_relevant"))
        scope_lbl = label_val("scope_controllability", r.get("scope_controllability"))
        alt_raw = r.get("external_alternative", "unclear")
        deeper_reason = (
            f"{scope_lbl} | Alt: {label_val('external_alternative', alt_raw)}"
            if alt_raw not in {"unclear", "none_uses_uc", ""}
            else scope_lbl
        )

        row_vals = [
            sanitize_excel_str(r.get("id")),
            SOURCE_LABELS.get(str(r.get("source")), str(r.get("source"))),
            int(r["year"]) if r.get("year") is not None else "",
            sanitize_excel_str(src_rec.get("url") or ""),
            sanitize_excel_str(src_rec.get("text") or ""),
            sanitize_excel_str(src_rec.get("context") or ""),
            rel_lbl,
            label_val("service_category", r.get("service_category")),
            label_val("booking_outcome", r.get("booking_outcome")),
            label_val("repeat_behavior", r.get("repeat_behavior")),
            label_val("repeat_barriers_or_drivers", r.get("repeat_barriers_or_drivers") or []),
            label_val("need_trigger", r.get("need_trigger")),
            deeper_reason,
            sanitize_excel_str(r.get("unmet_need") or "Unclear"),
            label_val("emotion", r.get("emotion")),
            label_val("severity", r.get("severity")),
            infer_first_time_customer(r),
            label_val("segment_proxy", r.get("segment_proxy")),
            sanitize_excel_str(r.get("evidence_quote") or ""),
            QUOTE_STATUS_LABELS.get(str(r.get("quote_status")), str(r.get("quote_status"))),
            sanitize_excel_str(r.get("tagged_by") or ""),
            compute_needs_review(r),
            "",  # REVIEW: correct? (Y/N)
            "",  # REVIEW: field to fix
            "",  # REVIEW: correct value
            "",  # REVIEW: note
        ]
        ws_all.append(row_vals)
        # Highlight review cells (columns W–Z = 23..26) and wrap long text columns
        for c_idx in range(1, 27):
            cell = ws_all.cell(row_i, c_idx)
            cell.font = BODY_FONT
            cell.alignment = CENTER_TOP if c_idx in (3, 7, 16, 17, 20, 23) else WRAP_TOP
            if c_idx >= 23:
                cell.fill = REVIEW_CELL_FILL
                cell.border = THIN_BORDER

    last_row_all = max(2, len(ok_tagged_sorted) + 1)
    ws_all.freeze_panes = "A2"
    ws_all.auto_filter.ref = f"A1:Z{last_row_all}"

    # Dropdown DataValidations from Codebook tab (immune to 255-char Excel limit)
    dv_yn = DataValidation(type="list", formula1="=Codebook!$E$2:$E$3", allow_blank=True)
    dv_yn.promptTitle = "Correct?"
    dv_yn.prompt = "Select Y (AI tags look right) or N (needs correction)."
    ws_all.add_data_validation(dv_yn)
    dv_yn.add(f"W2:W{last_row_all}")

    dv_field = DataValidation(
        type="list",
        formula1=f"=Codebook!$A$2:$A${len(editable_fields) + 1}",
        allow_blank=True,
    )
    dv_field.promptTitle = "Field to fix"
    dv_field.prompt = "Pick the codebook.yaml field to correct (see Codebook tab for allowed values)."
    ws_all.add_data_validation(dv_field)
    dv_field.add(f"X2:X{last_row_all}")

    col_widths_all = [
        24, 16, 8, 22, 52, 32, 16, 22, 22, 24, 34, 20, 32, 28, 16, 12, 16, 22, 42, 16, 20, 22, 18, 24, 28, 36
    ]
    for col_idx, w in enumerate(col_widths_all, start=1):
        ws_all.column_dimensions[get_column_letter(col_idx)].width = w

    # ==========================================================================
    # TAB 3: Not yet tagged
    # ==========================================================================
    ws_nyt = wb.create_sheet("Not yet tagged")
    headers_nyt = ["id", "source", "year", "rating", "filter_relevant", "filter_reason", "post text", "context", "link"]
    ws_nyt.append(headers_nyt)
    for col_idx in range(1, len(headers_nyt) + 1):
        c = ws_nyt.cell(1, col_idx)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER_TOP
        c.border = THIN_BORDER

    if not not_yet_tagged:
        ws_nyt.append(["—", "All 5,110 Stage 3 (yes/unsure) posts have been tagged in Stage 4 (0 remaining).", "", "", "", "", "", "", ""])
        ws_nyt.cell(2, 2).font = Font(name="Calibri", size=10, italic=True, color="047857")
    else:
        for row_i, r in enumerate(not_yet_tagged, start=2):
            src_rec = clean_map.get(r["id"], {})
            ws_nyt.append(
                [
                    sanitize_excel_str(r["id"]),
                    SOURCE_LABELS.get(str(r.get("source")), str(r.get("source"))),
                    r.get("year") or "",
                    r.get("rating") or "",
                    str(r.get("relevant") or ""),
                    sanitize_excel_str(r.get("reason") or ""),
                    sanitize_excel_str(src_rec.get("text") or ""),
                    sanitize_excel_str(src_rec.get("context") or ""),
                    sanitize_excel_str(src_rec.get("url") or ""),
                ]
            )
            for c_idx in range(1, len(headers_nyt) + 1):
                ws_nyt.cell(row_i, c_idx).font = BODY_FONT
                ws_nyt.cell(row_i, c_idx).alignment = WRAP_TOP

    ws_nyt.freeze_panes = "A2"
    ws_nyt.auto_filter.ref = f"A1:I{max(2, len(not_yet_tagged) + 1)}"
    for col_idx, w in enumerate([24, 18, 8, 8, 16, 30, 52, 32, 24], start=1):
        ws_nyt.column_dimensions[get_column_letter(col_idx)].width = w

    # ==========================================================================
    # TAB 4: Dropped
    # ==========================================================================
    ws_drop = wb.create_sheet("Dropped")
    headers_drop = ["id", "source", "rating", "drop reason", "post text", "context", "link"]
    ws_drop.append(headers_drop)
    for col_idx in range(1, len(headers_drop) + 1):
        c = ws_drop.cell(1, col_idx)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER_TOP
        c.border = THIN_BORDER

    for row_i, r in enumerate(dropped_rows, start=2):
        raw_reason = str(r.get("drop_reason") or "unknown")
        ws_drop.append(
            [
                sanitize_excel_str(r.get("id")),
                SOURCE_LABELS.get(str(r.get("source")), str(r.get("source"))),
                r.get("rating") if r.get("rating") is not None else "",
                DROP_REASON_LABELS.get(raw_reason, raw_reason),
                sanitize_excel_str(r.get("text") or ""),
                sanitize_excel_str(r.get("context") or ""),
                sanitize_excel_str(r.get("url") or ""),
            ]
        )
        for c_idx in range(1, len(headers_drop) + 1):
            ws_drop.cell(row_i, c_idx).font = BODY_FONT
            ws_drop.cell(row_i, c_idx).alignment = WRAP_TOP
            ws_drop.cell(row_i, c_idx).border = THIN_BORDER

    ws_drop.freeze_panes = "A2"
    ws_drop.auto_filter.ref = f"A1:G{max(2, len(dropped_rows) + 1)}"
    for col_idx, w in enumerate([26, 18, 10, 30, 54, 34, 26], start=1):
        ws_drop.column_dimensions[get_column_letter(col_idx)].width = w

    # Reorder sheets cleanly: Summary, All tagged posts, Not yet tagged, Dropped, Codebook
    wb._sheets = [ws_sum, ws_all, ws_nyt, ws_drop, ws_cb]
    wb.save(out_path)

    return {
        "status": "ok",
        "output_path": str(out_path),
        "tabs": [ws.title for ws in wb.worksheets],
        "counts": {
            "all_tagged_posts": len(ok_tagged_sorted),
            "needs_review_yes": sum(1 for r in ok_tagged_sorted if compute_needs_review(r).startswith("Yes")),
            "not_yet_tagged": len(not_yet_tagged),
            "dropped": len(dropped_rows),
            "codebook_fields_in_dropdown": len(editable_fields),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build human review Excel workbook from processed pipeline data")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH, help="Output .xlsx path")
    args = parser.parse_args()
    result = build_workbook(out_path=args.out)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
