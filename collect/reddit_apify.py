"""Cost-guarded 3-step Reddit collector via Apify (automation-lab/reddit-scraper).

Follows the brief & evidence-log rules:
- Reads live FREE-tier pricing and current monthly spend from the Apify API.
- Enforces a hard $3.50 total spend cap (refuses any run that could exceed it).
- Removes ALL usernames (author fields, u/handles, /user/ links), decodes HTML,
  scrubs emails/phones, excludes InstaHelp, and flags Native hardware.
- Stores each post's title+body as `context` on its comments.

Usage:
    python -m collect.reddit_apify price   # Free read-only pricing & balance check
    python -m collect.reddit_apify test    # 30-item test run (~$0.04)
    python -m collect.reddit_apify step1   # Fetch ~180-220 posts only + free local shortlist
    python -m collect.reddit_apify step3   # Fetch 15 comments per shortlisted thread -> data/raw/reddit.jsonl
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import requests
from apify_client import ApifyClient
from dotenv import load_dotenv

from collect.utils import (
    ROOT_DIR,
    build_keyword_patterns,
    load_config,
    make_item_id,
    save_jsonl,
    scrub_pii,
    summarize_dates,
)

ACTOR_ID = "automation-lab/reddit-scraper"
ACTOR_API_SLUG = "automation-lab~reddit-scraper"
HARD_BUDGET_USD = 3.50
CANDIDATES_PATH = ROOT_DIR / "data/raw/reddit_posts_candidates.jsonl"
SHORTLIST_PATH = ROOT_DIR / "data/raw/reddit_shortlist.jsonl"
MANUAL_URLS_PATH = ROOT_DIR / "data/manual/reddit_threads.txt"

_REDDIT_USER_URL_RE = re.compile(r"https?://(?:www\.|old\.)?reddit\.com/(?:u|user)/[A-Za-z0-9_-]+/?", re.IGNORECASE)
_REDDIT_USER_REF_RE = re.compile(r"(?<!\w)/?u/[A-Za-z0-9_-]+", re.IGNORECASE)

# Targeted Indian city & household search queries covering UC + off-platform home-service decisions
STEP1_SEARCH_PLAN: list[tuple[str, str, int]] = [
    ("bangalore", "Urban Company", 20),
    ("mumbai", "Urban Company", 20),
    ("delhi", "Urban Company", 20),
    ("hyderabad", "Urban Company", 15),
    ("pune", "Urban Company", 15),
    ("chennai", "Urban Company", 12),
    ("kolkata", "Urban Company", 10),
    ("gurgaon", "Urban Company", 12),
    ("india", "Urban Company", 20),
    ("TwoXIndia", "Urban Company", 20),
    ("bangalore", "deep cleaning service", 12),
    ("mumbai", "AC service local", 12),
    ("delhi", "AC service Urban Company", 12),
    ("india", "Urban Company vs local", 15),
    ("TwoXIndia", "salon at home", 12),
]

# Shortlist terms focused on home-service & repeat/alternative stories (excludes InstaHelp)
_HOME_SERVICE_SIGNAL_RE = re.compile(
    r"\b("
    r"urban\s*company|urban\s*clap|uc\b|deep\s*clean|bathroom\s*clean|kitchen\s*clean|"
    r"sofa\s*clean|home\s*clean|ac\s*service|ac\s*repair|air\s*conditioner|"
    r"plumber|plumbing|electrician|carpenter|pest\s*control|salon\s*at\s*home|"
    r"massage|facial|waxing|geyser|washing\s*machine|appliance|ro\s*service|painting|"
    r"local\s*(?:plumber|electrician|ac|technician|parlour|parlor|guy|mechanic)|"
    r"nobroker|mygate|society\s*whatsapp|rebook|again|worth\s*it|alternative|switched"
    r")\b",
    re.IGNORECASE,
)


def _get_token() -> str:
    load_dotenv(ROOT_DIR / ".env")
    token = os.getenv("APIFY_API_KEY") or os.getenv("APIFY_TOKEN")
    if not token:
        raise RuntimeError("APIFY_API_KEY (or APIFY_TOKEN) is missing from .env")
    return token


def clean_reddit_text(text: str | None) -> str:
    """Decode HTML entities, strip all Reddit user links/handles, and scrub emails/phones."""
    if not text:
        return ""
    decoded = html.unescape(text)
    decoded = _REDDIT_USER_URL_RE.sub("[user_link]", decoded)
    decoded = _REDDIT_USER_REF_RE.sub("[user]", decoded)
    return scrub_pii(decoded)


def sanitize_reddit_url(url: str | None) -> str:
    """Strip any /u/ or /user/ profile URLs; keep only clean subreddit thread permalinks."""
    if not url:
        return ""
    if _REDDIT_USER_URL_RE.search(url):
        return ""
    return url.split("?")[0]


def get_pricing_and_balance(token: str) -> dict[str, Any]:
    """Read live FREE-tier pricing for the actor and current monthly spend from Apify API."""
    headers = {"Authorization": f"Bearer {token}"}
    me = requests.get("https://api.apify.com/v2/users/me", headers=headers, timeout=15).json().get("data", {})
    plan = (me.get("plan") or {}).get("id", "FREE")
    limits = requests.get("https://api.apify.com/v2/users/me/limits", headers=headers, timeout=15).json().get("data", {})
    used_usd = float((limits.get("current") or {}).get("monthlyUsageUsd") or 0.0)
    max_monthly_usd = float((limits.get("limits") or {}).get("maxMonthlyUsageUsd") or 5.0)

    act = requests.get(f"https://api.apify.com/v2/acts/{ACTOR_API_SLUG}", headers=headers, timeout=15).json().get("data", {})
    pi = (act.get("pricingInfos") or [{}])[-1]
    events = pi.get("pricingPerEvent", {}).get("actorChargeEvents", {})

    def _tier_price(ev_key: str) -> float:
        ev = events.get(ev_key, {})
        if ev.get("eventPriceUsd") is not None:
            return float(ev["eventPriceUsd"])
        tiered = ev.get("eventTieredPricingUsd") or {}
        if plan in tiered:
            return float(tiered[plan]["tieredEventPriceUsd"])
        if "FREE" in tiered:
            return float(tiered["FREE"]["tieredEventPriceUsd"])
        return 0.0

    start_usd = _tier_price("start")
    post_usd = _tier_price("post")
    comment_usd = _tier_price("comment")
    remaining_budget_usd = round(max(0.0, HARD_BUDGET_USD - used_usd), 4)

    return {
        "actor": ACTOR_ID,
        "plan": plan,
        "monthly_used_usd": round(used_usd, 4),
        "monthly_free_limit_usd": round(max_monthly_usd, 2),
        "hard_budget_cap_usd": HARD_BUDGET_USD,
        "remaining_budget_usd": remaining_budget_usd,
        "start_cost_usd": start_usd,
        "post_cost_per_1k_usd": round(post_usd * 1000, 3),
        "comment_cost_per_1k_usd": round(comment_usd * 1000, 3),
        "post_unit_usd": post_usd,
        "comment_unit_usd": comment_usd,
    }


def _enforce_budget(pricing: dict[str, Any], est_cost_usd: float) -> float:
    """Refuse any run that could exceed the $3.50 hard cap; return safe max_total_charge_usd."""
    remaining = pricing["remaining_budget_usd"]
    if est_cost_usd > remaining:
        raise RuntimeError(
            f"Budget guard triggered: estimated cost ${est_cost_usd:.3f} exceeds "
            f"remaining budget ${remaining:.3f} (hard cap ${HARD_BUDGET_USD:.2f}). Refusing to run."
        )
    # Give small headroom above estimate while never exceeding remaining hard budget
    return round(min(remaining, max(est_cost_usd * 1.4, 0.15)), 3)


def _run_actor(token: str, run_input: dict[str, Any], max_charge_usd: float) -> tuple[list[dict[str, Any]], float]:
    """Run the Apify actor with server-side max_total_charge_usd cap and return (items, actual_cost_usd)."""
    client = ApifyClient(token)
    run = client.actor(ACTOR_ID).call(run_input=run_input, max_total_charge_usd=max_charge_usd)
    cost = float(run.get("usageTotalUsd") or 0.0)
    dataset_id = run.get("defaultDatasetId")
    items = list(client.dataset(dataset_id).iterate_items()) if dataset_id else []
    return items, cost


def _extract_post_record(item: dict[str, Any], insta_pats: list[re.Pattern[str]], native_pats: list[re.Pattern[str]]) -> dict[str, Any] | None:
    """Normalize a raw Apify post item, stripping all usernames and PII."""
    title = clean_reddit_text(item.get("title") or "")
    body = clean_reddit_text(item.get("body") or item.get("selftext") or item.get("text") or "")
    combined = f"{title}. {body}".strip(". ").strip() if title else body
    if not combined:
        return None

    subreddit = str(item.get("subreddit") or item.get("communityName") or "").replace("r/", "").strip()
    raw_url = sanitize_reddit_url(str(item.get("url") or item.get("permalink") or ""))
    if raw_url.startswith("/r/"):
        raw_url = f"https://www.reddit.com{raw_url}"
    raw_id = str(item.get("id") or item.get("postId") or raw_url or combined[:80])
    created_at = str(item.get("createdAt") or item.get("created_utc") or "")

    is_instahelp = any(p.search(combined) for p in insta_pats)
    is_native = any(p.search(combined) for p in native_pats)

    return {
        "id": make_item_id("reddit_post", raw_id),
        "raw_reddit_id": raw_id,
        "source": "reddit",
        "item_type": "post",
        "subreddit": subreddit,
        "title": title,
        "text": combined,
        "context": f"r/{subreddit}" if subreddit else "",
        "rating": None,
        "upvotes": int(item.get("score") or item.get("upVotes") or item.get("ups") or 0),
        "num_comments": int(item.get("numComments") or item.get("numberOfComments") or 0),
        "created_at": created_at,
        "url": raw_url,
        "is_instahelp": is_instahelp,
        "is_native_hardware": is_native,
    }


def _extract_comment_record(
    item: dict[str, Any],
    post_context_map: dict[str, tuple[str, str]],
    insta_pats: list[re.Pattern[str]],
    native_pats: list[re.Pattern[str]],
) -> dict[str, Any] | None:
    """Normalize a raw Apify comment item with parent post as `context` and zero usernames."""
    body = clean_reddit_text(item.get("body") or item.get("text") or "")
    if not body or body.lower() in {"[deleted]", "[removed]"}:
        return None

    post_url = sanitize_reddit_url(str(item.get("postUrl") or item.get("linkUrl") or item.get("url") or ""))
    if post_url.startswith("/r/"):
        post_url = f"https://www.reddit.com{post_url}"
    post_id = str(item.get("postId") or "")
    subreddit = str(item.get("subreddit") or item.get("communityName") or "").replace("r/", "").strip()

    ctx_text, ctx_sub = post_context_map.get(post_id) or post_context_map.get(post_url) or ("", subreddit)
    if not subreddit:
        subreddit = ctx_sub
    context = f"[r/{subreddit}] {ctx_text}".strip() if ctx_text else (f"r/{subreddit}" if subreddit else "")

    raw_id = str(item.get("id") or f"{post_id}:{body[:60]}")
    created_at = str(item.get("createdAt") or item.get("created_utc") or "")
    check_blob = f"{body} {context}"
    is_instahelp = any(p.search(check_blob) for p in insta_pats)
    is_native = any(p.search(check_blob) for p in native_pats)

    return {
        "id": make_item_id("reddit_comment", raw_id),
        "source": "reddit",
        "item_type": "comment",
        "subreddit": subreddit,
        "text": body,
        "context": context[:600],
        "rating": None,
        "upvotes": int(item.get("score") or item.get("upVotes") or item.get("ups") or 0),
        "created_at": created_at,
        "url": post_url,
        "is_instahelp": is_instahelp,
        "is_native_hardware": is_native,
    }


def cmd_price() -> None:
    token = _get_token()
    info = get_pricing_and_balance(token)
    print(json.dumps(info, indent=2))


def cmd_test() -> None:
    """Test run: max 30 posts from r/bangalore searching 'Urban Company' (~$0.04)."""
    cfg = load_config()
    _, insta_pats, native_pats = build_keyword_patterns(cfg)
    token = _get_token()
    pricing = get_pricing_and_balance(token)

    est_cost = pricing["start_cost_usd"] + 30 * pricing["post_unit_usd"]
    max_charge = _enforce_budget(pricing, est_cost)

    run_input = {
        "searchQuery": "Urban Company",
        "searchSubreddit": "bangalore",
        "sort": "relevance",
        "timeFilter": "all",
        "maxPostsPerSource": 30,
        "includeComments": False,
        "postedAfter": "2021-01-01",
        "includeNsfw": False,
        "deduplicatePosts": True,
    }
    raw_items, actual_cost = _run_actor(token, run_input, max_charge)
    parsed = []
    for it in raw_items:
        rec = _extract_post_record(it, insta_pats, native_pats)
        if rec:
            parsed.append(rec)

    summary = {
        "mode": "test",
        "raw_items_received": len(raw_items),
        "parsed_posts": len(parsed),
        "actual_cost_usd": round(actual_cost, 4),
        "remaining_budget_after_est_usd": round(max(0.0, pricing["remaining_budget_usd"] - actual_cost), 4),
        "samples": [
            {
                "subreddit": r["subreddit"],
                "date": r["created_at"][:10],
                "upvotes": r["upvotes"],
                "num_comments": r["num_comments"],
                "is_instahelp": r["is_instahelp"],
                "title": r["title"],
                "text_preview": r["text"][:220],
            }
            for r in parsed[:5]
        ],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


def cmd_step1() -> None:
    """Step 1 (posts only) + Step 2 (free local shortlist) across Indian city & household subreddits."""
    cfg = load_config()
    _, insta_pats, native_pats = build_keyword_patterns(cfg)
    exclude_instahelp = bool(cfg.get("project", {}).get("exclude_instahelp", True))
    token = _get_token()
    pricing = get_pricing_and_balance(token)

    search_urls = [
        f"https://www.reddit.com/r/{sub}/search/?q={quote_plus(query)}&restrict_sr=1&sort=relevance&t=all"
        for sub, query, _ in STEP1_SEARCH_PLAN
    ]
    max_per_source = 15
    est_posts = len(search_urls) * max_per_source
    est_cost = pricing["start_cost_usd"] + est_posts * pricing["post_unit_usd"]
    max_charge = _enforce_budget(pricing, est_cost)

    run_input = {
        "urls": search_urls,
        "maxPostsPerSource": max_per_source,
        "includeComments": False,
        "postedAfter": "2021-01-01",
        "includeNsfw": False,
        "deduplicatePosts": True,
    }
    raw_items, actual_cost = _run_actor(token, run_input, max_charge)

    seen_urls: set[str] = set()
    candidates: list[dict[str, Any]] = []
    instahelp_excluded = 0

    for it in raw_items:
        rec = _extract_post_record(it, insta_pats, native_pats)
        if not rec:
            continue
        key = rec["url"] or rec["id"]
        if key in seen_urls:
            continue
        seen_urls.add(key)
        if exclude_instahelp and rec["is_instahelp"]:
            instahelp_excluded += 1
            continue
        candidates.append(rec)

    save_jsonl(candidates, CANDIDATES_PATH)

    # Step 2 (free code): shortlist substantive home-service / UC threads with real discussion
    shortlist = []
    for rec in candidates:
        title_and_text = f"{rec['title']} {rec['text']}"
        if not _HOME_SERVICE_SIGNAL_RE.search(title_and_text):
            continue
        if rec["num_comments"] < 2:
            continue
        shortlist.append(rec)

    # Prioritize threads with rich discussion & clear Urban Company / home-service focus
    shortlist.sort(key=lambda r: (r["num_comments"], r["upvotes"]), reverse=True)
    shortlist = shortlist[:45]
    save_jsonl(shortlist, SHORTLIST_PATH)

    print(
        json.dumps(
            {
                "mode": "step1_and_step2",
                "raw_items_received": len(raw_items),
                "unique_candidates_after_instahelp_exclusion": len(candidates),
                "instahelp_excluded": instahelp_excluded,
                "shortlisted_threads": len(shortlist),
                "actual_cost_usd": round(actual_cost, 4),
                "shortlist_file": str(SHORTLIST_PATH),
                "shortlist_titles": [
                    {
                        "n": i + 1,
                        "subreddit": r["subreddit"],
                        "date": r["created_at"][:10],
                        "comments": r["num_comments"],
                        "title": r["title"],
                        "url": r["url"],
                    }
                    for i, r in enumerate(shortlist)
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def cmd_step3() -> None:
    """Step 3: fetch up to 15 comments each for shortlisted threads (+ manual URLs first)."""
    cfg = load_config()
    _, insta_pats, native_pats = build_keyword_patterns(cfg)
    exclude_instahelp = bool(cfg.get("project", {}).get("exclude_instahelp", True))
    token = _get_token()
    pricing = get_pricing_and_balance(token)

    urls: list[str] = []
    if MANUAL_URLS_PATH.exists():
        for line in MANUAL_URLS_PATH.read_text(encoding="utf-8").splitlines():
            u = line.strip()
            if u.startswith("http") and u not in urls:
                urls.append(u)

    shortlist_posts: list[dict[str, Any]] = []
    if SHORTLIST_PATH.exists():
        with SHORTLIST_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                shortlist_posts.append(rec)
                if rec.get("url") and rec["url"] not in urls:
                    urls.append(rec["url"])

    if not urls:
        raise RuntimeError("No shortlisted or manual Reddit URLs found. Run step1 first.")

    max_comments_per_post = 15
    est_cost = (
        pricing["start_cost_usd"]
        + len(urls) * pricing["post_unit_usd"]
        + len(urls) * max_comments_per_post * pricing["comment_unit_usd"]
    )
    max_charge = _enforce_budget(pricing, est_cost)

    run_input = {
        "urls": urls,
        "includeComments": True,
        "maxCommentsPerPost": max_comments_per_post,
        "commentDepth": 2,
        "includeNsfw": False,
        "deduplicatePosts": True,
    }
    raw_items, actual_cost = _run_actor(token, run_input, max_charge)

    # First pass: index posts so every comment gets its parent post as `context`
    post_context_map: dict[str, tuple[str, str]] = {}
    final_records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    instahelp_excluded = 0
    native_flagged = 0
    post_count = 0
    comment_count = 0

    for rec in shortlist_posts:
        post_context_map[rec.get("raw_reddit_id", "")] = (rec["text"][:500], rec["subreddit"])
        if rec.get("url"):
            post_context_map[rec["url"]] = (rec["text"][:500], rec["subreddit"])

    raw_posts = []
    raw_comments = []
    for it in raw_items:
        dtype = str(it.get("dataType") or it.get("type") or "").lower()
        if dtype == "comment" or ("title" not in it and ("body" in it or "commentId" in it)):
            raw_comments.append(it)
        else:
            raw_posts.append(it)
            # Also handle nested comments arrays if returned inline by actor
            nested = it.get("comments")
            if isinstance(nested, list):
                p_url = sanitize_reddit_url(str(it.get("url") or ""))
                p_id = str(it.get("id") or "")
                p_sub = str(it.get("subreddit") or "").replace("r/", "")
                for c in nested:
                    if isinstance(c, dict):
                        c.setdefault("postUrl", p_url)
                        c.setdefault("postId", p_id)
                        c.setdefault("subreddit", p_sub)
                        raw_comments.append(c)

    for it in raw_posts:
        rec = _extract_post_record(it, insta_pats, native_pats)
        if not rec:
            continue
        post_context_map[rec["raw_reddit_id"]] = (rec["text"][:500], rec["subreddit"])
        if rec["url"]:
            post_context_map[rec["url"]] = (rec["text"][:500], rec["subreddit"])
        if exclude_instahelp and rec["is_instahelp"]:
            instahelp_excluded += 1
            continue
        if rec["id"] in seen_ids:
            continue
        seen_ids.add(rec["id"])
        rec.pop("raw_reddit_id", None)
        if rec["is_native_hardware"]:
            native_flagged += 1
        final_records.append(rec)
        post_count += 1

    for it in raw_comments:
        rec = _extract_comment_record(it, post_context_map, insta_pats, native_pats)
        if not rec:
            continue
        if exclude_instahelp and rec["is_instahelp"]:
            instahelp_excluded += 1
            continue
        if rec["id"] in seen_ids:
            continue
        seen_ids.add(rec["id"])
        if rec["is_native_hardware"]:
            native_flagged += 1
        final_records.append(rec)
        comment_count += 1

    out_path = save_jsonl(final_records, cfg["sources"]["reddit"]["output_file"])
    print(
        json.dumps(
            {
                "mode": "step3",
                "threads_requested": len(urls),
                "raw_items_received": len(raw_items),
                "posts_kept": post_count,
                "comments_kept": comment_count,
                "total_kept": len(final_records),
                "instahelp_excluded": instahelp_excluded,
                "native_hardware_flagged": native_flagged,
                "date_range": summarize_dates(final_records),
                "actual_cost_usd": round(actual_cost, 4),
                "output_file": str(out_path),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m collect.reddit_apify <price|test|step1|step3>")
        sys.exit(1)
    cmd = sys.argv[1].strip().lower()
    if cmd == "price":
        cmd_price()
    elif cmd == "test":
        cmd_test()
    elif cmd == "step1":
        cmd_step1()
    elif cmd == "step3":
        cmd_step3()
    else:
        print(f"Unknown command '{cmd}'. Use: price, test, step1, step3")
        sys.exit(1)


if __name__ == "__main__":
    main()
