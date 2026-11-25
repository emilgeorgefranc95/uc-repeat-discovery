"""Stage 4: Structured AI tagging pipeline using codebook.yaml.

Reads items marked `yes` or `unsure` from data/processed/filtered.jsonl (joined with full
text and context from data/processed/clean.jsonl) and tags every item into the fixed
Urban Company codebook schema.

Features:
- Supports `openai/gpt-oss-120b` and `qwen/qwen3.8-27b` on Groq (separate daily allowances),
  plus optional Gemini fallback.
- Priority-first queue based on the Urban Company brief (repeat/rebooking/same-pro/alternative/
  scope/trigger signals, non-PlayStore sources, and 3★-5★ "nothing went wrong" reviews first).
- Disjoint sharding (`--shard 0/2`, `--shard 1/2`) so two models run in parallel on strictly
  DIFFERENT items with zero overlap, storing `tagged_by`, plus `--merge` to combine shards.
- `--inter-model`: runs `qwen/qwen3.8-27b` on 30 stratified items already tagged by
  `openai/gpt-oss-120b` in `tag_pilot.jsonl` and reports field-by-field agreement.
- Fixed system prompt from codebook.yaml, compact short-code JSON, strict validation
  (retry once then mark failed), auto-split on oversized batches, rapidfuzz quote repair,
  and `--auto-resume`.

Usage:
    python -m pipeline.tag --pilot
    python -m pipeline.tag --inter-model
    python -m pipeline.tag --full --model openai/gpt-oss-120b --shard 0/2 --out data/processed/tagged_gpt120b.jsonl --auto-resume
    python -m pipeline.tag --full --model qwen/qwen3.8-27b --shard 1/2 --out data/processed/tagged_qwen27b.jsonl --auto-resume
    python -m pipeline.tag --merge
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests
import yaml
from dotenv import load_dotenv
from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq, RateLimitError
from rapidfuzz import fuzz

from collect.utils import ROOT_DIR

CLEAN_PATH = ROOT_DIR / "data/processed/clean.jsonl"
FILTERED_PATH = ROOT_DIR / "data/processed/filtered.jsonl"
CODEBOOK_PATH = ROOT_DIR / "codebook.yaml"
PILOT_OUT_PATH = ROOT_DIR / "data/processed/tag_pilot.jsonl"
INTER_MODEL_OUT_PATH = ROOT_DIR / "data/processed/inter_model_check.json"
INTER_MODEL_QWEN_PATH = ROOT_DIR / "data/processed/tag_inter_model_qwen.jsonl"
FULL_OUT_PATH = ROOT_DIR / "data/processed/tagged.jsonl"
SHARD_FILES = [
    ROOT_DIR / "data/processed/tagged_gpt120b.jsonl",
    ROOT_DIR / "data/processed/tagged_qwen27b.jsonl",
]

DEFAULT_MODEL = "openai/gpt-oss-120b"
SECOND_MODEL = "qwen/qwen3.8-27b"
BATCH_SIZE = 20
MAX_TEXT_WORDS = 180
MAX_CONTEXT_WORDS = 35

# Priority keywords aligned strictly to Urban Company's 90-day repeat brief
PRIORITY_SIGNAL_RE = re.compile(
    r"\b("
    r"again|rebook|repeat|second\s+time|first\s+time|regular|every\s+month|monthly|every\s+3\s+months|"
    r"same\s+(?:professional|technician|beautician|cleaner|plumber|electrician|guy|person|staff)|"
    r"preferred|favorite|favourite|another\s+service|other\s+service|different\s+service|multiple\s+services|"
    r"local|offline|whatsapp|neighbour|neighbor|society|mygate|nobroker|yes\s*madam|parlour|parlor|diy|"
    r"switched|stopped\s+using|rarely|occasionally|only\s+use|only\s+for|for\s+years|loyal|"
    r"exclusion|excluded|rate\s+card|after\s+service|post\s+service|follow\s*up|warranty|"
    r"diwali|festive|summer|monsoon|shifting|moving|guests|slot|reschedule|chatbot"
    r")\b",
    re.IGNORECASE,
)

SHORT_CODES: dict[str, dict[str, str]] = {
    "is_relevant": {"y": "yes", "n": "no", "u": "unsure"},
    "service_category": {
        "sal": "salon_grooming",
        "cln": "home_cleaning",
        "ac": "ac_appliance_repair",
        "plm": "plumbing_electrical_carpentry",
        "pst": "pest_control_painting",
        "mul": "multiple_categories",
        "ins": "instahelp_excluded",
        "gen": "general_home_services",
        "u": "unclear",
    },
    "booking_outcome": {
        "ok": "completed_fine",
        "fr": "completed_with_friction",
        "fl": "service_failed_or_cancelled",
        "nb": "never_booked_uc",
        "u": "unclear",
    },
    "repeat_behavior": {
        "rep": "active_repeater",
        "sf": "stopped_despite_fine_experience",
        "sb": "stopped_after_bad_experience",
        "sc": "single_category_or_rare_user",
        "sw": "switched_off_platform",
        "ask": "considering_or_asking",
        "u": "unclear",
    },
    "scope_controllability": {
        "cs": "customer_side_product",
        "sp": "supply_partner_execution",
        "pr": "pricing_or_discount_only",
        "mx": "mixed",
        "u": "unclear",
    },
    "repeat_barriers_or_drivers": {
        "b1": "single_category_mental_model",
        "b2": "low_perceived_frequency_or_no_trigger",
        "b3": "prefers_local_provider_or_maid",
        "b4": "hired_uc_partner_directly_offline",
        "b5": "society_whatsapp_or_neighbour_referral",
        "b6": "cannot_rebook_same_trusted_pro",
        "b7": "unclear_scope_or_surprise_addons",
        "b8": "booking_or_slot_scheduling_friction",
        "b9": "no_post_service_followup_or_care",
        "b10": "trust_safety_or_home_privacy",
        "b11": "perceived_high_price_vs_local",
        "b12": "service_quality_or_partner_failure",
        "d1": "convenience_standardization_driver",
        "d2": "hygiene_and_professionalism_driver",
        "u": "unclear",
    },
    "external_alternative": {
        "loc": "local_technician_or_shop",
        "md": "domestic_help_or_maid",
        "soc": "society_whatsapp_or_neighbour",
        "dir": "direct_uc_partner_offline",
        "diy": "family_or_diy",
        "cmp": "competitor_app",
        "uc": "none_uses_uc",
        "u": "unclear",
    },
    "need_trigger": {
        "em": "emergency_breakdown",
        "sf": "seasonal_or_festival",
        "rt": "routine_maintenance",
        "oc": "special_occasion_or_guests",
        "mv": "moving_or_renovation",
        "u": "unclear",
    },
    "emotion": {
        "del": "delighted",
        "sat": "satisfied",
        "neu": "neutral_pragmatic",
        "cnf": "confused_or_hesitant",
        "fru": "frustrated",
        "dis": "distrustful",
        "u": "unclear",
    },
    "severity": {"1": "1_low", "2": "2_medium", "3": "3_high"},
    "segment_proxy": {
        "bac": "bachelor_or_shared_flat",
        "wrk": "working_couple_or_nuclear_family",
        "fam": "family_with_elders_or_kids",
        "t1": "tier1_metro",
        "t2": "tier2_city",
        "ft": "first_time_user",
        "mb": "multi_booking_user",
        "u": "unclear",
    },
    "brief_questions": {
        "q1": "Q1_post_booking_expectations",
        "q2": "Q2_how_when_need_decided",
        "q3": "Q3_who_turned_to_next",
        "q4": "Q4_why_rebooked_or_not",
        "q5": "Q5_outside_app_alternatives",
        "q6": "Q6_returners_vs_non_returners",
        "q7": "Q7_segment_category_city_diff",
        "q8": "Q8_consistent_unmet_needs",
    },
}

FIELD_KEY_MAP = {
    "r": "is_relevant",
    "cat": "service_category",
    "out": "booking_outcome",
    "rb": "repeat_behavior",
    "sc": "scope_controllability",
    "bar": "repeat_barriers_or_drivers",
    "alt": "external_alternative",
    "trg": "need_trigger",
    "un": "unmet_need",
    "em": "emotion",
    "sev": "severity",
    "seg": "segment_proxy",
    "qs": "brief_questions",
    "q": "evidence_quote",
}

_GROUNDING_CUES: dict[str, re.Pattern[str]] = {
    "hired_uc_partner_directly_offline": re.compile(r"\b(direct|number|offline|outside\s+app|bypass|personal\s+number|call\s+him|call\s+her)\b", re.IGNORECASE),
    "society_whatsapp_or_neighbour_referral": re.compile(r"\b(whatsapp|society|neighbour|neighbor|mygate|nobrokerhood|adda|watchman|guard)\b", re.IGNORECASE),
    "cannot_rebook_same_trusted_pro": re.compile(r"\b(same\s+(?:person|guy|pro|professional|technician|beautician|cleaner|plumber|staff)|book\s+him\s+again|book\s+her\s+again|rebook|preferred|favorite|favourite)\b", re.IGNORECASE),
}


def load_codebook() -> dict[str, Any]:
    with CODEBOOK_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_fixed_system_prompt(codebook: dict[str, Any]) -> str:
    """Build ONE identical, deterministic system prompt embedding codebook.yaml short codes."""
    code_lines = []
    for short_k, full_field in FIELD_KEY_MAP.items():
        if full_field in SHORT_CODES:
            pairs = ", ".join(f"{c}={v}" for c, v in SHORT_CODES[full_field].items())
            multi = " [list, max 3]" if full_field == "repeat_barriers_or_drivers" else (" [list]" if full_field == "brief_questions" else "")
            code_lines.append(f"- {short_k} ({full_field}{multi}): {pairs}")
        elif full_field == "unmet_need":
            code_lines.append('- un (unmet_need): short phrase (<=12 words) naming the unmet customer need, or "unclear"')
        elif full_field == "evidence_quote":
            code_lines.append("- q (evidence_quote): ONE continuous verbatim substring copied EXACTLY from the post text (8-25 words), NO '...' ellipses")

    codebook_block = "\n".join(code_lines)

    return f"""You are a rigorous consumer-research tagger for Urban Company's 90-day repeat-booking study (Codebook v{codebook.get('version', '1.0')}).

