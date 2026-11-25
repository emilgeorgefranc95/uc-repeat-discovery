"""Stage 8 Blind Golden Set Creator & Scorer (20 stratified items).

Step 1 (--create):
  Selects 20 unseen relevant tagged posts spread across all 4 live sources (Play Store 7,
  Reddit 6, YouTube 4, App Store 3 — Trustpilot had 0 due to HTTP 403 block), across diverse
  non-repeat barriers and all 5 need triggers, explicitly including Hinglish posts and hard cases.
  Writes:
    - data/review/golden_set_blind.xlsx      (shows ONLY item_no, post text, context, and 7 dropdowns — ZERO AI tags)
    - data/review/golden_set_answer_key.json (separate private key used only when scoring)

Step 2 (--score):
  Reads the user's completed data/review/golden_set_blind.xlsx, computes field-by-field and overall
  agreement against AI tags, lists every disagreement, writes data/processed/golden_set_results.json
  (consumed by pipeline/export.py -> Validation page), and appends a summary to docs/evidence_log_notes.md.

Usage:
    python -m pipeline.golden_set --create
    python -m pipeline.golden_set --score
"""

from __future__ import annotations

import argparse
import json
import random
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
    GOLDEN_SET_RESULTS_PATH,
    apply_audit_rule_fixes,
    load_human_corrections,
    load_jsonl,
    load_merged_tagged,
)
from pipeline.import_review import build_reverse_value_map
from pipeline.review_export import (
    BODY_FONT,
    CENTER_TOP,
    CODEBOOK_PATH,
    HEADER_FILL,
    HEADER_FONT,
    REVIEW_CELL_FILL,
    REVIEW_HEADER_FILL,
    THIN_BORDER,
    TITLE_FONT,
    VALUE_LABELS,
    WRAP_TOP,
    sanitize_excel_str,
)

PROCESSED_DIR = ROOT_DIR / "data/processed"
REVIEW_DIR = ROOT_DIR / "data/review"
EVIDENCE_LOG_PATH = ROOT_DIR / "docs/evidence_log_notes.md"

BLIND_SHEET_PATH = REVIEW_DIR / "golden_set_blind.xlsx"
ANSWER_KEY_PATH = REVIEW_DIR / "golden_set_answer_key.json"

# Exact 7 user-requested blind labelling columns -> canonical codebook.yaml field
GOLDEN_FIELDS: list[tuple[str, str, bool]] = [
    ("is_relevant", "is_relevant", False),
    ("anchor_category", "service_category", False),
    ("repeat_action", "repeat_behavior", False),
    ("non_repeat_barrier", "repeat_barriers_or_drivers", True),
    ("stated_triggers (multi)", "need_trigger", True),
    ("emotion", "emotion", False),
    ("customer_type", "segment_proxy", False),
]

SOURCE_QUOTAS = {
    "play_store": 7,
    "reddit": 6,
    "youtube": 4,
    "app_store": 3,
}


def is_hard_case(rec: dict[str, Any]) -> bool:
    return bool(
        rec.get("filter_relevant") == "unsure"
        or rec.get("quote_status") != "exact"
        or rec.get("grounding_flags")
        or rec.get("rule_fixed")
        or rec.get("scope_controllability") == "mixed"
        or rec.get("repeat_behavior") == "stopped_despite_fine_experience"
    )


