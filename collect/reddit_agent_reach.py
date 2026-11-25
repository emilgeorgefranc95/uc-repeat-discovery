"""Zero-cost Reddit collector powered by open-source Agent Reach (`--backend public-web`).

Collects Urban Company and Indian home-service Reddit discussions + top comments
without any paid APIs:
- Runs targeted Indian city & household queries via `agent-reach reddit search <q> --limit 20 --backend public-web`.
- Filters out Urban Company stock/IPO/investing threads so only consumer home-service discussions remain.
- Splits each thread into the original post + individual parsed comments (attaching the post as `context`).
- Strips ALL usernames (`u/...`, author headers, profile links), decodes HTML, scrubs emails/phones,
  excludes InstaHelp, and flags Native hardware.
- Writes clean records to `data/raw/reddit.jsonl`.

Usage:
    python -m collect.reddit_agent_reach
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from collect.utils import (
    ROOT_DIR,
    build_keyword_patterns,
    load_config,
    make_item_id,
    save_jsonl,
    scrub_pii,
    summarize_dates,
)

AGENT_REACH_BIN = shutil.which("agent-reach") or str(Path.home() / ".local/bin/agent-reach")

REDDIT_QUERIES: list[str] = [
    # Indian city & household communities
    "Urban Company r/bangalore",
    "Urban Company r/mumbai",
    "Urban Company r/delhi",
    "Urban Company r/hyderabad",
    "Urban Company r/pune",
    "Urban Company r/chennai",
    "Urban Company r/kolkata",
    "Urban Company r/gurgaon",
    "Urban Company r/noida",
    "Urban Company r/india",
    "Urban Company r/TwoXIndia",
    # Repeat usage, trust, and outside-app alternatives (counters Bias #2)
    "Urban Company worth it experience India",
    "Urban Company vs local plumber electrician India",
    "Urban Company AC service vs local mechanic India",
    "Urban Company deep cleaning review India",
    "Urban Company salon at home vs parlour India",
    "stopped using Urban Company India",
    "Urban Company rebook same professional India",
    "home deep cleaning service India reddit",
    "AC servicing India local or Urban Company",
    "find reliable plumber electrician India society whatsapp",
    "Urban Company bathroom cleaning worth it",
    "Urban Company pest control review India",
    "Urban Company painting service review India",
]

# Exclude stock/IPO/investing subreddits and pure stock-market threads
_STOCK_SUBREDDITS = {
    "indianstocks",
    "indianstockmarket",
    "stockmarketindia",
    "equityresearchindia",
    "indiainvestments",
    "dalalstreettalks",
    "mutualfunds",
    "personalfinanceindia",
    "indianstreetbets",
    "ipos",
    "startupsindia",
    "india_investments",
}

_STOCK_TITLE_RE = re.compile(
    r"\b(ipo|share\s*price|stock|stocks|allotment|listing\s*gain|gmp|drhp|rhp|"
    r"valuation|market\s*cap|earnings|q[1-4]\s*fy|nse|bse|invest\s+in\s+urban\s+company|"
    r"buy\s+urban\s+company\s+shares|portfolio|multibagger|dividend)\b",
    re.IGNORECASE,
)

# Must relate to Urban Company OR Indian home services
_CONSUMER_RELEVANCE_RE = re.compile(
    r"\b(urban\s*company|urban\s*clap|urbanclap|deep\s*clean|bathroom\s*clean|"
    r"kitchen\s*clean|sofa\s*clean|home\s*clean|ac\s*servic|ac\s*repair|air\s*conditioner|"
    r"plumber|plumbing|electrician|carpenter|pest\s*control|salon\s*at\s*home|"
    r"massage|waxing|facial|geyser|washing\s*machine|water\s*purifier|ro\s*service|"
    r"painting\s*service|house\s*painting|home\s*service|local\s*technician|"
    r"local\s*plumber|local\s*electrician|nobroker|mygate)\b",
    re.IGNORECASE,
)

_SUBREDDIT_FROM_URL_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]+)/", re.IGNORECASE)
_REDDIT_USER_URL_RE = re.compile(r"https?://(?:www\.|old\.)?reddit\.com/(?:u|user)/[A-Za-z0-9_-]+/?", re.IGNORECASE)
_REDDIT_USER_REF_RE = re.compile(r"(?<!\w)/?u/[A-Za-z0-9_-]+", re.IGNORECASE)
_POSTED_BY_LINE_RE = re.compile(r"^Posted by .*?$", re.IGNORECASE | re.MULTILINE)
_COMMENT_HEADER_RE = re.compile(
    r"^\*\*u/[A-Za-z0-9_-]+\*\*\s*·\s*([-\d]+)\s*points?\s*·\s*([^\n]+)$",
    re.MULTILINE,
)


def clean_text(text: str | None) -> str:
    """Decode HTML, strip Reddit usernames/profile URLs, markdown quotes, and scrub PII."""
    if not text:
        return ""
    s = html.unescape(text)
    s = _REDDIT_USER_URL_RE.sub("[user_link]", s)
    s = _REDDIT_USER_REF_RE.sub("[user]", s)
    # Remove leading markdown blockquote '>' prefixes
    s = re.sub(r"(?m)^\s*>\s?", "", s)
    s = re.sub(r"\*\*|\*|__|~~|`", "", s)
    return scrub_pii(s)


def extract_subreddit(url: str, title: str) -> str:
    m = _SUBREDDIT_FROM_URL_RE.search(url or "")
    if m:
        return m.group(1)
    m2 = re.search(r"\br/([A-Za-z0-9_]+)\b", title or "")
    return m2.group(1) if m2 else ""


def parse_thread_markdown(raw_title: str, raw_text: str) -> tuple[str, list[dict[str, Any]]]:
    """Split Agent Reach Exa Reddit markdown into (post_body, list_of_comments)."""
    # Strip trailing " : r/subreddit" from search result title
    title = re.sub(r"\s*:\s*r/[A-Za-z0-9_]+\s*$", "", raw_title or "").strip()
    title = clean_text(title)

    # Split at "## Top comments" if present
    parts = re.split(r"(?m)^##\s*Top comments.*$", raw_text or "", maxsplit=1)
    post_section = parts[0] if parts else ""
    comments_section = parts[1] if len(parts) > 1 else ""

    # Clean post section: remove heading line (# Title) and "Posted by ..." metadata line
    post_section = re.sub(r"(?m)^#\s+.*$", "", post_section)
    post_section = _POSTED_BY_LINE_RE.sub("", post_section)
    post_section = re.sub(r"(?m)^---\s*$", "", post_section)
    # Sometimes Exa puts comments directly after "..." without "## Top comments"
    if not comments_section and _COMMENT_HEADER_RE.search(post_section):
        m_first = _COMMENT_HEADER_RE.search(post_section)
        if m_first:
            comments_section = post_section[m_first.start() :]
            post_section = post_section[: m_first.start()]

    post_body = clean_text(post_section)
    post_full = f"{title}. {post_body}".strip(". ").strip() if (title and post_body and post_body.lower() != title.lower()) else (post_body or title)

    # Parse individual comments under "## Top comments"
    comments: list[dict[str, Any]] = []
    if comments_section:
        matches = list(_COMMENT_HEADER_RE.finditer(comments_section))
        for idx, m in enumerate(matches):
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(comments_section)
            chunk = comments_section[start:end]
            chunk = re.sub(r"(?m)^---\s*$", "", chunk)
            c_text = clean_text(chunk)
            if not c_text or len(c_text.split()) < 6:
                continue
            pts_str = m.group(1)
            pts = int(pts_str) if pts_str.lstrip("-").isdigit() else 0
            c_date = m.group(2).strip()
            comments.append({"text": c_text, "upvotes": pts, "raw_date": c_date})

    return post_full, comments


def run_agent_reach_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Invoke `agent-reach reddit search <query> --limit <limit> --backend public-web`."""
    cmd = [
        AGENT_REACH_BIN,
        "reddit",
        "search",
        query,
        "--limit",
        str(limit),
        "--backend",
        "public-web",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    if not payload.get("ok"):
        return []
    return payload.get("items") or []


def collect_reddit_agent_reach(cfg: dict[str, Any]) -> dict[str, Any]:
    """Collect consumer home-service Reddit posts & comments using Agent Reach ($0.00)."""
    proj_cfg = cfg.get("project", {})
    min_words = int(proj_cfg.get("min_words_after_filter", 6))
    exclude_instahelp = bool(proj_cfg.get("exclude_instahelp", True))
    _, insta_pats, native_pats = build_keyword_patterns(cfg)

    seen_urls: set[str] = set()
    raw_threads_pulled = 0
    stock_filtered = 0
    offtopic_filtered = 0
    pre2021_filtered = 0
    instahelp_excluded = 0
    native_flagged = 0

    final_records: list[dict[str, Any]] = []
    seen_record_ids: set[str] = set()
    subreddit_counts: Counter[str] = Counter()
    posts_kept = 0
    comments_kept = 0

    for query in REDDIT_QUERIES:
        items = run_agent_reach_search(query, limit=20)
        for it in items:
            raw_threads_pulled += 1
            url = str(it.get("url") or "").split("?")[0]
            if not url or "/comments/" not in url:
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            raw_title = str(it.get("title") or "")
            raw_text = str(it.get("text") or "")
            published_at = str(it.get("published_at") or "")
            subreddit = extract_subreddit(url, raw_title)

            # Filter out stock/IPO subreddits and pure stock-market threads
            if subreddit.lower() in _STOCK_SUBREDDITS or _STOCK_TITLE_RE.search(raw_title):
                stock_filtered += 1
                continue

            # Keep 2021+ threads when year is known
            if len(published_at) >= 4 and published_at[:4].isdigit() and int(published_at[:4]) < 2021:
                pre2021_filtered += 1
                continue

            post_full, parsed_comments = parse_thread_markdown(raw_title, raw_text)
            if not post_full:
                continue

            # Must be about Urban Company or Indian home services
            combined_check = f"{post_full} " + " ".join(c["text"] for c in parsed_comments[:3])
            if not _CONSUMER_RELEVANCE_RE.search(combined_check):
                offtopic_filtered += 1
                continue

            # Check InstaHelp exclusion at thread level
            post_insta = any(p.search(post_full) for p in insta_pats)
            if exclude_instahelp and post_insta:
                instahelp_excluded += 1
                continue

            post_native = any(p.search(post_full) for p in native_pats)
            post_id = make_item_id("reddit_post", url)

            if len(post_full.split()) >= min_words and post_id not in seen_record_ids:
                seen_record_ids.add(post_id)
                if post_native:
                    native_flagged += 1
                subreddit_counts[subreddit or "unknown"] += 1
                posts_kept += 1
                final_records.append(
                    {
                        "id": post_id,
                        "source": "reddit",
                        "item_type": "post",
                        "subreddit": subreddit,
                        "text": post_full,
                        "context": f"r/{subreddit}" if subreddit else "",
                        "rating": None,
                        "created_at": published_at,
                        "url": url,
                        "is_instahelp": False,
                        "is_native_hardware": post_native,
                    }
                )

            # Add parsed top comments with post as `context`
            ctx_snippet = f"[r/{subreddit}] {post_full[:450]}" if subreddit else post_full[:450]
            for c_idx, c in enumerate(parsed_comments):
                c_text = c["text"]
                c_check = f"{c_text} {ctx_snippet}"
                c_insta = any(p.search(c_check) for p in insta_pats)
                if exclude_instahelp and c_insta:
                    instahelp_excluded += 1
                    continue
                c_native = any(p.search(c_check) for p in native_pats)
                c_id = make_item_id("reddit_comment", f"{url}#{c_idx}:{c_text[:50]}")
                if c_id in seen_record_ids:
                    continue
                seen_record_ids.add(c_id)
                if c_native:
                    native_flagged += 1
                subreddit_counts[subreddit or "unknown"] += 1
                comments_kept += 1
                final_records.append(
                    {
                        "id": c_id,
                        "source": "reddit",
                        "item_type": "comment",
                        "subreddit": subreddit,
                        "text": c_text,
                        "context": ctx_snippet,
                        "rating": None,
                        "upvotes": c["upvotes"],
                        "created_at": published_at,
                        "url": url,
                        "is_instahelp": False,
                        "is_native_hardware": c_native,
                    }
                )

        time.sleep(0.3)

    out_path = save_jsonl(final_records, cfg["sources"]["reddit"]["output_file"])
    return {
        "source": "reddit",
        "method": "agent-reach public-web (Exa, $0.00)",
        "queries_run": len(REDDIT_QUERIES),
        "raw_hits_scanned": raw_threads_pulled,
        "unique_threads_seen": len(seen_urls),
        "stock_ipo_filtered": stock_filtered,
        "offtopic_filtered": offtopic_filtered,
        "pre2021_filtered": pre2021_filtered,
        "instahelp_excluded": instahelp_excluded,
        "native_hardware_flagged": native_flagged,
        "posts_kept": posts_kept,
        "comments_kept": comments_kept,
        "total_kept": len(final_records),
        "top_subreddits": dict(subreddit_counts.most_common(12)),
        "date_range": summarize_dates(final_records),
        "cost_usd": 0.0,
        "output_file": str(out_path),
    }


def main() -> None:
    cfg = load_config()
    stats = collect_reddit_agent_reach(cfg)
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