CRITICAL RULES:
1. These posts ALREADY passed Stage 3 relevance filtering. Mark r="y" for ANY post about Urban Company or Indian home services — including first-time advice questions, moving/rental inquiries, app/chatbot/help UX friction, local-vs-UC comparisons, and completed or failed bookings. Mark r="n" ONLY for spam, comedy jokes, or totally unrelated topics.
2. NEVER blank out fields with "u" when r="n" or when a post is a question. Relevance ("r") is a flag only: ALWAYS fill EVERY field based on what the text explicitly states OR plainly implies. Use "u" ONLY when the text genuinely gives zero clue for that specific field.
3. "sev" (severity) ONLY accepts "1", "2", or "3" — NEVER output "u" for sev! For every relevant post (r="y"), always include 1-3 codes in "qs" (q1..q8) — do NOT leave "qs" empty.
4. Semantic precision:
   - Use rb="sf" (stopped_despite_fine_experience) ONLY when the service itself went fine (out="ok"). When the booking failed/cancelled or had major partner friction (out="fl"/"fr"), use rb="sb" (stopped_after_bad_experience) or rb="sw" (switched_off_platform).
   - Map daily maid / househelp / InstaHelp bookings to cat="ins", NEVER cat="sal".
   - Use sc="cs" (customer_side_product) for app bugs, AI chatbot loops/no human help, unclear scope/exclusions/rate card, slot/reschedule control, unable to rebook same pro, or trust/discovery questions. Use sc="sp" for technician no-show, skill, damage, or rudeness.