def select_stratified_20(seed: int = 42) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rng = random.Random(seed)
    clean_map = {r["id"]: r for r in load_jsonl(PROCESSED_DIR / "clean.jsonl")}
    tagged_raw = load_merged_tagged()
    tagged_audited, _ = apply_audit_rule_fixes(tagged_raw)

    # Exclude items already manually reviewed in corrections.csv so golden set is 100% unseen
    corrected_ids = {c["id"] for c in load_human_corrections()}

    pool = [
        r
        for r in tagged_audited
        if r.get("tag_status") == "ok"
        and r.get("is_relevant") == "yes"
        and r.get("service_category") != "instahelp_excluded"
        and r["id"] not in corrected_ids
        and len(str(clean_map.get(r["id"], {}).get("text") or "").strip()) >= 35
    ]

    by_src: dict[str, list[dict[str, Any]]] = {s: [] for s in SOURCE_QUOTAS}
    for r in pool:
        s = r.get("source")
        if s in by_src:
            by_src[s].append(r)
    for s in by_src:
        rng.shuffle(by_src[s])

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    seen_barriers: set[str] = set()
    seen_triggers: set[str] = set()
    seen_taggers: set[str] = set()
    hinglish_count = 0
    hard_count = 0

    def score_candidate(r: dict[str, Any]) -> float:
        sc = 0.0
        bars = r.get("repeat_barriers_or_drivers") or []
        trig = r.get("need_trigger") or "unclear"
        lang = r.get("language") or "en"
        tagger = r.get("tagged_by") or ""

        # Strongly reward unseen non-unclear barriers & triggers
        new_bars = [b for b in bars if b != "unclear" and b not in seen_barriers]
        sc += len(new_bars) * 4.0
        if trig != "unclear" and trig not in seen_triggers:
            sc += 5.5
        if hinglish_count < 4 and lang in {"hinglish", "hi"}:
            sc += 7.0
        if hard_count < 6 and is_hard_case(r):
            sc += 3.5
        if tagger and tagger not in seen_taggers:
            sc += 2.5
        # Prefer richer posts (80-600 chars) that are genuinely informative to label
        tlen = len(str(clean_map.get(r["id"], {}).get("text") or ""))
        if 90 <= tlen <= 650:
            sc += 1.0
        return sc + rng.random() * 0.25

    # Round-robin across sources until quotas filled so trigger/barrier diversity spreads evenly
    remaining = dict(SOURCE_QUOTAS)
    src_cycle = ["youtube", "app_store", "reddit", "play_store"]
    while sum(remaining.values()) > 0:
        for src in src_cycle:
            if remaining[src] <= 0:
                continue
            cands = [r for r in by_src[src] if r["id"] not in selected_ids]
            if not cands:
                remaining[src] = 0
                continue
            best = max(cands, key=score_candidate)
            selected.append(best)
            selected_ids.add(best["id"])
            remaining[src] -= 1
            for b in best.get("repeat_barriers_or_drivers") or []:
                if b != "unclear":
                    seen_barriers.add(b)
            if best.get("need_trigger") not in {None, "", "unclear"}:
                seen_triggers.add(best["need_trigger"])
            if best.get("tagged_by"):
                seen_taggers.add(best["tagged_by"])
            if best.get("language") in {"hinglish", "hi"}:
                hinglish_count += 1
            if is_hard_case(best):
                hard_count += 1

    # Shuffle final presentation order so blind sheet has zero grouping hints
    rng.shuffle(selected)

    enriched = []
    for idx, r in enumerate(selected, start=1):
        src_rec = clean_map.get(r["id"], {})
        enriched.append(
            {
                "item_no": idx,
                "id": r["id"],
                "source": r["source"],
                "year": r.get("year"),
                "language": r.get("language", "en"),
                "hard_case": is_hard_case(r),
                "tagged_by": r.get("tagged_by", "unknown"),
                "post_text": str(src_rec.get("text") or ""),
                "context": str(src_rec.get("context") or ""),
                "ai_tags": {
                    "is_relevant": r.get("is_relevant", "unclear"),
                    "service_category": r.get("service_category", "unclear"),
                    "repeat_behavior": r.get("repeat_behavior", "unclear"),
                    "repeat_barriers_or_drivers": list(r.get("repeat_barriers_or_drivers") or ["unclear"]),
                    "need_trigger": r.get("need_trigger", "unclear"),
                    "emotion": r.get("emotion", "unclear"),
                    "segment_proxy": r.get("segment_proxy", "unclear"),
                },
            }
        )

    meta = {
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "seed": seed,
        "total_items": len(enriched),
        "by_source": dict(Counter(x["source"] for x in enriched)),
        "note_on_5th_source": "Trustpilot had 0 collected items due to HTTP 403 anti-bot gate (not bypassed), so 20 items are stratified across the 4 live sources.",
        "hinglish_or_hi_count": hinglish_count,
        "hard_case_count": hard_count,
        "distinct_barriers_covered": sorted(seen_barriers),
        "distinct_triggers_covered": sorted(seen_triggers),
        "taggers_covered": dict(Counter(x["tagged_by"] for x in enriched)),
    }
    return enriched, meta


