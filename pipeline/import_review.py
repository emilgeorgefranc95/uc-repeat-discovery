"""Stage 8 Human Review Importer: reads REVIEW edits from review_workbook.xlsx into data/manual/corrections.csv.

Scans the 'All tagged posts' tab for rows where:
  - 'REVIEW: correct? (Y/N)' == 'N', OR
  - Both 'REVIEW: field to fix' and 'REVIEW: correct value' are populated.

Normalizes plain-English field names and plain-English values back to canonical codebook.yaml
field names and codes, validates against codebook.yaml, looks up the previous value, uses
'REVIEW: note' as the reason, and merges cleanly (deduplicating on id+field) into
data/manual/corrections.csv.

Usage:
    python -m pipeline.import_review
    python -m pipeline.import_review --dry-run
    python -m pipeline.import_review --workbook data/review/review_workbook.xlsx
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

import openpyxl
import yaml

from collect.utils import ROOT_DIR
from pipeline.export import (
    MANUAL_CORRECTIONS_PATH,
    apply_audit_rule_fixes,
    load_merged_tagged,
)
from pipeline.review_export import CODEBOOK_PATH, DEFAULT_OUT_PATH, VALUE_LABELS

CSV_COLUMNS = ["id", "field", "old_value", "correct_value", "reason", "reviewer", "date"]

# Accept both canonical codebook.yaml field names and plain-English workbook column labels
FIELD_ALIASES: dict[str, str] = {
    "relevant": "is_relevant",
    "is_relevant": "is_relevant",
    "anchor category": "service_category",
    "service_category": "service_category",
    "journey stage": "booking_outcome",
    "booking_outcome": "booking_outcome",
    "repeat action": "repeat_behavior",
    "repeat_behavior": "repeat_behavior",
    "non-repeat barrier": "repeat_barriers_or_drivers",
    "repeat_barriers_or_drivers": "repeat_barriers_or_drivers",
    "stated triggers": "need_trigger",
    "need_trigger": "need_trigger",
    "deeper reason": "scope_controllability",
    "scope_controllability": "scope_controllability",
    "external_alternative": "external_alternative",
    "outside alternative": "external_alternative",
    "unmet need": "unmet_need",
    "unmet_need": "unmet_need",
    "emotion": "emotion",
    "severity": "severity",
    "segment signals": "segment_proxy",
    "segment_proxy": "segment_proxy",
    "brief_questions": "brief_questions",
}


def build_reverse_value_map(codebook_fields: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Map lowercase raw codes AND plain-English labels -> canonical codebook value."""
    rev: dict[str, dict[str, str]] = {}
    for fname, meta in codebook_fields.items():
        mapping: dict[str, str] = {}
        for v in meta.get("values") or []:
            mapping[v.lower()] = v
            mapping[v.replace("_", " ").lower()] = v
        for raw_code, plain_lbl in VALUE_LABELS.get(fname, {}).items():
            mapping[plain_lbl.lower()] = raw_code
        rev[fname] = mapping
    return rev


def normalize_field_name(raw_field: str, codebook_fields: dict[str, Any]) -> str | None:
    key = raw_field.strip().lower()
    if key in FIELD_ALIASES:
        return FIELD_ALIASES[key]
    if raw_field.strip() in codebook_fields:
        return raw_field.strip()
    return None


def normalize_and_validate_value(
    field: str,
    raw_val: str,
    codebook_fields: dict[str, Any],
    rev_map: dict[str, dict[str, str]],
) -> tuple[str | None, str | None]:
    """Return (canonical_value_string, error_message)."""
    meta = codebook_fields.get(field) or {}
    val_clean = raw_val.strip()
    if not val_clean:
        return None, "Empty correct value"

    # Free-text field (unmet_need)
    if meta.get("type") in {"short_text", "exact_quote"} or not meta.get("values"):
        return val_clean, None

    allowed_map = rev_map.get(field, {})
    is_multi = bool(meta.get("multi_select"))

    if is_multi:
        sep = ";" if ";" in val_clean else ","
        parts = [p.strip() for p in val_clean.split(sep) if p.strip()]
        normalized = []
        for p in parts:
            canon = allowed_map.get(p.lower())
            if not canon:
                return None, f"Invalid value '{p}' for multi-select field '{field}'"
            if canon not in normalized:
                normalized.append(canon)
        if not normalized:
            return None, f"No valid values supplied for '{field}'"
        return ",".join(normalized), None

    canon = allowed_map.get(val_clean.lower())
    if not canon:
        return None, f"Invalid value '{val_clean}' for field '{field}'"
    return canon, None


def format_old_value(rec: dict[str, Any] | None, field: str) -> str:
    if not rec:
        return ""
    val = rec.get(field, "")
    if isinstance(val, list):
        return ",".join(str(v) for v in val)
    return str(val or "")