5. Quote ("q") MUST be ONE continuous verbatim span copied character-for-character from the post text (8-25 words, never from context, NEVER using "...").
6. Return ONLY valid compact JSON: {{"items": [{{...}}, ...]}} using the short codes below. Include EVERY item number "n".

SHORT-CODE CODEBOOK:
{codebook_block}

WORKED EXAMPLES (distinct item numbers):
Input:
[1] [play_store] (2★) The next staff member assigned to me was very good, and I've been trying to book him again, but he is never available. Classic Bathroom Cleaning has also been removed.
[2] [reddit] (no-rating) (ctx: r/bangalore cockroach infestation) I used urban company pest control last year. Their cockroach treatment wasn't effective so I bought advion gel on Amazon and apply it myself every few months.
[3] [play_store] (3★) In full house deep cleaning highest package cupboard cleaning is not there, at least mention it in exclusion list. Weekly bathroom cleaning price is ok so giving 3 stars.
[4] [reddit] (no-rating) (ctx: r/hyderabad) Moving into a rental apartment and thinking to get UC bathroom deep cleaning. First time using them, is it worth it and what should I check?
[5] [play_store] (1★) The AI chatbot can't answer my query and there is no option to reach a human when my issue isn't listed in the app Help section.

Output:
{{"items": [
  {{"n":1,"r":"y","cat":"cln","out":"ok","rb":"sf","sc":"cs","bar":["b6","b8"],"alt":"u","trg":"rt","un":"See availability of a previously trusted cleaner","em":"fru","sev":"3","seg":"mb","qs":["q3","q4","q8"],"q":"I've been trying to book him again, but he is never available"}},
  {{"n":2,"r":"y","cat":"pst","out":"fl","rb":"sw","sc":"sp","bar":["b12"],"alt":"diy","trg":"rt","un":"Effective long-lasting pest treatment","em":"dis","sev":"3","seg":"t1","qs":["q3","q4","q5"],"q":"Their cockroach treatment wasn't effective so I bought advion gel on Amazon"}},
  {{"n":3,"r":"y","cat":"mul","out":"fr","rb":"rep","sc":"mx","bar":["b7","b11"],"alt":"uc","trg":"rt","un":"Clear upfront inclusion and exclusion checklist before booking","em":"neu","sev":"2","seg":"mb","qs":["q1","q4","q8"],"q":"at least mention it in exclusion list. Weekly bathroom cleaning price is ok"}},
  {{"n":4,"r":"y","cat":"cln","out":"nb","rb":"ask","sc":"cs","bar":["b10","b7"],"alt":"u","trg":"mv","un":"Clear expectations and trust proof before first deep clean","em":"cnf","sev":"2","seg":"ft","qs":["q2","q7","q8"],"q":"Moving into a rental apartment and thinking to get UC bathroom deep cleaning"}},
  {{"n":5,"r":"y","cat":"gen","out":"fr","rb":"u","sc":"cs","bar":["b9","b8"],"alt":"u","trg":"u","un":"Resolve unlisted issues outside rigid AI chatbot menu","em":"fru","sev":"2","seg":"u","qs":["q1","q8"],"q":"there is no option to reach a human when my issue isn't listed"}}
]}}