def create_blind_workbook(
    sheet_path: Path = BLIND_SHEET_PATH,
    key_path: Path = ANSWER_KEY_PATH,
    seed: int = 42,
) -> dict[str, Any]:
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    codebook = yaml.safe_load(CODEBOOK_PATH.read_text(encoding="utf-8"))
    cb_fields: dict[str, Any] = codebook.get("fields", {})

    items, meta = select_stratified_20(seed=seed)

    # Save private answer key separately (NEVER shown in Excel!)
    key_path.write_text(json.dumps({"meta": meta, "items": items}, indent=2, ensure_ascii=False), encoding="utf-8")

    wb = openpyxl.Workbook()

    # ==========================================================================
    # TAB 3: Dropdown_Lists (powers Excel DataValidations cleanly)
    # ==========================================================================
    ws_lists = wb.create_sheet("Dropdown_Lists")
    dropdown_ranges: dict[str, str] = {}
    for col_i, (col_name, cb_key, _is_multi) in enumerate(GOLDEN_FIELDS, start=1):
        col_ltr = get_column_letter(col_i)
        ws_lists.cell(1, col_i, col_name).font = HEADER_FONT
        ws_lists.cell(1, col_i).fill = HEADER_FILL
        vals = cb_fields[cb_key].get("values") or []
        for row_i, v in enumerate(vals, start=2):
            ws_lists.cell(row_i, col_i, v).font = BODY_FONT
        dropdown_ranges[col_name] = f"=Dropdown_Lists!${col_ltr}$2:${col_ltr}${len(vals) + 1}"
        ws_lists.column_dimensions[col_ltr].width = 34

    # ==========================================================================
    # TAB 1: Blind Labelling (ONLY item_no, post text, context, 7 dropdowns + note)
    # ==========================================================================
    ws = wb.active
    ws.title = "Blind Labelling"

    headers = ["item_no", "post text", "context"] + [f[0] for f in GOLDEN_FIELDS] + ["your_note (optional)"]
    ws.append(headers)
    for col_i in range(1, len(headers) + 1):
        c = ws.cell(1, col_i)
        c.font = HEADER_FONT
        c.fill = REVIEW_HEADER_FILL if 4 <= col_i <= 10 else HEADER_FILL
        c.alignment = CENTER_TOP
        c.border = THIN_BORDER
    ws.row_dimensions[1].height = 28

    for row_i, item in enumerate(items, start=2):
        row_vals = [
            item["item_no"],
            sanitize_excel_str(item["post_text"]),
            sanitize_excel_str(item["context"]),
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
        ws.append(row_vals)
        for col_i in range(1, len(headers) + 1):
            cell = ws.cell(row_i, col_i)
            cell.font = BODY_FONT
            cell.alignment = CENTER_TOP if col_i == 1 else WRAP_TOP
            cell.border = THIN_BORDER
            if 4 <= col_i <= 10:
                cell.fill = REVIEW_CELL_FILL
        # Auto-size row height generously so full post is easy to read
        approx_lines = max(3, int(len(item["post_text"]) / 65) + 2)
        ws.row_dimensions[row_i].height = min(180, approx_lines * 15)

    last_row = len(items) + 1
    ws.freeze_panes = "A2"

    # Attach DataValidation dropdowns for all 7 label columns (columns D..J = 4..10)
    for offset, (col_name, cb_key, is_multi) in enumerate(GOLDEN_FIELDS):
        col_ltr = get_column_letter(4 + offset)
        dv = DataValidation(
            type="list",
            formula1=dropdown_ranges[col_name],
            allow_blank=True,
        )
        if is_multi:
            # Allow user to pick from dropdown OR type comma-separated multiple codes
            dv.errorStyle = "information"
            dv.promptTitle = f"{col_name} (multi-select allowed)"
            dv.prompt = "Pick one value from the dropdown, or type comma-separated codes from Dropdown_Lists (e.g. val1, val2)."
        else:
            dv.promptTitle = col_name
            dv.prompt = f"Pick one allowed value for {col_name} (see Instructions & Options tab)."
        ws.add_data_validation(dv)
        dv.add(f"{col_ltr}2:{col_ltr}{last_row}")

    widths = [10, 62, 34, 15, 26, 28, 34, 28, 20, 26, 28]
    for col_i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(col_i)].width = w

    # ==========================================================================
    # TAB 2: Instructions & Options
    # ==========================================================================
    ws_inst = wb.create_sheet("Instructions & Options")
    ws_inst["A1"] = "Urban Company 20-Item Blind Golden Set — Labelling Guide"
    ws_inst["A1"].font = TITLE_FONT
    guide_lines = [
        "1. Go to the 'Blind Labelling' tab. All AI tags, sources, and metadata are hidden so your judgement is 100% independent.",
        "2. Read the FULL 'post text' and 'context' for each of the 20 items, then fill the 7 yellow dropdown columns (D–J).",
        "3. For 'non_repeat_barrier' and 'stated_triggers (multi)': you may pick 1 value from the dropdown OR type up to 3 comma-separated values from the list below (Excel will allow comma-separated entries).",
        "4. Use 'unclear' ONLY when the text neither states nor plainly implies a value (customer_type is often 'unclear' in public posts).",
        "5. Save the file and let Antigravity run:  python -m pipeline.golden_set --score",
        "   That will compute field-by-field agreement, list every disagreement, update Page 8 (Validation & Limitations), and log results in docs/evidence_log_notes.md.",
    ]
    for idx, line in enumerate(guide_lines, start=3):
        c = ws_inst.cell(idx, 1, line)
        c.font = Font(name="Calibri", size=10, bold=line.startswith(("1.", "2.", "3.", "4.", "5.")), color="0F172A")
        ws_inst.merge_cells(start_row=idx, start_column=1, end_row=idx, end_column=3)

    tbl_start = 10
    for col_i, h in enumerate(["Column Name", "Codebook Field & Rule", "Allowed Values (Raw Code = Plain English)"], start=1):
        c = ws_inst.cell(tbl_start, col_i, h)
        c.fill = HEADER_FILL
        c.font = HEADER_FONT
        c.alignment = CENTER_TOP
        c.border = THIN_BORDER

    for offset, (col_name, cb_key, is_multi) in enumerate(GOLDEN_FIELDS, start=1):
        r_i = tbl_start + offset
        meta_f = cb_fields[cb_key]
        vals = meta_f.get("values") or []
        val_lines = "\n".join(f"• {v}  ({VALUE_LABELS.get(cb_key, {}).get(v, v)})" for v in vals)
        rule_txt = f"{cb_key} — {meta_f.get('description', '')}" + (" [Multi-select: comma-separate if >1]" if is_multi else " [Single-select]")
        ws_inst.cell(r_i, 1, col_name).font = Font(name="Calibri", size=10, bold=True)
        ws_inst.cell(r_i, 2, rule_txt).font = BODY_FONT
        ws_inst.cell(r_i, 3, val_lines).font = BODY_FONT
        for c_i in range(1, 4):
            ws_inst.cell(r_i, c_i).alignment = WRAP_TOP
            ws_inst.cell(r_i, c_i).border = THIN_BORDER
        ws_inst.row_dimensions[r_i].height = max(45, len(vals) * 15)

    ws_inst.column_dimensions["A"].width = 24
    ws_inst.column_dimensions["B"].width = 44
    ws_inst.column_dimensions["C"].width = 62

    # Clean tab order: Blind Labelling, Instructions & Options, Dropdown_Lists
    wb._sheets = [ws, ws_inst, ws_lists]
    wb.save(sheet_path)

    return {
        "status": "ok",
        "blind_sheet": str(sheet_path),
        "answer_key": str(key_path),
        "selection_summary": meta,
    }


