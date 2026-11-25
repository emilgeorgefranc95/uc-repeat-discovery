"""Apple App Store collector using Apple's public iTunes RSS customer reviews feed."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import requests

from collect.utils import (
    build_keyword_patterns,
    evaluate_text,
    make_item_id,
    save_jsonl,
    scrub_pii,
    summarize_dates,
)


def collect_app_store(cfg: dict[str, Any]) -> dict[str, Any]:
    """Collect and keyword-filter Urban Company Apple App Store reviews (India)."""
    src_cfg = cfg["sources"]["app_store"]
    proj_cfg = cfg.get("project", {})
    app_id = str(src_cfg["app_id"])
    country = src_cfg.get("country", "in")
    max_pages = min(10, int(src_cfg.get("max_pages", 10)))
    min_words = int(proj_cfg.get("min_words_after_filter", 6))
    exclude_instahelp = bool(proj_cfg.get("exclude_instahelp", True))

    keep_patterns, instahelp_patterns, native_patterns = build_keyword_patterns(cfg)

    pulled = 0
    kept_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    kept_ratings: Counter[int] = Counter()
    instahelp_excluded = 0
    native_flagged = 0

    headers = {"User-Agent": "Mozilla/5.0 (compatible; ResearchDiscoveryBot/1.0)"}

    for page in range(1, max_pages + 1):
        url = (
            f"https://itunes.apple.com/{country}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortby=mostrecent/json"
        )
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            break
        data = resp.json()
        entries = data.get("feed", {}).get("entry", [])
        if not entries:
            break

        for entry in entries:
            if "im:rating" not in entry:
                continue

            raw_id = entry.get("id", {}).get("label", "")
            if raw_id in seen_ids:
                continue
            seen_ids.add(raw_id)
            pulled += 1

            title = scrub_pii(entry.get("title", {}).get("label", ""))
            body = scrub_pii(entry.get("content", {}).get("label", ""))
            text = f"{title}. {body}".strip(". ").strip() if title else body
            if not text:
                continue

            kept, matched_keywords, is_instahelp, is_native_hardware = evaluate_text(
                text,
                keep_patterns,
                instahelp_patterns,
                native_patterns=native_patterns,
                min_words=min_words,
                exclude_instahelp=exclude_instahelp,
            )
            if exclude_instahelp and is_instahelp:
                instahelp_excluded += 1
                continue
            if not kept:
                continue

            rating_str = entry.get("im:rating", {}).get("label")
            rating = int(rating_str) if rating_str and rating_str.isdigit() else None
            if rating is not None:
                kept_ratings[rating] += 1
            if is_native_hardware:
                native_flagged += 1

            created_at = entry.get("updated", {}).get("label", "")
            kept_records.append(
                {
                    "id": make_item_id("app_store", raw_id or text[:60]),
                    "source": "app_store",
                    "text": text,
                    "context": "",
                    "rating": rating,
                    "created_at": created_at,
                    "url": f"https://apps.apple.com/{country}/app/id{app_id}",
                    "matched_keywords": matched_keywords,
                    "is_instahelp": is_instahelp,
                    "is_native_hardware": is_native_hardware,
                }
            )

        time.sleep(0.3)

    out_path = save_jsonl(kept_records, src_cfg["output_file"])
    return {
        "source": "app_store",
        "pulled": pulled,
        "kept": len(kept_records),
        "instahelp_excluded": instahelp_excluded,
        "native_hardware_flagged": native_flagged,
        "date_range": summarize_dates(kept_records),
        "kept_ratings": dict(sorted(kept_ratings.items())),
        "output_file": str(out_path),
    }