Return ONLY valid JSON matching this schema."""


def truncate_words(text: str | None, max_words: int) -> str:
    if not text:
        return ""
    words = str(text).split()
    return " ".join(words[:max_words]) if len(words) > max_words else " ".join(words)


def build_user_prompt(batch: list[dict[str, Any]]) -> str:
    lines = ["Tag every numbered item using ONLY the short codes:"]
    for idx, rec in enumerate(batch, 1):
        src = rec.get("source", "unknown")
        rating = f"{rec['rating']}★" if rec.get("rating") is not None else "no-rating"
        ctx = truncate_words(rec.get("context", ""), MAX_CONTEXT_WORDS)
        txt = truncate_words(rec.get("text", ""), MAX_TEXT_WORDS)
        ctx_part = f" (ctx: {ctx})" if ctx else ""
        lines.append(f"[{idx}] [{src}] ({rating}){ctx_part} {txt}")
    return "\n".join(lines)


def extract_json_items(content: str) -> list[dict[str, Any]]:
    """Strip optional <think>...</think> blocks and markdown fences, then parse JSON."""
    raw = re.sub(r"<think>.*?</think>", "", content or "", flags=re.DOTALL | re.IGNORECASE).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    # Fallback: locate outermost JSON object if extra prose surrounds it
    if not raw.startswith("{") and not raw.startswith("["):
        m = re.search(r"(\{.*\}|\[.*\])", raw, flags=re.DOTALL)
        if m:
            raw = m.group(1)
    data = json.loads(raw)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(data, list):
        return data
    raise ValueError("Unexpected JSON format")


def verify_and_repair_quote(quote: str, full_text: str) -> tuple[str, str, float]:
    """Free quote verification: exact -> repaired (rapidfuzz >= 80) -> hidden (< 80)."""
    q = (quote or "").strip().strip('"').strip("'")
    if not q or not full_text:
        return "", "hidden_low_score", 0.0

    q_clean = re.sub(r"\.{3,}|…", " ", q).strip()
    if not q_clean:
        return "", "hidden_low_score", 0.0

    if q_clean in full_text:
        return q_clean, "exact", 100.0
    idx_ci = full_text.lower().find(q_clean.lower())
    if idx_ci != -1:
        return full_text[idx_ci : idx_ci + len(q_clean)], "exact", 100.0

    align = fuzz.partial_ratio_alignment(q_clean.lower(), full_text.lower())
    if align is None:
        return "", "hidden_low_score", 0.0

    score = float(align.score)
    if score < 80.0:
        return "", "hidden_low_score", round(score, 1)

    start = align.dest_start
    end = align.dest_end
    while start > 0 and full_text[start - 1].isalnum():
        start -= 1
    while end < len(full_text) and full_text[end].isalnum():
        end += 1

    repaired = full_text[start:end].strip()
    if not repaired:
        return "", "hidden_low_score", round(score, 1)
    return repaired, "repaired", round(score, 1)


def decode_and_validate_item(
    raw_obj: dict[str, Any],
    codebook: dict[str, Any],
    full_text: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Decode short codes to full codebook values and strictly validate every field."""
    errors: list[str] = []
    decoded: dict[str, Any] = {}
    cb_fields = codebook["fields"]

    for short_k, full_field in FIELD_KEY_MAP.items():
        val = raw_obj.get(short_k, raw_obj.get(full_field))
        field_spec = cb_fields[full_field]

        if full_field == "unmet_need":
            s = str(val or "unclear").strip()
            decoded[full_field] = " ".join(s.split()[:12]) if s else "unclear"
            continue

        if full_field == "evidence_quote":
            q_str = str(val or "").strip()
            fixed_q, q_status, q_score = verify_and_repair_quote(q_str, full_text)
            decoded["evidence_quote"] = fixed_q
            decoded["quote_status"] = q_status
            decoded["quote_score"] = q_score
            continue

        allowed = set(field_spec["values"])
        code_map = SHORT_CODES[full_field]

        if field_spec.get("multi_select"):
            if isinstance(val, str):
                val_list = [v.strip() for v in val.split(",") if v.strip()]
            elif isinstance(val, list):
                val_list = [str(v).strip() for v in val if str(v).strip()]
            else:
                val_list = []

            mapped_list: list[str] = []
            for entry in val_list:
                el = entry.lower()
                mapped = code_map.get(el, entry if entry in allowed else None)
                if mapped is None:
                    errors.append(f"{full_field}: invalid value '{entry}'")
                elif mapped not in mapped_list:
                    mapped_list.append(mapped)

            max_items = field_spec.get("max_items")
            if max_items and len(mapped_list) > max_items:
                mapped_list = mapped_list[:max_items]
            if not mapped_list and full_field == "repeat_barriers_or_drivers":
                mapped_list = ["unclear"]
            decoded[full_field] = mapped_list
        else:
            s = str(val if val is not None else "").strip()
            sl = s.lower()
            mapped = code_map.get(sl, s if s in allowed else None)
            if mapped is None:
                errors.append(f"{full_field}: invalid value '{s}'")
            else:
                decoded[full_field] = mapped

    if errors:
        return None, errors

    g_flags = []
    for bar in decoded.get("repeat_barriers_or_drivers", []):
        cue = _GROUNDING_CUES.get(bar)
        if cue and not cue.search(full_text):
            g_flags.append(f"unverified_cue:{bar}")
    decoded["grounding_flags"] = g_flags
    return decoded, []


def parse_reset_seconds(val: str | None) -> float:
    if not val:
        return 60.0
    s = str(val).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    total = 0.0
    for amt, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", s):
        num = float(amt)
        total += num * (3600 if unit == "h" else 60 if unit == "m" else 1 if unit == "s" else 0.001)
    return total if total > 0 else 60.0


