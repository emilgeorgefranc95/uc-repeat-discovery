"""Shared utilities for Stage 1 collectors: config, PII scrubbing, keyword filtering, JSONL I/O."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.yaml"

# Patterns for scrubbing PII before any text is written to disk
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?91[\s\-]?)?(?:[6-9]\d{9}|[6-9]\d{4}[\s\-]\d{5}|[6-9]\d{2}[\s\-]\d{3}[\s\-]\d{4})(?!\d)"
)
_HANDLE_RE = re.compile(r"(?<!\w)(?:@[A-Za-z0-9_.-]{2,}|u/[A-Za-z0-9_-]{2,})")
_WHITESPACE_RE = re.compile(r"\s+")

_NON_KEEP_KEYWORD_GROUPS = {"instahelp_terms", "native_hardware_terms"}


def load_config() -> dict[str, Any]:
    """Load config.yaml from the project root."""
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def scrub_pii(text: str | None) -> str:
    """Remove emails, Indian phone numbers, and social/Reddit handles."""
    if not text:
        return ""
    cleaned = _EMAIL_RE.sub("[email]", text)
    cleaned = _PHONE_RE.sub("[phone]", cleaned)
    cleaned = _HANDLE_RE.sub("[user]", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


def make_item_id(source: str, raw_key: str) -> str:
    """Create a deterministic anonymous ID without exposing usernames."""
    digest = hashlib.sha256(f"{source}:{raw_key}".encode("utf-8")).hexdigest()[:16]
    return f"{source}_{digest}"


def build_keyword_patterns(
    cfg: dict[str, Any],
) -> tuple[list[tuple[str, re.Pattern[str]]], list[re.Pattern[str]], list[re.Pattern[str]]]:
    """Compile case-insensitive word-boundary regexes from all keyword groups in config.yaml."""
    kw_cfg = cfg.get("keywords", {})
    seen_terms: set[str] = set()
    keep_terms: list[str] = []

    for group_name, terms in kw_cfg.items():
        if group_name in _NON_KEEP_KEYWORD_GROUPS or not isinstance(terms, list):
            continue
        for term in terms:
            norm = str(term).strip().lower()
            if norm and norm not in seen_terms:
                seen_terms.add(norm)
                keep_terms.append(norm)

    instahelp_terms = [str(t).strip().lower() for t in kw_cfg.get("instahelp_terms", []) if t]
    native_terms = [str(t).strip().lower() for t in kw_cfg.get("native_hardware_terms", []) if t]

    keep_patterns = [
        (term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
        for term in keep_terms
    ]
    instahelp_patterns = [
        re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        for term in instahelp_terms
    ]
    native_patterns = [
        re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        for term in native_terms
    ]
    return keep_patterns, instahelp_patterns, native_patterns


def evaluate_text(
    text: str,
    keep_patterns: list[tuple[str, re.Pattern[str]]],
    instahelp_patterns: list[re.Pattern[str]],
    native_patterns: list[re.Pattern[str]] | None = None,
    min_words: int = 6,
    context: str = "",
    exclude_instahelp: bool = True,
) -> tuple[bool, list[str], bool, bool]:
    """Evaluate length + keyword filter; exclude InstaHelp if enabled; flag Native hardware."""
    words = text.split()
    combined = f"{text} {context}".strip() if context else text
    is_instahelp = any(p.search(combined) for p in instahelp_patterns)
    is_native_hardware = any(p.search(combined) for p in (native_patterns or []))

    if exclude_instahelp and is_instahelp:
        return False, [], True, is_native_hardware

    if len(words) < min_words:
        return False, [], is_instahelp, is_native_hardware

    matched = [term for term, pat in keep_patterns if pat.search(text)]
    kept = len(matched) > 0
    return kept, matched, is_instahelp, is_native_hardware


def save_jsonl(records: list[dict[str, Any]], output_path: str | Path) -> Path:
    """Write records as newline-delimited JSON, creating parent folders if needed."""
    path = Path(output_path)
    if not path.is_absolute():
        path = ROOT_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def summarize_dates(records: list[dict[str, Any]]) -> str:
    """Return 'YYYY-MM-DD to YYYY-MM-DD' across records with valid ISO dates."""
    dates = sorted(str(r.get("created_at", ""))[:10] for r in records if r.get("created_at"))
    dates = [d for d in dates if len(d) == 10 and d[4] == "-"]
    if not dates:
        return "unknown"
    return f"{dates[0]} to {dates[-1]}"