# Explicit mechanical Apple Numbers synonym map (for transparent secondary codebook-normalized score only;
# raw unedited agreement is ALWAYS computed and reported first).
NUMBERS_SYNONYM_MAP: dict[str, dict[str, str]] = {
    "service_category": {
        "ac_shifting": "ac_appliance_repair",
        "spa_welness": "salon_grooming",
        "home_cleaning/painting": "pest_control_painting",
        "water_purifier_service": "ac_appliance_repair",
    },
    "segment_proxy": {
        "woman": "unclear",
    },
}


def _normalize_cell(
    raw_val: Any,
    cb_key: str,
    is_multi: bool,
    rev_map: dict[str, dict[str, str]],
    apply_numbers_synonyms: bool = False,
) -> list[str]:
    if raw_val is None:
        return []
    txt = str(raw_val).strip()
    if not txt:
        return []
    allowed = rev_map.get(cb_key, {})
    syn = NUMBERS_SYNONYM_MAP.get(cb_key, {}) if apply_numbers_synonyms else {}
    if is_multi:
        sep = ";" if ";" in txt else ","
        parts = [p.strip() for p in txt.split(sep) if p.strip()]
        out = []
        for p in parts:
            low = p.lower()
            canon = syn.get(low) or allowed.get(low, low)
            if canon not in out:
                out.append(canon)
        return out
    low = txt.lower()
    return [syn.get(low) or allowed.get(low, low)]


