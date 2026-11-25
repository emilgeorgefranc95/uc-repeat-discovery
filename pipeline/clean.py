"""Stage 2: Deterministic cleaning & deduplication pipeline (no AI, plain code only).

1. Merges all raw JSONL files in data/raw/ into one dataset.
2. Filters empty texts, spam/promotional links, official Urban Company staff replies,
   and any residual InstaHelp items.
3. Removes exact and near-duplicate texts using rapidfuzz.
4. Drops items under 6 words UNLESS they contain a configured Urban Company keyword.
5. Adds fields: `year`, `language` (en / hi / hinglish / other), and normalized `context`.
6. Saves kept records to data/processed/clean.jsonl and every dropped item with its
   reason to data/processed/dropped.jsonl.

Usage:
    python -m pipeline.clean
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from collect.utils import ROOT_DIR, build_keyword_patterns, load_config

RAW_DIR = ROOT_DIR / "data/raw"
CLEAN_PATH = ROOT_DIR / "data/processed/clean.jsonl"
DROPPED_PATH = ROOT_DIR / "data/processed/dropped.jsonl"

NEAR_DUP_THRESHOLD = 92.0

# Normalization helpers
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\u0900-\u097f\u0980-\u0dff\s]+", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

# Platform staff / official brand reply detection (Stage 2 Watch-Out rule)
_STAFF_REPLY_RE = re.compile(
    r"(\bteam\s+urban\s*(?:company|clap)\b|"
    r"\burban\s*company\s+(?:support|team|care)\s+here\b|"
    r"\bwe\s+(?:sincerely\s+)?apolog(?:ize|ise)\s+for\s+the\s+inconvenience\b.*\b(?:dm|email|reach\s+out|contact|booking\s*id|registered\s+number)\b|"
    r"\bplease\s+(?:share|dm|send)\s+your\s+(?:booking\s*id|registered\s+(?:mobile|phone|number)|contact\s+details)\b|"
    r"\bthank\s+you\s+for\s+(?:your\s+feedback|bringing\s+this\s+to\s+our\s+attention)\b.*\b(?:urban\s*company|team|escalat)\b)",
    re.IGNORECASE,
)

# Promotional spam detection (e.g., YouTube/Reddit self-promo, referral codes, phone-only ads)
_SPAM_RE = re.compile(
    r"(\b(?:subscribe\s+(?:to\s+)?my\s+channel|check\s+out\s+my\s+channel|click\s+(?:the\s+)?link\s+in\s+bio|"
    r"earn\s+money\s+online|work\s+from\s+home\s+earn|whatsapp\s+me\s+on|call\s+for\s+best\s+offer|"
    r"use\s+my\s+referral\s+code|dm\s+for\s+paid\s+promo|buy\s+followers)\b)",
    re.IGNORECASE,
)

# Unicode script ranges for rule-based language detection
_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
_OTHER_INDIC_OR_NONLATIN_RE = re.compile(
    r"[\u0980-\u0DFF\u0600-\u06FF\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]"
)
_LATIN_RE = re.compile(r"[A-Za-z]")

# Unambiguous Romanized Hindi / Hinglish markers (avoiding English words like 'the', 'hi', 'do', 'par')
_HINGLISH_WORDS = {
    "hai", "hain", "nahi", "nhi", "naa", "bahut", "bhot", "bohot", "kya", "kyu", "kyun",
    "kyunki", "karo", "karte", "karna", "karke", "kiya", "kiye", "kar", "mera", "meri", "mere",
    "apna", "apni", "apne", "aap", "aapka", "aapki", "tum", "tumhara", "bhai", "bhaiya", "yaar",
    "didi", "paise", "paisa", "rupaye", "bekar", "bakwas", "ghatiya", "achha", "accha", "acha",
    "bilkul", "mat", "toh", "bhi", "sab", "log", "wala", "wali", "wale", "pehle", "abhi",
    "phir", "fir", "kuch", "koi", "ghar", "kaam", "band", "chalu", "gaya", "gayi", "gaye",
    "raha", "rahi", "rahe", "hota", "hoti", "hote", "tha", "thi", "liye", "aur", "lekin",
    "agar", "sirf", "bas", "ekdum", "zyada", "jyada", "kam", "samajh", "baat", "batao",
    "chahiye", "chahie", "bol", "bola", "bolo", "bolte", "deti", "deta", "dete", "diya",
    "liya", "leke", "lagta", "lagti", "laga", "lagi", "mujhko", "mujhe", "hum", "humne",
    "maine", "unhone", "unka", "unki", "unke", "yaha", "waha", "idhar", "udhar", "kaise",
    "kab", "kahan", "kitna", "kitne", "sahi", "galat", "theek", "thik", "dono", "rha", "rhi",
}

# Romanized Malayalam / Tamil / South-Indian markers so Manglish/Tanglish maps cleanly to 'other'
_NON_HINDI_ROMANIZED_WORDS = {
    "aanu", "alla", "enthu", "enth", "nalla", "veed", "veedu", "cheyyan", "cheytha",
    "kittum", "kndu", "kandu", "ellrum", "ellarum", "ith", "ithu", "adhym", "padiku",
    "swanthm", "thudappum", "ullil", "keralathil", "makkale", "kalakki", "polichu",
    "pannunga", "irukku", "romba", "nalla", "illa", "enna", "epdi", "bro", "macha",
}


def normalize_for_dedupe(text: str) -> str:
    """Lowercase, strip URLs/punctuation/extra spaces for duplicate comparison."""
    s = _URL_RE.sub(" ", text.lower())
    s = _NON_ALNUM_RE.sub(" ", s)
    return _SPACE_RE.sub(" ", s).strip()


def extract_year(created_at: str | None) -> int | None:
    """Extract 4-digit year from ISO or human date strings."""
    if not created_at:
        return None
    m = re.search(r"\b(20\d{2})\b", str(created_at))
    return int(m.group(1)) if m else None


def detect_language(text: str) -> str:
    """Rule-based language classifier returning one of: en, hi, hinglish, other."""
    if not text:
        return "other"
    dev_chars = len(_DEVANAGARI_RE.findall(text))
    other_script_chars = len(_OTHER_INDIC_OR_NONLATIN_RE.findall(text))
    latin_chars = len(_LATIN_RE.findall(text))
    total_alpha = dev_chars + other_script_chars + latin_chars

    if total_alpha == 0:
        return "other"
    if other_script_chars >= 5 and other_script_chars / total_alpha >= 0.15:
        return "other"
    if dev_chars >= 5 and dev_chars / total_alpha >= 0.15:
        return "hi"

    tokens = re.findall(r"[a-z]+", text.lower())
    if not tokens:
        return "other"

    south_hits = sum(1 for t in tokens if t in _NON_HINDI_ROMANIZED_WORDS)
    if south_hits >= 2:
        return "other"

    hinglish_hits = sum(1 for t in tokens if t in _HINGLISH_WORDS)
    if hinglish_hits >= 2 or (len(tokens) <= 10 and hinglish_hits >= 1 and hinglish_hits / len(tokens) >= 0.15):
        return "hinglish"

    if latin_chars / total_alpha >= 0.6:
        return "en"
    return "other"


def word_count_clean(text: str) -> int:
    """Count meaningful word tokens (ignoring bare URLs and pure emojis/punctuation)."""
    no_url = _URL_RE.sub(" ", text)
    tokens = [t for t in re.findall(r"[\w\u0900-\u0DFF]+", no_url) if not t.isdigit()]
    return len(tokens)


def normalize_context(rec: dict[str, Any]) -> str:
    """Ensure forum/Reddit replies keep parent post question and YouTube comments keep video title."""
    src = rec.get("source", "")
    ctx = str(rec.get("context") or "").strip()
    if src == "youtube":
        return ctx  # Video title captured during Stage 1 collection
    if src == "reddit":
        # Comments already hold "[r/sub] <post title & body>"; posts hold "r/sub"
        return ctx
    return ctx


def load_raw_records() -> list[dict[str, Any]]:
    """Merge all .jsonl files in data/raw/ (excluding intermediate shortlist/candidate files)."""
    records: list[dict[str, Any]] = []
    skip_names = {"reddit_posts_candidates.jsonl", "reddit_shortlist.jsonl"}
    for path in sorted(RAW_DIR.glob("*.jsonl")):
        if path.name in skip_names or path.stat().st_size == 0:
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                rec.setdefault("source", path.stem)
                records.append(rec)
    return records


def save_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run_clean() -> dict[str, Any]:
    cfg = load_config()
    keep_patterns, insta_patterns, native_patterns = build_keyword_patterns(cfg)
    exclude_instahelp = bool(cfg.get("project", {}).get("exclude_instahelp", True))

    raw_records = load_raw_records()

    # Funnel trackers per source
    funnel: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "raw": 0,
            "dropped_empty_spam_staff_insta": 0,
            "after_preclean": 0,
            "dropped_exact_dup": 0,
            "dropped_near_dup": 0,
            "after_dedupe": 0,
            "dropped_short": 0,
            "after_length_filter": 0,
        }
    )

    dropped_records: list[dict[str, Any]] = []
    precleaned: list[dict[str, Any]] = []

    # Step 1: Pre-clean (empty, spam, platform staff replies, residual InstaHelp)
    for rec in raw_records:
        src = str(rec.get("source") or "unknown")
        funnel[src]["raw"] += 1

        text = str(rec.get("text") or "").strip()
        context = normalize_context(rec)
        rec["context"] = context

        if not text:
            funnel[src]["dropped_empty_spam_staff_insta"] += 1
            dropped_records.append({**rec, "drop_reason": "empty_text"})
            continue

        if _STAFF_REPLY_RE.search(text):
            funnel[src]["dropped_empty_spam_staff_insta"] += 1
            dropped_records.append({**rec, "drop_reason": "platform_staff_reply"})
            continue

        if _SPAM_RE.search(text):
            funnel[src]["dropped_empty_spam_staff_insta"] += 1
            dropped_records.append({**rec, "drop_reason": "spam_or_promo"})
            continue

        combined_check = f"{text} {context}".strip()
        is_insta = bool(rec.get("is_instahelp")) or any(p.search(combined_check) for p in insta_patterns)
        if exclude_instahelp and is_insta:
            funnel[src]["dropped_empty_spam_staff_insta"] += 1
            dropped_records.append({**rec, "drop_reason": "instahelp_excluded"})
            continue

        rec["is_instahelp"] = False
        if "is_native_hardware" not in rec:
            rec["is_native_hardware"] = any(p.search(combined_check) for p in native_patterns)

        funnel[src]["after_preclean"] += 1
        precleaned.append(rec)

    # Step 2: Exact and near-duplicate removal (rapidfuzz, bucketed for fast O(N) performance)
    seen_exact: dict[str, str] = {}
    # Bucket by (source, length_bucket, first_token) to keep near-dup comparisons fast & accurate
    near_buckets: dict[tuple[str, int, str], list[tuple[str, str]]] = defaultdict(list)
    deduped: list[dict[str, Any]] = []

    for rec in precleaned:
        src = str(rec.get("source") or "unknown")
        norm = normalize_for_dedupe(rec["text"])

        if not norm:
            funnel[src]["dropped_exact_dup"] += 1
            dropped_records.append({**rec, "drop_reason": "empty_after_normalization"})
            continue

        if norm in seen_exact:
            funnel[src]["dropped_exact_dup"] += 1
            dropped_records.append(
                {**rec, "drop_reason": "exact_duplicate", "duplicate_of": seen_exact[norm]}
            )
            continue

        # Check near-duplicates in neighboring length buckets sharing the same first word
        words = norm.split()
        first_tok = words[0] if words else ""
        len_bucket = len(norm) // 25
        matched_dup_id: str | None = None
        matched_score = 0.0

        for lb in (len_bucket - 1, len_bucket, len_bucket + 1):
            for existing_id, existing_norm in near_buckets[(src, lb, first_tok)]:
                # Quick length ratio guard before fuzz.ratio
                if abs(len(norm) - len(existing_norm)) > max(12, int(0.15 * max(len(norm), len(existing_norm)))):
                    continue
                score = fuzz.ratio(norm, existing_norm)
                if score >= NEAR_DUP_THRESHOLD:
                    matched_dup_id = existing_id
                    matched_score = score
                    break
            if matched_dup_id:
                break

        if matched_dup_id:
            funnel[src]["dropped_near_dup"] += 1
            dropped_records.append(
                {
                    **rec,
                    "drop_reason": "near_duplicate",
                    "duplicate_of": matched_dup_id,
                    "similarity_score": round(matched_score, 1),
                }
            )
            continue

        seen_exact[norm] = rec["id"]
        near_buckets[(src, len_bucket, first_tok)].append((rec["id"], norm))
        funnel[src]["after_dedupe"] += 1
        deduped.append(rec)

    # Step 3: Length filter (< 6 words dropped UNLESS it contains an Urban Company keyword from config.yaml)
    # + Enrich with year & language
    cleaned: list[dict[str, Any]] = []
    short_kept_by_kw = 0
    year_counts: Counter[str] = Counter()
    lang_counts: Counter[str] = Counter()
    drop_reason_counts: Counter[str] = Counter()

    for rec in deduped:
        src = str(rec.get("source") or "unknown")
        text = rec["text"]
        wc = word_count_clean(text)
        has_uc_kw = any(pat.search(text) for _, pat in keep_patterns)

        if wc < 6 and not has_uc_kw:
            funnel[src]["dropped_short"] += 1
            dropped_records.append({**rec, "drop_reason": "too_short_under_6_words", "word_count": wc})
            continue

        if wc < 6 and has_uc_kw:
            short_kept_by_kw += 1

        year = extract_year(rec.get("created_at"))
        lang = detect_language(text)

        enriched = {
            **rec,
            "year": year,
            "language": lang,
            "word_count": wc,
            "short_kept_by_keyword": wc < 6 and has_uc_kw,
        }
        cleaned.append(enriched)
        funnel[src]["after_length_filter"] += 1
        year_counts[str(year) if year is not None else "unknown"] += 1
        lang_counts[lang] += 1

    for d in dropped_records:
        drop_reason_counts[d["drop_reason"]] += 1

    save_jsonl(cleaned, CLEAN_PATH)
    save_jsonl(dropped_records, DROPPED_PATH)

    # Sample 5 random dropped short items (and also 5 random dropped items overall if <5 short)
    rng = random.Random(42)
    dropped_short_pool = [d for d in dropped_records if d["drop_reason"] == "too_short_under_6_words"]
    dropped_short_sample = rng.sample(dropped_short_pool, min(5, len(dropped_short_pool)))
    dropped_other_pool = [d for d in dropped_records if d["drop_reason"] != "too_short_under_6_words"]
    dropped_other_sample = rng.sample(dropped_other_pool, min(5, len(dropped_other_pool)))

    totals = {
        k: sum(v[k] for v in funnel.values())
        for k in (
            "raw",
            "dropped_empty_spam_staff_insta",
            "after_preclean",
            "dropped_exact_dup",
            "dropped_near_dup",
            "after_dedupe",
            "dropped_short",
            "after_length_filter",
        )
    }

    summary = {
        "funnel_by_source": dict(sorted(funnel.items())),
        "totals": totals,
        "short_kept_by_keyword": short_kept_by_kw,
        "drop_reasons": dict(drop_reason_counts.most_common()),
        "counts_by_year": dict(sorted(year_counts.items())),
        "counts_by_language": dict(lang_counts.most_common()),
        "dropped_short_total": len(dropped_short_pool),
        "dropped_short_sample": [
            {"source": d.get("source"), "wc": d.get("word_count"), "text": d.get("text")}
            for d in dropped_short_sample
        ],
        "dropped_other_sample": [
            {"source": d.get("source"), "reason": d.get("drop_reason"), "text": str(d.get("text", ""))[:180]}
            for d in dropped_other_sample
        ],
        "clean_file": str(CLEAN_PATH),
        "dropped_file": str(DROPPED_PATH),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    run_clean()
