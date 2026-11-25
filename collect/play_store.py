"""Google Play Store collector for Urban Company reviews."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from google_play_scraper import Sort, reviews

from collect.utils import (
    build_keyword_patterns,
    evaluate_text,
    make_item_id,
    save_jsonl,
    scrub_pii,
    summarize_dates,
)


def _fetch_batch(
    app_id: str,
    lang: str,
    country: str,
    sort_order: Sort,
    target_count: int,
    filter_score: int | None = None,
) -> list[dict[str, Any]]:
    """Paginate through Google Play reviews up to target_count."""
    collected: list[dict[str, Any]] = []
    continuation_token = None

    while len(collected) < target_count:
        batch_size = min(200, target_count - len(collected))
        kwargs: dict[str, Any] = {
            "lang": lang,
            "country": country,
            "sort": sort_order,
            "count": batch_size,
            "continuation_token": continuation_token,
        }
        if filter_score is not None:
            kwargs["filter_score_with"] = filter_score

        result, continuation_token = reviews(app_id, **kwargs)
        if not result:
            break
        collected.extend(result)
        if continuation_token is None:
            break
        time.sleep(0.2)

    return collected


def collect_play_store(cfg: dict[str, Any]) -> dict[str, Any]:
    """Collect and keyword-filter Urban Company Google Play Store reviews."""
    src_cfg = cfg["sources"]["play_store"]
    proj_cfg = cfg.get("project", {})
    app_id = src_cfg["app_id"]
    lang = src_cfg.get("lang", "en")
    country = src_cfg.get("country", "in")
    max_reviews = int(src_cfg.get("max_reviews", 5000))
    balanced = bool(src_cfg.get("balanced_by_rating", True))
    sort_name = str(src_cfg.get("sort", "newest")).lower()
    sort_order = Sort.NEWEST if sort_name == "newest" else Sort.MOST_RELEVANT
    min_words = int(proj_cfg.get("min_words_after_filter", 6))
    exclude_instahelp = bool(proj_cfg.get("exclude_instahelp", True))

    keep_patterns, instahelp_patterns, native_patterns = build_keyword_patterns(cfg)

    raw_reviews: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    if balanced:
        per_star = max(1, max_reviews // 5)
        for star in (1, 2, 3, 4, 5):
            batch = _fetch_batch(app_id, lang, country, sort_order, per_star, filter_score=star)
            for item in batch:
                rid = str(item.get("reviewId", ""))
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    raw_reviews.append(item)
    else:
        batch = _fetch_batch(app_id, lang, country, sort_order, max_reviews)
        for item in batch:
            rid = str(item.get("reviewId", ""))
            if rid and rid not in seen_ids:
                seen_ids.add(rid)
                raw_reviews.append(item)

    pulled = len(raw_reviews)
    kept_records: list[dict[str, Any]] = []
    pulled_ratings: Counter[int] = Counter()
    kept_ratings: Counter[int] = Counter()
    instahelp_excluded = 0
    native_flagged = 0

    for item in raw_reviews:
        rating = item.get("score")
        if rating is not None:
            pulled_ratings[int(rating)] += 1

        text = scrub_pii(item.get("content"))
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

        if is_native_hardware:
            native_flagged += 1
        if rating is not None:
            kept_ratings[int(rating)] += 1

        at_val = item.get("at")
        created_at = at_val.isoformat() if hasattr(at_val, "isoformat") else str(at_val or "")

        kept_records.append(
            {
                "id": make_item_id("play_store", str(item.get("reviewId") or text[:60])),
                "source": "play_store",
                "text": text,
                "context": "",
                "rating": int(rating) if rating is not None else None,
                "thumbs_up": int(item.get("thumbsUpCount") or 0),
                "created_at": created_at,
                "url": f"https://play.google.com/store/apps/details?id={app_id}&hl={lang}&gl={country}",
                "matched_keywords": matched_keywords,
                "is_instahelp": is_instahelp,
                "is_native_hardware": is_native_hardware,
            }
        )

    out_path = save_jsonl(kept_records, src_cfg["output_file"])
    return {
        "source": "play_store",
        "pulled": pulled,
        "kept": len(kept_records),
        "instahelp_excluded": instahelp_excluded,
        "native_hardware_flagged": native_flagged,
        "date_range": summarize_dates(kept_records),
        "pulled_ratings": dict(sorted(pulled_ratings.items())),
        "kept_ratings": dict(sorted(kept_ratings.items())),
        "output_file": str(out_path),
    }