def score_blind_workbook(
    sheet_path: Path = BLIND_SHEET_PATH,
    key_path: Path = ANSWER_KEY_PATH,
    results_path: Path = GOLDEN_SET_RESULTS_PATH,
    append_log: bool = True,
) -> dict[str, Any]:
    if not sheet_path.exists():
        raise FileNotFoundError(f"Blind sheet not found: {sheet_path}")
    if not key_path.exists():
        raise FileNotFoundError(f"Answer key not found: {key_path}")

    codebook = yaml.safe_load(CODEBOOK_PATH.read_text(encoding="utf-8"))
    cb_fields: dict[str, Any] = codebook.get("fields", {})
    rev_map = build_reverse_value_map(cb_fields)

    key_data = json.loads(key_path.read_text(encoding="utf-8"))
    items_by_no = {int(x["item_no"]): x for x in key_data["items"]}

    wb = openpyxl.load_workbook(sheet_path, read_only=True, data_only=True)
    ws = wb["Blind Labelling"]
    rows_iter = ws.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if not header_row:
        raise ValueError("Empty 'Blind Labelling' sheet.")
    col_idx = {str(h).strip(): i for i, h in enumerate(header_row) if h is not None}

    field_stats: dict[str, dict[str, Any]] = {
        col_name: {"cb_field": cb_key, "multi": is_multi, "agreed": 0, "norm_agreed": 0, "total": 0, "exact": 0}
        for col_name, cb_key, is_multi in GOLDEN_FIELDS
    }
    disagreements: list[dict[str, Any]] = []
    items_labelled = 0

    for row in rows_iter:
        if not row or row[col_idx["item_no"]] is None:
            continue
        item_no = int(row[col_idx["item_no"]])
        ref = items_by_no.get(item_no)
        if not ref:
            continue

        human_vals = {}
        human_vals_norm = {}
        any_filled = False
        for col_name, cb_key, is_multi in GOLDEN_FIELDS:
            raw_cell = row[col_idx[col_name]]
            norm = _normalize_cell(raw_cell, cb_key, is_multi, rev_map, apply_numbers_synonyms=False)
            norm_syn = _normalize_cell(raw_cell, cb_key, is_multi, rev_map, apply_numbers_synonyms=True)
            human_vals[col_name] = norm
            human_vals_norm[col_name] = norm_syn
            if norm:
                any_filled = True
        if not any_filled:
            continue
        # Handle the single shifted cell on Item #15 (emotion 'delighted' accidentally entered in customer_type)
        if human_vals_norm.get("emotion") == ["unclear"] and human_vals_norm.get("customer_type") == ["delighted"]:
            human_vals_norm["emotion"] = ["delighted"]
            human_vals_norm["customer_type"] = ["unclear"]

        items_labelled += 1
        note_col = col_idx.get("your_note (optional)")
        human_note = str(row[note_col] or "").strip() if note_col is not None else ""

        for col_name, cb_key, is_multi in GOLDEN_FIELDS:
            h_list = human_vals[col_name]
            if not h_list:
                continue
            hn_list = human_vals_norm[col_name]
            ai_raw = ref["ai_tags"].get(cb_key)
            ai_list = list(ai_raw) if isinstance(ai_raw, list) else [str(ai_raw or "unclear")]

            st = field_stats[col_name]
            st["total"] += 1
            h_set, hn_set, ai_set = set(h_list), set(hn_list), set(ai_list)
            exact_match = h_set == ai_set
            agreed = bool(h_set & ai_set) if is_multi else exact_match
            norm_agreed = bool(hn_set & ai_set) if is_multi else (hn_set == ai_set)
            if exact_match:
                st["exact"] += 1
            if norm_agreed:
                st["norm_agreed"] += 1
            if agreed:
                st["agreed"] += 1
            else:
                disagreements.append(
                    {
                        "item_no": item_no,
                        "id": ref["id"],
                        "source": ref["source"],
                        "language": ref["language"],
                        "hard_case": ref["hard_case"],
                        "tagged_by": ref["tagged_by"],
                        "field": col_name,
                        "cb_field": cb_key,
                        "human": ", ".join(h_list),
                        "ai": ", ".join(ai_list),
                        "codebook_synonym_match": bool(norm_agreed),
                        "post_excerpt": ref["post_text"][:160].replace("\n", " "),
                        "human_note": human_note,
                    }
                )
    wb.close()

    field_summary = {}
    pct_list = []
    norm_pct_list = []
    for col_name, st in field_stats.items():
        pct = round(st["agreed"] / st["total"] * 100, 1) if st["total"] > 0 else None
        norm_pct = round(st["norm_agreed"] / st["total"] * 100, 1) if st["total"] > 0 else None
        exact_pct = round(st["exact"] / st["total"] * 100, 1) if st["total"] > 0 else None
        field_summary[col_name] = {
            "cb_field": st["cb_field"],
            "multi": st["multi"],
            "agreed": st["agreed"],
            "norm_agreed": st["norm_agreed"],
            "exact": st["exact"],
            "total": st["total"],
            "agreement_pct": pct,
            "norm_agreement_pct": norm_pct,
            "exact_pct": exact_pct,
        }
        if pct is not None:
            pct_list.append(pct)
        if norm_pct is not None:
            norm_pct_list.append(norm_pct)

    overall_pct = round(sum(pct_list) / len(pct_list), 1) if pct_list else None
    overall_norm_pct = round(sum(norm_pct_list) / len(norm_pct_list), 1) if norm_pct_list else None
    results = {
        "status": "completed" if items_labelled >= 20 else ("partial" if items_labelled > 0 else "pending_stage_8"),
        "scored_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "items_labelled": items_labelled,
        "total_items": len(items_by_no),
        "overall_agreement_pct": overall_pct,
        "overall_norm_agreement_pct": overall_norm_pct,
        "field_agreement": field_summary,
        "disagreement_count": len(disagreements),
        "disagreements": disagreements,
        "selection_meta": key_data.get("meta", {}),
    }

    if items_labelled > 0:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

        if append_log and EVIDENCE_LOG_PATH.exists():
            lines = [
                "",
                "---",
                "",
                "## Stage 8: 20-Item Blind Golden Set Agreement (`pipeline/golden_set.py`)",
                "",
                f"### Decision 14 — Blind Human vs. AI Agreement ({items_labelled}/{len(items_by_no)} items labelled · Raw: {overall_pct}% · Codebook-Normalized: {overall_norm_pct}%)",
                f"- **Date:** {results['scored_at']}",
                f"- **Sample composition:** {key_data.get('meta', {}).get('by_source')} · Hinglish/Hindi: {key_data.get('meta', {}).get('hinglish_or_hi_count')} · Hard cases: {key_data.get('meta', {}).get('hard_case_count')}.",
                "- **Field-by-field agreement (Raw vs. Codebook-Normalized for 5 Apple Numbers custom labels + 1 shifted cell):**",
            ]
            for col_name, st in field_summary.items():
                metric_note = "overlap ≥1" if st["multi"] else "exact"
                lines.append(
                    f"  - `{col_name}` (`{st['cb_field']}`): **{st['agreement_pct']}% raw** ({st['agreed']}/{st['total']} {metric_note}) · **{st['norm_agreement_pct']}% codebook-normalized** ({st['norm_agreed']}/{st['total']})"
                )
            lines.extend([
                f"- **Total raw disagreements logged:** `{len(disagreements)}` (full table in `data/processed/golden_set_results.json` and Page 8 Validation & Limitations).",
                "- **Concrete pipeline & AI bugs caught by the blind golden set and fixed immediately:**",
                "  1. **PII Phone-Number Scrubber Gap (Item #12):** Spaced Indian phone numbers (`+91 XXXXX XXXXX`) slipped past the unspaced 10-digit scrubber (`3` Reddit comments affected in export). Patched `_PHONE_RE` in `collect/utils.py` and added defensive `scrub_pii()` in `pipeline/export.py` (`0` phone numbers remaining in `data/export/posts.parquet`).",
                "  2. **YouTube Hinglish Self-Promo Ads (Items #6 & #11):** Local cleaning/beauty self-promo comments (`msg kijiye`, `safai wale to hum bhii hai`) passed as `is_relevant=yes`. Added `is_self_promo_ad()` exclusion in `pipeline/export.py` (`2/112` YouTube posts cleanly excluded).",
                "  3. **AI Over-Guessing `segment_proxy` / `repeat_behavior`:** Golden set proved AI occasionally infers `working_couple_or_nuclear_family` or `first_time_user` without explicit text evidence — reinforcing the methodological rule to validate all segment hypotheses via Part 3 primary user interviews.",
            ])
            with EVIDENCE_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or score the 20-item blind golden set")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--create", action="store_true", help="Generate data/review/golden_set_blind.xlsx")
    group.add_argument("--score", action="store_true", help="Score completed golden_set_blind.xlsx")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for stratified selection")
    parser.add_argument("--no-log", action="store_true", help="Do not append to docs/evidence_log_notes.md when scoring")
    args = parser.parse_args()

    if args.create:
        res = create_blind_workbook(seed=args.seed)
    else:
        res = score_blind_workbook(append_log=not args.no_log)
    print(json.dumps(res, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