def import_review_workbook(
    workbook_path: Path = DEFAULT_OUT_PATH,
    corrections_path: Path = MANUAL_CORRECTIONS_PATH,
    reviewer: str = "human_review",
    dry_run: bool = False,
) -> dict[str, Any]:
    if not workbook_path.exists():
        raise FileNotFoundError(f"Workbook not found: {workbook_path}")

    codebook = yaml.safe_load(CODEBOOK_PATH.read_text(encoding="utf-8"))
    cb_fields: dict[str, Any] = codebook.get("fields", {})
    rev_map = build_reverse_value_map(cb_fields)

    # Load post-rule-fix baseline to populate accurate old_value
    tagged_raw = load_merged_tagged()
    tagged_audited, _ = apply_audit_rule_fixes(tagged_raw)
    tagged_by_id = {r["id"]: r for r in tagged_audited if r.get("tag_status") == "ok"}

    wb = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    if "All tagged posts" not in wb.sheetnames:
        raise ValueError("Expected sheet 'All tagged posts' in review workbook.")
    ws = wb["All tagged posts"]

    rows_iter = ws.iter_rows(values_only=True)
    header_row = next(rows_iter, None)
    if not header_row:
        raise ValueError("Empty 'All tagged posts' sheet.")
    col_idx = {str(h).strip(): i for i, h in enumerate(header_row) if h is not None}

    required_cols = ["id", "REVIEW: correct? (Y/N)", "REVIEW: field to fix", "REVIEW: correct value", "REVIEW: note"]
    missing = [c for c in required_cols if c not in col_idx]
    if missing:
        raise ValueError(f"Missing expected columns in workbook: {missing}")

    # Load existing corrections.csv
    existing: list[dict[str, str]] = []
    if corrections_path.exists():
        with corrections_path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("id") and row.get("field"):
                    existing.append({k: row.get(k, "") for k in CSV_COLUMNS})

    existing_idx = {(r["id"], r["field"]): i for i, r in enumerate(existing)}
    today_str = date.today().isoformat()

    added = 0
    updated = 0
    skipped_same = 0
    reviewed_y = 0
    invalid_rows: list[dict[str, Any]] = []

    for excel_row_num, row in enumerate(rows_iter, start=2):
        rid = str(row[col_idx["id"]] or "").strip()
        if not rid:
            continue
        yn = str(row[col_idx["REVIEW: correct? (Y/N)"]] or "").strip().upper()
        raw_field = str(row[col_idx["REVIEW: field to fix"]] or "").strip()
        raw_val = str(row[col_idx["REVIEW: correct value"]] or "").strip()
        note = str(row[col_idx["REVIEW: note"]] or "").strip()

        if yn == "Y" and not raw_field and not raw_val:
            reviewed_y += 1
            continue

        wants_fix = (yn == "N") or bool(raw_field or raw_val)
        if not wants_fix:
            continue

        if not raw_field or not raw_val:
            invalid_rows.append(
                {
                    "excel_row": excel_row_num,
                    "id": rid,
                    "error": "Marked for fix (N) but missing 'REVIEW: field to fix' or 'REVIEW: correct value'",
                }
            )
            continue

        canon_field = normalize_field_name(raw_field, cb_fields)
        if not canon_field:
            invalid_rows.append({"excel_row": excel_row_num, "id": rid, "error": f"Unknown field '{raw_field}'"})
            continue

        canon_val, err = normalize_and_validate_value(canon_field, raw_val, cb_fields, rev_map)
        if err or canon_val is None:
            invalid_rows.append({"excel_row": excel_row_num, "id": rid, "error": err})
            continue

        old_val = format_old_value(tagged_by_id.get(rid), canon_field)
        reason_text = note if note else f"Manual review correction on {canon_field}"

        new_entry = {
            "id": rid,
            "field": canon_field,
            "old_value": old_val,
            "correct_value": canon_val,
            "reason": reason_text,
            "reviewer": reviewer,
            "date": today_str,
        }

        key = (rid, canon_field)
        if key in existing_idx:
            cur = existing[existing_idx[key]]
            if cur["correct_value"] == canon_val and cur["reason"] == reason_text:
                skipped_same += 1
            else:
                existing[existing_idx[key]] = new_entry
                updated += 1
        else:
            existing_idx[key] = len(existing)
            existing.append(new_entry)
            added += 1

    wb.close()

    if not dry_run and (added > 0 or updated > 0 or not corrections_path.exists()):
        corrections_path.parent.mkdir(parents=True, exist_ok=True)
        with corrections_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(existing)

    return {
        "status": "ok",
        "dry_run": dry_run,
        "workbook": str(workbook_path),
        "corrections_csv": str(corrections_path),
        "reviewed_confirmed_Y": reviewed_y,
        "added": added,
        "updated": updated,
        "skipped_unchanged": skipped_same,
        "invalid_count": len(invalid_rows),
        "invalid_rows": invalid_rows[:20],
        "total_corrections_in_csv": len(existing),
        "next_step": "Run `python -m pipeline.export` to apply corrections to data/export/.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import human REVIEW corrections from review_workbook.xlsx")
    parser.add_argument("--workbook", type=Path, default=DEFAULT_OUT_PATH, help="Path to review_workbook.xlsx")
    parser.add_argument("--out", type=Path, default=MANUAL_CORRECTIONS_PATH, help="Path to corrections.csv")
    parser.add_argument("--reviewer", type=str, default="human_review", help="Reviewer label")
    parser.add_argument("--dry-run", action="store_true", help="Validate without modifying corrections.csv")
    args = parser.parse_args()
    res = import_review_workbook(
        workbook_path=args.workbook,
        corrections_path=args.out,
        reviewer=args.reviewer,
        dry_run=args.dry_run,
    )
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
