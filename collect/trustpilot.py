"""Public Trustpilot review collector for Urban Company."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

import requests
from bs4 import BeautifulSoup

from collect.utils import (
    build_keyword_patterns,
    evaluate_text,
    make_item_id,
    save_jsonl,
    scrub_pii,
    summarize_dates,
)


def _extract_reviews_from_html(html: str) -> list[dict[str, Any]]:
    """Parse public review objects from Trustpilot's __NEXT_DATA__ payload."""
    soup = BeautifulSoup(html, "html.parser")
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag or not script_tag.string:
        return []
    try:
        payload = json.loads(script_tag.string)
    except json.JSONDecodeError:
        return []

    return payload.get("props", {}).get("pageProps", {}).get("reviews", []) or []


def collect_trustpilot(cfg: dict[str, Any]) -> dict[str, Any]:
    """Collect and keyword-filter public Trustpilot reviews for Urban Company."""
    src_cfg = cfg["sources"]["trustpilot"]
    proj_cfg = cfg.get("project", {})
    domain = src_cfg["domain"]
    max_pages = int(src_cfg.get("max_pages", 25))
    min_words = int(proj_cfg.get("min_words_after_filter", 6))
    exclude_instahelp = bool(proj_cfg.get("exclude_instahelp", True))

    keep_patterns, instahelp_patterns, native_patterns = build_keyword_patterns(cfg)

    pulled = 0
    kept_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    kept_ratings: Counter[int] = Counter()
    instahelp_excluded = 0
    native_flagged = 0

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
    }

    for page in range(1, max_pages + 1):
        url = f"https://www.trustpilot.com/review/{domain}?page={page}"
        resp = requests.get(url, headers=headers, timeout=20)
        if resp.status_code != 200:
            break

        reviews_list = _extract_reviews_from_html(resp.text)
        if not reviews_list:
            break

        for rev in reviews_list:
            raw_id = str(rev.get("id", ""))
            if raw_id in seen_ids:
                continue
            seen_ids.add(raw_id)
            pulled += 1

            title = scrub_pii(rev.get("title"))
            body = scrub_pii(rev.get("text"))
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

            rating = rev.get("rating")
            rating_int = int(rating) if rating is not None else None
            if rating_int is not None:
                kept_ratings[rating_int] += 1
            if is_native_hardware:
                native_flagged += 1

            dates_obj = rev.get("dates") or {}
            created_at = str(dates_obj.get("publishedDate") or dates_obj.get("experiencedDate") or "")

            kept_records.append(
                {
                    "id": make_item_id("trustpilot", raw_id or text[:60]),
                    "source": "trustpilot",
                    "text": text,
                    "context": "",
                    "rating": rating_int,
                    "created_at": created_at,
                    "url": f"https://www.trustpilot.com/reviews/{raw_id}" if raw_id else url,
                    "matched_keywords": matched_keywords,
                    "is_instahelp": is_instahelp,
                    "is_native_hardware": is_native_hardware,
                }
            )

        time.sleep(1.0)

    out_path = save_jsonl(kept_records, src_cfg["output_file"])
    return {
        "source": "trustpilot",
        "pulled": pulled,
        "kept": len(kept_records),
        "instahelp_excluded": instahelp_excluded,
        "native_hardware_flagged": native_flagged,
        "date_range": summarize_dates(kept_records),
        "kept_ratings": dict(sorted(kept_ratings.items())),
        "output_file": str(out_path),
    }
