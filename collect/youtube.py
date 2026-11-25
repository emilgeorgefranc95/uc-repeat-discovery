"""YouTube comments collector using the official YouTube Data API v3."""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from collect.utils import (
    ROOT_DIR,
    build_keyword_patterns,
    evaluate_text,
    make_item_id,
    save_jsonl,
    scrub_pii,
    summarize_dates,
)


def collect_youtube(cfg: dict[str, Any]) -> dict[str, Any]:
    """Search public Indian home-service / UC videos and collect top-level comments."""
    load_dotenv(ROOT_DIR / ".env")
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is missing from .env")

    src_cfg = cfg["sources"]["youtube"]
    proj_cfg = cfg.get("project", {})
    queries = list(src_cfg.get("queries", []))
    region_code = src_cfg.get("region_code", "IN")
    lang = src_cfg.get("relevance_language", "en")
    max_videos = int(src_cfg.get("max_videos_per_query", 6))
    max_comments = int(src_cfg.get("max_comments_per_video", 100))
    min_words = int(proj_cfg.get("min_words_after_filter", 6))
    exclude_instahelp = bool(proj_cfg.get("exclude_instahelp", True))

    keep_patterns, instahelp_patterns, native_patterns = build_keyword_patterns(cfg)
    youtube = build("youtube", "v3", developerKey=api_key)

    quota_units = 0
    seen_videos: dict[str, str] = {}

    for query in queries:
        req = youtube.search().list(
            q=query,
            part="snippet",
            type="video",
            regionCode=region_code,
            relevanceLanguage=lang,
            maxResults=min(25, max_videos),
        )
        resp = req.execute()
        quota_units += 100  # search.list costs 100 units

        for item in resp.get("items", []):
            vid = item.get("id", {}).get("videoId")
            title = scrub_pii(item.get("snippet", {}).get("title", ""))
            if vid and vid not in seen_videos:
                seen_videos[vid] = title

    pulled = 0
    kept_records: list[dict[str, Any]] = []
    seen_comments: set[str] = set()
    instahelp_excluded = 0
    native_flagged = 0

    for vid, video_title in seen_videos.items():
        next_page_token = None
        fetched_for_video = 0

        while fetched_for_video < max_comments:
            try:
                req = youtube.commentThreads().list(
                    part="snippet",
                    videoId=vid,
                    maxResults=min(100, max_comments - fetched_for_video),
                    textFormat="plainText",
                    order="relevance",
                    pageToken=next_page_token,
                )
                resp = req.execute()
                quota_units += 1  # commentThreads.list costs 1 unit
            except HttpError:
                break

            items = resp.get("items", [])
            if not items:
                break

            for thread in items:
                cid = str(thread.get("id", ""))
                if cid in seen_comments:
                    continue
                seen_comments.add(cid)
                pulled += 1
                fetched_for_video += 1

                top = thread.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                text = scrub_pii(top.get("textDisplay") or top.get("textOriginal"))
                if not text:
                    continue

                kept, matched_keywords, is_instahelp, is_native_hardware = evaluate_text(
                    text,
                    keep_patterns,
                    instahelp_patterns,
                    native_patterns=native_patterns,
                    min_words=min_words,
                    context=video_title,
                    exclude_instahelp=exclude_instahelp,
                )
                if exclude_instahelp and is_instahelp:
                    instahelp_excluded += 1
                    continue

                # Keep substantive comments on targeted home-service videos if keywords appear in title
                if not kept and len(text.split()) >= min_words:
                    title_kept, title_kw, title_insta, title_native = evaluate_text(
                        video_title,
                        keep_patterns,
                        instahelp_patterns,
                        native_patterns=native_patterns,
                        min_words=1,
                        exclude_instahelp=exclude_instahelp,
                    )
                    if exclude_instahelp and title_insta:
                        instahelp_excluded += 1
                        continue
                    if title_kept:
                        kept = True
                        matched_keywords = [f"title:{k}" for k in title_kw[:3]]
                        is_native_hardware = is_native_hardware or title_native

                if not kept:
                    continue

                if is_native_hardware:
                    native_flagged += 1

                kept_records.append(
                    {
                        "id": make_item_id("youtube", cid or text[:60]),
                        "source": "youtube",
                        "text": text,
                        "context": video_title,
                        "rating": None,
                        "likes": int(top.get("likeCount") or 0),
                        "created_at": str(top.get("publishedAt") or ""),
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "matched_keywords": matched_keywords,
                        "is_instahelp": is_instahelp,
                        "is_native_hardware": is_native_hardware,
                    }
                )

            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break

    out_path = save_jsonl(kept_records, src_cfg["output_file"])
    return {
        "source": "youtube",
        "videos_scanned": len(seen_videos),
        "pulled": pulled,
        "kept": len(kept_records),
        "instahelp_excluded": instahelp_excluded,
        "native_hardware_flagged": native_flagged,
        "date_range": summarize_dates(kept_records),
        "youtube_quota_units_est": quota_units,
        "youtube_quota_pct_of_10k": round((quota_units / 10000) * 100, 2),
        "output_file": str(out_path),
    }