def call_model_batch(
    provider: str,
    client_or_key: Any,
    model_id: str,
    system_prompt: str,
    batch: list[dict[str, Any]],
    auto_resume: bool = False,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any], bool, bool]:
    """Call LLM for a batch. Returns (items_or_None, meta, hit_daily_limit, needs_split)."""
    user_prompt = build_user_prompt(batch)
    max_retries = 12

    for attempt in range(1, max_retries + 1):
        try:
            if provider == "groq":
                kwargs: dict[str, Any] = {
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.0,
                    "max_completion_tokens": 4096,
                    "response_format": {"type": "json_object"},
                }
                if "gpt-oss" in model_id:
                    kwargs["reasoning_effort"] = "low"
                elif "qwen" in model_id:
                    kwargs["reasoning_effort"] = "none"
                raw_resp = client_or_key.chat.completions.with_raw_response.create(**kwargs)
                headers = {k.lower(): v for k, v in raw_resp.headers.items()}
                completion = raw_resp.parse()
                content = completion.choices[0].message.content or ""
                usage = completion.usage
                meta = {
                    "prompt_tokens": int(usage.prompt_tokens or 0) if usage else 0,
                    "completion_tokens": int(usage.completion_tokens or 0) if usage else 0,
                    "total_tokens": int(usage.total_tokens or 0) if usage else 0,
                    "ratelimit_limit_tokens": headers.get("x-ratelimit-limit-tokens"),
                    "ratelimit_remaining_tokens": headers.get("x-ratelimit-remaining-tokens"),
                    "ratelimit_remaining_requests": headers.get("x-ratelimit-remaining-requests"),
                    "ratelimit_reset_tokens": headers.get("x-ratelimit-reset-tokens"),
                }
                return extract_json_items(content), meta, False, False

            else:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
                resp = requests.post(
                    url,
                    headers={"Content-Type": "application/json", "x-goog-api-key": client_or_key},
                    json={
                        "systemInstruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                        "generationConfig": {
                            "temperature": 0.0,
                            "maxOutputTokens": 6144,
                            "responseMimeType": "application/json",
                        },
                    },
                    timeout=120,
                )
                if resp.status_code == 429:
                    err_t = resp.text[:300]
                    is_daily = "perday" in err_t.lower() or "daily" in err_t.lower()
                    if is_daily and not auto_resume:
                        return None, {"error": err_t}, True, False
                    time.sleep(65.0 if not is_daily else 600.0)
                    continue
                if resp.status_code >= 500:
                    wait_s = min(10.0 * attempt, 60.0)
                    print(f"[gemini-{resp.status_code}] Server unavailable (attempt {attempt}/{max_retries}); sleeping {wait_s:.0f}s...", flush=True)
                    time.sleep(wait_s)
                    continue
                resp.raise_for_status()
                body = resp.json()
                parts = ((body.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
                content = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
                usage = body.get("usageMetadata") or {}
                meta = {
                    "prompt_tokens": int(usage.get("promptTokenCount") or 0),
                    "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
                    "total_tokens": int(usage.get("totalTokenCount") or 0),
                }
                return extract_json_items(content), meta, False, False

        except RateLimitError as e:
            err_msg = str(e)
            resp_headers = {k.lower(): v for k, v in e.response.headers.items()} if getattr(e, "response", None) else {}
            if "request too large" in err_msg.lower() or (
                "requested" in err_msg.lower() and len(batch) > 1
            ):
                m_req = re.search(r"Requested\s+(\d+)", err_msg)
                m_lim = re.search(r"Limit\s+(\d+)", err_msg)
                if m_req and m_lim and int(m_req.group(1)) > int(m_lim.group(1)) * 0.9:
                    return None, {"error": err_msg}, False, True

            retry_after = parse_reset_seconds(
                resp_headers.get("retry-after")
                or resp_headers.get("x-ratelimit-reset-tokens")
                or resp_headers.get("x-ratelimit-reset-requests")
            )
            is_daily = (
                "tokens per day" in err_msg.lower()
                or "requests per day" in err_msg.lower()
                or "tpd" in err_msg.lower()
                or "rpd" in err_msg.lower()
                or retry_after > 180
            )
            if is_daily:
                if auto_resume:
                    wait_s = min(retry_after + 5, 3600)
                    print(f"[tag-limit:{model_id}] Daily limit reached; --auto-resume sleeping {wait_s:.0f}s...", flush=True)
                    time.sleep(wait_s)
                    continue
                return None, {"error": err_msg, "retry_after_sec": retry_after}, True, False

            wait_s = min(max(retry_after + 2.0, 15.0), 65.0)
            print(f"[tag-limit:{model_id}] TPM limit (attempt {attempt}/{max_retries}); sleeping {wait_s:.1f}s...", flush=True)
            time.sleep(wait_s)

        except APIStatusError as e:
            err_s = str(e)
            # If model rejects reasoning_effort="none", retry without reasoning_effort
            if e.status_code == 400 and "reasoning_effort" in err_s.lower() and "qwen" in model_id:
                try:
                    raw_resp = client_or_key.chat.completions.with_raw_response.create(
                        model=model_id,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        temperature=0.0,
                        max_completion_tokens=4096,
                        response_format={"type": "json_object"},
                    )
                    headers = {k.lower(): v for k, v in raw_resp.headers.items()}
                    completion = raw_resp.parse()
                    content = completion.choices[0].message.content or ""
                    usage = completion.usage
                    meta = {
                        "prompt_tokens": int(usage.prompt_tokens or 0) if usage else 0,
                        "completion_tokens": int(usage.completion_tokens or 0) if usage else 0,
                        "total_tokens": int(usage.total_tokens or 0) if usage else 0,
                        "ratelimit_limit_tokens": headers.get("x-ratelimit-limit-tokens"),
                        "ratelimit_remaining_tokens": headers.get("x-ratelimit-remaining-tokens"),
                        "ratelimit_remaining_requests": headers.get("x-ratelimit-remaining-requests"),
                        "ratelimit_reset_tokens": headers.get("x-ratelimit-reset-tokens"),
                    }
                    return extract_json_items(content), meta, False, False
                except Exception:
                    pass
            if e.status_code in (400, 413) and len(batch) > 1:
                return None, {"error": err_s[:200]}, False, True
            if attempt == max_retries:
                raise
            time.sleep(min(4.0 * attempt, 25.0))

        except (APIConnectionError, APITimeoutError, requests.RequestException) as e:
            if attempt == max_retries:
                raise
            time.sleep(min(4.0 * attempt, 25.0))

        except (json.JSONDecodeError, ValueError) as e:
            if len(batch) > 1 and attempt >= 2:
                return None, {"error": f"JSON error: {str(e)[:120]}"}, False, True
            if attempt == max_retries:
                return [], {"error": f"JSON parse failed: {str(e)[:120]}"}, False, False
            time.sleep(3.0)

    return None, {"error": "Max retries exceeded"}, True, False


def tag_batch_recursive(
    provider: str,
    client_or_key: Any,
    model_id: str,
    system_prompt: str,
    codebook: dict[str, Any],
    batch: list[dict[str, Any]],
    auto_resume: bool,
    token_acc: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """Tag a batch with automatic split-in-half, missing-item retry, and single validation retry."""
    parsed, meta, hit_daily, needs_split = call_model_batch(
        provider, client_or_key, model_id, system_prompt, batch, auto_resume=auto_resume
    )
    if hit_daily:
        return [], meta, True

    if needs_split and len(batch) > 1:
        mid = len(batch) // 2
        print(f"[tag-split:{model_id}] Splitting batch of {len(batch)} -> {mid} + {len(batch) - mid}", flush=True)
        left_out, left_meta, left_daily = tag_batch_recursive(
            provider, client_or_key, model_id, system_prompt, codebook, batch[:mid], auto_resume, token_acc
        )
        if left_daily:
            return left_out, left_meta, True
        right_out, right_meta, right_daily = tag_batch_recursive(
            provider, client_or_key, model_id, system_prompt, codebook, batch[mid:], auto_resume, token_acc
        )
        return left_out + right_out, right_meta, right_daily

    token_acc["prompt"] += int(meta.get("prompt_tokens", 0))
    token_acc["completion"] += int(meta.get("completion_tokens", 0))
    token_acc["total"] += int(meta.get("total_tokens", 0))

    if provider == "groq":
        rem_tok = meta.get("ratelimit_remaining_tokens")
        lim_tok = meta.get("ratelimit_limit_tokens")
        threshold = int(int(lim_tok) * 0.55) if (lim_tok and str(lim_tok).isdigit()) else 4500
        if rem_tok is not None and str(rem_tok).isdigit() and int(rem_tok) < threshold:
            wait_s = parse_reset_seconds(meta.get("ratelimit_reset_tokens"))
            time.sleep(min(wait_s + 2.0, 62.0))
        else:
            time.sleep(1.0)
    else:
        time.sleep(4.2)

    by_n: dict[int, dict[str, Any]] = {}
    for obj in (parsed or []):
        if not isinstance(obj, dict):
            continue
        n_val = obj.get("n")
        if isinstance(n_val, int) and 1 <= n_val <= len(batch) and n_val not in by_n:
            by_n[n_val] = obj

    results: list[dict[str, Any]] = []
    retry_queue: list[tuple[dict[str, Any], str]] = []

    for idx, rec in enumerate(batch, 1):
        raw_obj = by_n.get(idx)
        if raw_obj is None:
            retry_queue.append((rec, "missing_from_reply"))
            continue
        decoded, errs = decode_and_validate_item(raw_obj, codebook, rec["text"])
        if decoded is None:
            retry_queue.append((rec, "; ".join(errs)))
            continue
        results.append(_build_tagged_record(rec, decoded, model_id, "ok", retried=False))

    if retry_queue:
        retry_items = [r for r, _ in retry_queue]
        reason_map = {r["id"]: why for r, why in retry_queue}
        print(f"[tag-retry:{model_id}] Retrying {len(retry_items)} missing/invalid item(s) once...", flush=True)
        r_parsed, r_meta, r_daily, _ = call_model_batch(
            provider, client_or_key, model_id, system_prompt, retry_items, auto_resume=auto_resume
        )
        if r_daily:
            return results, r_meta, True

        token_acc["prompt"] += int(r_meta.get("prompt_tokens", 0))
        token_acc["completion"] += int(r_meta.get("completion_tokens", 0))
        token_acc["total"] += int(r_meta.get("total_tokens", 0))

        r_by_n: dict[int, dict[str, Any]] = {}
        for pos, obj in enumerate(r_parsed or [], 1):
            if not isinstance(obj, dict):
                continue
            nv = obj.get("n")
            if isinstance(nv, int) and 1 <= nv <= len(retry_items) and nv not in r_by_n:
                r_by_n[nv] = obj
            elif pos not in r_by_n and pos <= len(retry_items):
                r_by_n[pos] = obj

        for idx, rec in enumerate(retry_items, 1):
            raw_obj = r_by_n.get(idx)
            if raw_obj is not None:
                decoded, errs = decode_and_validate_item(raw_obj, codebook, rec["text"])
                if decoded is not None:
                    results.append(_build_tagged_record(rec, decoded, model_id, "ok", retried=True))
                    continue
                fail_err = "; ".join(errs)
            else:
                fail_err = f"still_missing_after_retry ({reason_map[rec['id']]})"
            results.append(_build_failed_record(rec, model_id, fail_err))

        meta = r_meta

    return results, meta, False


def _build_tagged_record(
    rec: dict[str, Any], decoded: dict[str, Any], model_id: str, status: str, retried: bool
) -> dict[str, Any]:
    return {
        "id": rec["id"],
        "source": rec.get("source"),
        "year": rec.get("year"),
        "rating": rec.get("rating"),
        "language": rec.get("language"),
        "is_native_hardware": rec.get("is_native_hardware", False),
        "filter_relevant": rec.get("filter_relevant"),
        "priority_item": bool(rec.get("priority_item", False)),
        "tagged_by": model_id,
        "tag_status": status,
        "retried": retried,
        **decoded,
    }


def _build_failed_record(rec: dict[str, Any], model_id: str, error_msg: str) -> dict[str, Any]:
    return {
        "id": rec["id"],
        "source": rec.get("source"),
        "year": rec.get("year"),
        "rating": rec.get("rating"),
        "language": rec.get("language"),
        "is_native_hardware": rec.get("is_native_hardware", False),
        "filter_relevant": rec.get("filter_relevant"),
        "priority_item": bool(rec.get("priority_item", False)),
        "tagged_by": model_id,
        "tag_status": "failed",
        "retried": True,
        "validation_error": error_msg,
    }


def load_eligible_items() -> list[dict[str, Any]]:
    """Join clean.jsonl with filtered.jsonl (if available) and return eligible items with priority flag."""
    clean_map: dict[str, dict[str, Any]] = {}
    with CLEAN_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                clean_map[r["id"]] = r

    eligible: list[dict[str, Any]] = []
    if FILTERED_PATH.exists():
        with FILTERED_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                fr = json.loads(line)
                if fr.get("relevant") not in {"yes", "unsure"}:
                    continue
                cr = clean_map.get(fr["id"])
                if not cr:
                    continue
                is_prio = bool(PRIORITY_SIGNAL_RE.search(cr.get("text", "")))
                eligible.append({**cr, "filter_relevant": fr["relevant"], "priority_item": is_prio})
    else:
        for cr in clean_map.values():
            is_prio = bool(PRIORITY_SIGNAL_RE.search(cr.get("text", "")))
            eligible.append({**cr, "filter_relevant": "unfiltered", "priority_item": is_prio})
    return eligible


def sort_priority_queue(eligible: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Priority items first based on the Urban Company brief:
    1) 2021+ priority-signal items across Reddit/AppStore/YouTube + 3★-5★ Play Store
    2) 2021+ remaining priority-signal items (1★-2★ Play Store)
    3) 2021+ non-priority items
    4) Pre-2021 items
    """
    def _rank(r: dict[str, Any]) -> tuple[int, int, int, int, str]:
        yr = int(r.get("year") or 0)
        is_recent = 0 if yr >= 2021 else 1
        is_prio = 0 if r.get("priority_item") else 1
        src = r.get("source", "")
        rating = r.get("rating")
        # Elevate non-PlayStore sources & 3★-5★ "nothing went wrong" reviews inside priority tier
        if src != "play_store":
            src_tier = 0
        elif rating is not None and int(rating) >= 3:
            src_tier = 1
        else:
            src_tier = 2
        return (is_recent, is_prio, src_tier, -yr, r["id"])

    return sorted(eligible, key=_rank)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def append_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def select_pilot_50(eligible: list[dict[str, Any]], seed: int = 42) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    by_src: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in eligible:
        by_src[str(r.get("source", "unknown"))].append(r)

    targets = {"play_store": 22, "app_store": 6, "reddit": 14, "youtube": 8}
    sample: list[dict[str, Any]] = []
    picked: set[str] = set()
    for src, target in targets.items():
        pool = by_src.get(src, [])
        for r in rng.sample(pool, min(target, len(pool))):
            sample.append(r)
            picked.add(r["id"])
    if len(sample) < 50:
        rem = [r for r in eligible if r["id"] not in picked]
        sample.extend(rng.sample(rem, min(50 - len(sample), len(rem))))
    rng.shuffle(sample)
    return sample[:50]


def run_inter_model_check() -> dict[str, Any]:
    """Compare openai/gpt-oss-120b vs qwen/qwen3.8-27b on the exact same 30 stratified items."""
    load_dotenv(ROOT_DIR / ".env")
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY missing from .env")
    client = Groq(api_key=api_key)
    codebook = load_codebook()
    system_prompt = build_fixed_system_prompt(codebook)

    pilot_120b = [r for r in load_jsonl(PILOT_OUT_PATH) if r.get("tag_status") == "ok"]
    if len(pilot_120b) < 30:
        raise RuntimeError("Run --pilot first so tag_pilot.jsonl has >= 30 valid gpt-oss-120b rows.")

    clean_map = {r["id"]: r for r in load_jsonl(CLEAN_PATH)}

    # Pick 30 stratified items from pilot_120b across all 4 sources
    by_src: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in pilot_120b:
        by_src[str(r.get("source"))].append(r)
    chosen_120b = (
        by_src["play_store"][:12]
        + by_src["reddit"][:9]
        + by_src["app_store"][:5]
        + by_src["youtube"][:4]
    )[:30]
    chosen_map_120b = {r["id"]: r for r in chosen_120b}

    batch_inputs = [
        {**clean_map[r["id"]], "filter_relevant": r.get("filter_relevant"), "priority_item": False}
        for r in chosen_120b
        if r["id"] in clean_map
    ]

    if INTER_MODEL_QWEN_PATH.exists():
        INTER_MODEL_QWEN_PATH.unlink()

    token_acc = {"prompt": 0, "completion": 0, "total": 0}
    qwen_results: list[dict[str, Any]] = []
    # Run in chunks of 15 for Qwen stability & TPM comfort
    for i in range(0, len(batch_inputs), 15):
        chunk = batch_inputs[i : i + 15]
        res, _, hit_daily = tag_batch_recursive(
            "groq", client, SECOND_MODEL, system_prompt, codebook, chunk, auto_resume=False, token_acc=token_acc
        )
        qwen_results.extend(res)
        if hit_daily:
            break

    append_jsonl(qwen_results, INTER_MODEL_QWEN_PATH)
    qwen_ok = {r["id"]: r for r in qwen_results if r.get("tag_status") == "ok"}

    paired_ids = [rid for rid in chosen_map_120b if rid in qwen_ok]
    single_fields = [
        "is_relevant",
        "service_category",
        "booking_outcome",
        "repeat_behavior",
        "scope_controllability",
        "external_alternative",
        "need_trigger",
        "emotion",
        "severity",
        "segment_proxy",
    ]
    agreement: dict[str, dict[str, Any]] = {}
    for fld in single_fields:
        exact = sum(1 for rid in paired_ids if chosen_map_120b[rid].get(fld) == qwen_ok[rid].get(fld))
        agreement[fld] = {
            "exact_match": exact,
            "total": len(paired_ids),
            "pct": round(exact / max(1, len(paired_ids)) * 100, 1),
        }

    # Multi-select agreement (overlap >= 1 item AND average Jaccard)
    for mfld in ("repeat_barriers_or_drivers", "brief_questions"):
        any_overlap = 0
        jaccard_sum = 0.0
        for rid in paired_ids:
            s1 = set(chosen_map_120b[rid].get(mfld) or [])
            s2 = set(qwen_ok[rid].get(mfld) or [])
            inter = len(s1 & s2)
            union = max(1, len(s1 | s2))
            if inter > 0:
                any_overlap += 1
            jaccard_sum += inter / union
        agreement[mfld] = {
            "overlap_ge_1": any_overlap,
            "overlap_pct": round(any_overlap / max(1, len(paired_ids)) * 100, 1),
            "avg_jaccard_pct": round((jaccard_sum / max(1, len(paired_ids))) * 100, 1),
            "total": len(paired_ids),
        }

    core_fields = ["is_relevant", "service_category", "booking_outcome", "repeat_behavior", "scope_controllability", "emotion"]
    avg_core_pct = round(sum(agreement[f]["pct"] for f in core_fields) / len(core_fields), 1)

    report = {
        "model_a": DEFAULT_MODEL,
        "model_b": SECOND_MODEL,
        "items_compared": len(paired_ids),
        "qwen_valid": len(qwen_ok),
        "qwen_failed": len(qwen_results) - len(qwen_ok),
        "qwen_tokens": token_acc["total"],
        "qwen_quote_status": dict(Counter(r.get("quote_status") for r in qwen_ok.values())),
        "avg_core_field_agreement_pct": avg_core_pct,
        "field_by_field_agreement": agreement,
    }
    INTER_MODEL_OUT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def run_merge() -> dict[str, Any]:
    """Merge disjoint shard files (+ any existing tagged.jsonl) into data/processed/tagged.jsonl without duplicates."""
    seen: dict[str, dict[str, Any]] = {}
    for p in [*SHARD_FILES, FULL_OUT_PATH]:
        if not p.exists():
            continue
        for r in load_jsonl(p):
            rid = r["id"]
            if rid not in seen or (seen[rid].get("tag_status") != "ok" and r.get("tag_status") == "ok"):
                seen[rid] = r

    merged = list(seen.values())
    FULL_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FULL_OUT_PATH.open("w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_model = Counter(r.get("tagged_by") for r in merged)
    by_status = Counter(r.get("tag_status") for r in merged)
    summary = {
        "merged_total": len(merged),
        "by_model": dict(by_model),
        "by_status": dict(by_status),
        "output_file": str(FULL_OUT_PATH),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def run_tag(
    mode: str,
    provider: str,
    model_id: str,
    auto_resume: bool,
    out_override: str | None = None,
    shard: str | None = None,
) -> dict[str, Any]:
    load_dotenv(ROOT_DIR / ".env")
    if provider == "groq":
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY missing from .env")
        client_or_key: Any = Groq(api_key=api_key)
    else:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY missing from .env")
        client_or_key = api_key

    codebook = load_codebook()
    system_prompt = build_fixed_system_prompt(codebook)
    eligible = load_eligible_items()

    if mode == "pilot":
        out_path = Path(out_override) if out_override else PILOT_OUT_PATH
        if out_path.exists():
            out_path.unlink()
        queue = select_pilot_50(eligible, seed=42)
    else:
        out_path = Path(out_override) if out_override else FULL_OUT_PATH
        queue = sort_priority_queue(eligible)
        if shard:
            idx_s, total_s = shard.split("/")
            shard_idx, shard_total = int(idx_s), int(total_s)
            queue = [r for i, r in enumerate(queue) if i % shard_total == shard_idx]

    # Never tag an item already present in this output file OR in any other shard / merged file
    done_ids: set[str] = set()
    for check_p in {out_path, FULL_OUT_PATH, *SHARD_FILES}:
        if check_p.exists():
            for r in load_jsonl(check_p):
                done_ids.add(r["id"])

    pending = [r for r in queue if r["id"] not in done_ids]
    print(
        f"[tag:{model_id}] mode={mode} shard={shard or 'all'} | queue={len(queue)} | "
        f"already_done={len(queue) - len(pending)} | pending={len(pending)} | "
        f"priority_pending={sum(1 for r in pending if r.get('priority_item'))}",
        flush=True,
    )

    token_acc = {"prompt": 0, "completion": 0, "total": 0}
    stopped_daily = False
    last_meta: dict[str, Any] = {}
    done_in_queue = len(queue) - len(pending)

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i : i + BATCH_SIZE]
        batch_results, meta, hit_daily = tag_batch_recursive(
            provider, client_or_key, model_id, system_prompt, codebook, batch, auto_resume, token_acc
        )
        last_meta = meta
        if batch_results:
            append_jsonl(batch_results, out_path)
            done_in_queue += len(batch_results)
        print(
            f"[tag:{model_id}] done {done_in_queue}/{len(queue)} | tokens={token_acc['total']:,}",
            flush=True,
        )
        if hit_daily:
            stopped_daily = True
            break

    all_tagged = load_jsonl(out_path)
    ok_rows = [r for r in all_tagged if r.get("tag_status") == "ok"]
    fail_rows = [r for r in all_tagged if r.get("tag_status") != "ok"]
    summary = {
        "mode": mode,
        "model": model_id,
        "shard": shard or "all",
        "processed_in_file": len(all_tagged),
        "ok_count": len(ok_rows),
        "failed_count": len(fail_rows),
        "stopped_daily_limit": stopped_daily,
        "tokens": token_acc,
        "output_file": str(out_path),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 4 structured AI tagger")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pilot", action="store_true", help="Run 50-item stratified pilot only")
    group.add_argument("--inter-model", action="store_true", help="Compare gpt-oss-120b vs qwen3.8-27b on 30 items")
    group.add_argument("--full", action="store_true", help="Run full priority-sorted queue (optionally sharded)")
    group.add_argument("--merge", action="store_true", help="Merge disjoint shard outputs into tagged.jsonl")
    parser.add_argument("--provider", choices=["groq", "gemini"], default="groq")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--shard", default=None, help="Disjoint shard e.g. 0/2 or 1/2")
    parser.add_argument("--out", default=None, help="Custom output JSONL path")
    parser.add_argument("--auto-resume", action="store_true", help="Wait for daily reset and continue")
    args = parser.parse_args()

    if args.inter_model:
        run_inter_model_check()
    elif args.merge:
        run_merge()
    else:
        run_tag("pilot" if args.pilot else "full", args.provider, args.model, args.auto_resume, args.out, args.shard)


if __name__ == "__main__":
    main()
