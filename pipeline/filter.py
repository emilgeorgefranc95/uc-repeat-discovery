"""Stage 3: AI relevance filter supporting Groq and Gemini (defaulting to gemini-3.1-flash-lite).

Classifies each cleaned post as `yes` / `no` / `unsure` with a <=10-word reason
grounded strictly in the text, plus a deterministic safety net (`rescued_by_rule`)
that upgrades borderline customer-side/alternative posts from `no` to `unsure`.

Features:
- 25 items per request; text truncated to 150 words, context to 25 words.
- Supports `--provider gemini --model gemini-3.1-flash-lite` or `--provider groq --model openai/gpt-oss-20b`.
- Resumable by item `id` (skips completed IDs); handles 429/5xx retries and daily resets
  when `--auto-resume` is passed.
- Pilot mode (`--pilot`): stratified random sample of 100 items across all sources.
- Full mode (`--full`): prioritizes 2021+ items first, then older items.

Usage:
    python -m pipeline.filter --pilot --provider gemini --model gemini-3.1-flash-lite
    python -m pipeline.filter --full --provider gemini --model gemini-3.1-flash-lite --auto-resume
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
from dotenv import load_dotenv
from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq, RateLimitError

from collect.utils import ROOT_DIR

CLEAN_PATH = ROOT_DIR / "data/processed/clean.jsonl"
PILOT_OUT_PATH = ROOT_DIR / "data/processed/filter_pilot.jsonl"
FULL_OUT_PATH = ROOT_DIR / "data/processed/filtered.jsonl"

DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
BATCH_SIZE = 25
MAX_TEXT_WORDS = 150
MAX_CONTEXT_WORDS = 25

SYSTEM_PROMPT = """You are a strict, evidence-based classifier for an Urban Company Consumer Growth research study.
Goal: keep ONLY posts that help understand customer-side repeat/non-repeat behavior, home-service decision triggers, post-booking expectations/scope clarity, outside-app alternatives, or app/slot/rebooking friction.

IMPORTANT: Simply being about Urban Company is NOT enough for "yes". Most 1-star complaints about bad technicians, no-shows, damaged appliances, redos, or Native RO hardware defects MUST be labelled "no".

For each numbered item, return a JSON object:
{"items": [{"n": <item_number>, "relevant": "yes"|"no"|"unsure", "reason": "<max 10 words describing ONLY what the text states>"}]}

Classification rules:
- "yes": CLEARLY describes at least one of these customer-side signals (in English or Hinglish):
  * trying to rebook the same trusted professional, or using/avoiding multiple service categories
  * why they return regularly OR stop/rarely book even when nothing went wrong with service quality
  * how/when they decide a home service is needed (e.g. Diwali, summer, guests, shifting, monthly routine, first-time trial intent)
  * outside-app alternatives or comparisons (local technician/plumber/electrician/parlour/shop/maid, budget company vs UC, society WhatsApp, MyGate/NoBroker, Yes Madam, DIY)
  * post-booking scope/exclusion clarity gaps (e.g. kitchen fan or cupboards excluded without notice, rate card confusion)
  * city or household-type contrast (e.g. Mumbai vs Gurgaon, bachelor, elderly parents)
  * customer-side app UX, search/navigation bug, slot availability, or involuntary rescheduling control friction
- "no":
  * PURE supply/partner execution failures (technician late, no-show, cancelled, didn't bring kit, rude, poor cleaning, broke AC/stove/washing machine, redo request, refund fight) with NO repeat-preference, scope-exclusion, or alternative-provider signal
  * PURE Native RO water purifier or Native Smart Lock hardware purchase, installation delay, or hardware defect/after-sales complaint
  * PURE price-only complaints ("too expensive", "high charges") with no behavioral or alternative context
  * Off-topic YouTube vlog/comedy/US "$15,000" cleaning comments, competitor self-ads, gig-worker salary/stock chatter, or generic praise ("good service, polite staff")
- "unsure":
  * MIXED posts combining a partner/price/support complaint WITH a customer-side signal (e.g., overpriced + cleaners never available; bad service + mentions trying to rebook or switching)
  * Short or ambiguous Hinglish/English comments hinting at trying, comparing, or avoiding home services. When in doubt between "no" and "unsure", choose "unsure".

Worked examples (distinct item numbers):
Input:
[1] [play_store] (2★) Got a great cleaner last month and tried to book him again, but app shows no way to see his available slots.
[2] [play_store] (1★) Technician arrived 2 hours late, didn't bring kit, damaged my washing machine and now no one is refunding me.
[3] [play_store] (1★) Bought Native RO water purifier, installation delayed 3 days and after-sales support is pathetic.
[4] [youtube] (ctx: Urban Clap House Cleaning) M bhi is diwali pr try karugi urban..
[5] [play_store] (3★) Chimney cleaning price is high, and in deep cleaning highest package cupboards weren't included; at least mention it in exclusions. Weekly bathroom clean is ok.
[6] [app_store] (4★) Price increases every month and quality isn't improving. Also majority of times cleaners are not available when needed.

Output:
{"items": [
  {"n": 1, "relevant": "yes", "reason": "Wants to rebook trusted cleaner but lacks slot visibility"},
  {"n": 2, "relevant": "no", "reason": "Pure partner lateness, appliance damage, and refund complaint"},
  {"n": 3, "relevant": "no", "reason": "Pure Native RO hardware installation and support complaint"},
  {"n": 4, "relevant": "yes", "reason": "Hinglish Diwali seasonal trigger and first-time trial intent"},
  {"n": 5, "relevant": "unsure", "reason": "Mixes high price with unclear deep-clean exclusion list"},
  {"n": 6, "relevant": "unsure", "reason": "Mixes price gripe with recurring cleaner slot unavailability"}
]}

Return ONLY valid JSON matching the schema above. Include EVERY item number from the input."""

# Safety-net regexes (English + Hinglish customer-side / repeat / outside-app signals)
_FIRST_PERSON_OR_Q_RE = re.compile(
    r"(\?|\b(?:i|i'm|i've|i'd|my|we|we've|our|me|anyone|how|should|worth|"
    r"m|mai|main|mein|hum|mera|meri|mere|apka|aapka|apna|karugi|karunga)\b)",
    re.IGNORECASE,
)
_RESCUE_SIGNAL_RE = re.compile(
    r"\b("
    r"rebook|repeat|book\s+again|use\s+again|booking\s+again|try\s+kar|"
    r"same\s+(?:professional|technician|beautician|cleaner|plumber|electrician|guy|person)|"
    r"local\s+(?:plumber|electrician|mechanic|technician|tech|parlour|parlor|maid|guy|shop|shops|market|service|vendor|repair)|"
    r"budget\s+company|urban\s+se|vs\s+urban|"
    r"whatsapp|neighbour|neighbor|society|mygate|nobroker|yes\s*madam|parlour|parlor|diy|"
    r"alternative|switched\s+(?:to|back)|another\s+service|other\s+service|different\s+service|"
    r"exclusion|excluded|rate\s+card|first\s+time|regular\s+customer|every\s+month|monthly|"
    r"diwali|festive|summer|monsoon|shifting|moving|slot\s+availab|not\s+getting\s+slot|"
    r"not\s+available|app\s+navigation|can't\s+find|cannot\s+find|couldn't\s+find|preferred|"
    r"warranty|change\s+(?:location|address)|earlier\s+it\s+was|last\s+year|notification"
    r")\b",
    re.IGNORECASE,
)


def truncate_words(text: str | None, max_words: int) -> str:
    if not text:
        return ""
    words = str(text).split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words])


def parse_reset_seconds(val: str | None) -> float:
    """Parse duration strings like '12m34.5s', '45s', '1h2m3s', or numeric seconds."""
    if not val:
        return 60.0
    s = str(val).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    total = 0.0
    for amt, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", s):
        num = float(amt)
        if unit == "h":
            total += num * 3600
        elif unit == "m":
            total += num * 60
        elif unit == "s":
            total += num
        elif unit == "ms":
            total += num / 1000.0
    return total if total > 0 else 60.0


def apply_safety_net(text: str, relevant: str) -> tuple[str, bool]:
    """Upgrade 'no' -> 'unsure' if text has strong customer-side repeat/alternative signal + first-person/question."""
    if relevant != "no":
        return relevant, False
    if _FIRST_PERSON_OR_Q_RE.search(text) and _RESCUE_SIGNAL_RE.search(text):
        return "unsure", True
    return relevant, False


def build_user_prompt(batch: list[dict[str, Any]]) -> str:
    lines = ["Classify each item below:"]
    for idx, rec in enumerate(batch, 1):
        src = rec.get("source", "unknown")
        rating = f"{rec['rating']}★" if rec.get("rating") is not None else "no-rating"
        ctx = truncate_words(rec.get("context", ""), MAX_CONTEXT_WORDS)
        txt = truncate_words(rec.get("text", ""), MAX_TEXT_WORDS)
        ctx_part = f" (ctx: {ctx})" if ctx else ""
        lines.append(f"[{idx}] [{src}] ({rating}){ctx_part} {txt}")
    return "\n".join(lines)


def extract_json_payload(content: str) -> list[dict[str, Any]]:
    """Parse JSON response robustly, handling optional markdown fences."""
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
    data = json.loads(raw)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    if isinstance(data, list):
        return data
    raise ValueError("Unexpected JSON structure from model")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def append_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def select_pilot_sample(all_items: list[dict[str, Any]], n: int = 100, seed: int = 42) -> list[dict[str, Any]]:
    """Stratified random sample of 100 items across all 4 sources."""
    rng = random.Random(seed)
    by_src: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in all_items:
        by_src[str(r.get("source", "unknown"))].append(r)

    targets = {"play_store": 45, "app_store": 15, "reddit": 20, "youtube": 20}
    sample: list[dict[str, Any]] = []
    picked_ids: set[str] = set()

    for src, target in targets.items():
        pool = by_src.get(src, [])
        take = min(target, len(pool))
        for r in rng.sample(pool, take):
            sample.append(r)
            picked_ids.add(r["id"])

    if len(sample) < n:
        remaining = [r for r in all_items if r["id"] not in picked_ids]
        sample.extend(rng.sample(remaining, min(n - len(sample), len(remaining))))

    rng.shuffle(sample)
    return sample[:n]


def sort_for_full_run(all_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Prioritize 2021+ items first (non-PlayStore + newest Play Store), then pre-2021."""
    recent = [r for r in all_items if (r.get("year") or 0) >= 2021]
    older = [r for r in all_items if (r.get("year") or 0) < 2021]
    recent.sort(key=lambda r: (0 if r.get("source") != "play_store" else 1, -(r.get("year") or 0)))
    older.sort(key=lambda r: -(r.get("year") or 0))
    return recent + older


def call_batch_groq(
    client: Groq,
    model_id: str,
    batch: list[dict[str, Any]],
    auto_resume: bool = False,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any], bool]:
    """Call Groq for one batch. Returns (parsed_items_or_None, meta, hit_daily_limit)."""
    user_prompt = build_user_prompt(batch)
    max_retries = 6

    for attempt in range(1, max_retries + 1):
        try:
            raw_resp = client.chat.completions.with_raw_response.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_completion_tokens=4096,
                reasoning_effort="low",
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
                "ratelimit_limit_requests": headers.get("x-ratelimit-limit-requests"),
                "ratelimit_remaining_requests": headers.get("x-ratelimit-remaining-requests"),
                "ratelimit_reset_tokens": headers.get("x-ratelimit-reset-tokens"),
                "ratelimit_reset_requests": headers.get("x-ratelimit-reset-requests"),
            }
            parsed = extract_json_payload(content)
            return parsed, meta, False

        except RateLimitError as e:
            err_msg = str(e)
            resp_headers = {}
            if getattr(e, "response", None) is not None:
                resp_headers = {k.lower(): v for k, v in e.response.headers.items()}

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
                    wait_sec = min(retry_after + 5, 3600)
                    print(f"[rate-limit] Daily limit reached. --auto-resume sleeping {wait_sec:.0f}s...", flush=True)
                    time.sleep(wait_sec)
                    continue
                return None, {"error": err_msg, "retry_after_sec": retry_after, "headers": resp_headers}, True

            sleep_s = min(max(retry_after, 2.0 * attempt), 65.0)
            print(f"[rate-limit] Minute limit hit (attempt {attempt}/{max_retries}); sleeping {sleep_s:.1f}s...", flush=True)
            time.sleep(sleep_s)

        except (APIConnectionError, APITimeoutError, APIStatusError, json.JSONDecodeError, ValueError) as e:
            if attempt == max_retries:
                raise
            sleep_s = min(3.0 * attempt, 20.0)
            print(f"[retry] {type(e).__name__} on attempt {attempt}/{max_retries}: {str(e)[:120]}; retrying in {sleep_s:.0f}s...", flush=True)
            time.sleep(sleep_s)

    return None, {"error": "Max retries exceeded"}, True


def call_batch_gemini(
    api_key: str,
    model_id: str,
    batch: list[dict[str, Any]],
    auto_resume: bool = False,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any], bool]:
    """Call Gemini REST API (`generateContent`) for one batch. Never logs the API key."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": build_user_prompt(batch)}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }

    max_retries = 8
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=90)
            if resp.status_code == 429:
                err_text = resp.text[:400]
                is_daily = (
                    "perday" in err_text.lower()
                    or "per_day" in err_text.lower()
                    or "daily" in err_text.lower()
                    or "generaterequestsperday" in err_text.lower()
                )
                retry_match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_text, re.IGNORECASE)
                retry_s = float(retry_match.group(1)) if retry_match else (60.0 if not is_daily else 900.0)
                if is_daily:
                    if auto_resume:
                        wait_s = min(max(retry_s, 300.0), 3600.0)
                        print(f"[gemini-429] Daily quota reached; --auto-resume sleeping {wait_s:.0f}s...", flush=True)
                        time.sleep(wait_s)
                        continue
                    return None, {"error": err_text, "retry_after_sec": retry_s}, True
                wait_s = min(max(retry_s + 2.0, 10.0 * attempt), 65.0)
                print(f"[gemini-429] Minute limit (attempt {attempt}/{max_retries}); sleeping {wait_s:.1f}s...", flush=True)
                time.sleep(wait_s)
                continue

            if resp.status_code >= 500:
                wait_s = min(4.0 * attempt, 30.0)
                print(f"[gemini-{resp.status_code}] Server error (attempt {attempt}/{max_retries}); retrying in {wait_s:.0f}s...", flush=True)
                time.sleep(wait_s)
                continue

            resp.raise_for_status()
            body = resp.json()
            candidates = body.get("candidates") or []
            if not candidates:
                raise ValueError(f"Empty Gemini candidates: {str(body)[:200]}")
            parts = (candidates[0].get("content") or {}).get("parts") or []
            content = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
            usage = body.get("usageMetadata") or {}
            meta = {
                "prompt_tokens": int(usage.get("promptTokenCount") or 0),
                "completion_tokens": int(usage.get("candidatesTokenCount") or 0),
                "total_tokens": int(usage.get("totalTokenCount") or 0),
            }
            parsed = extract_json_payload(content)
            return parsed, meta, False

        except (requests.RequestException, json.JSONDecodeError, ValueError) as e:
            if attempt == max_retries:
                raise
            wait_s = min(4.0 * attempt, 25.0)
            print(f"[gemini-retry] {type(e).__name__} (attempt {attempt}/{max_retries}): {str(e)[:120]}; retrying in {wait_s:.0f}s...", flush=True)
            time.sleep(wait_s)

    return None, {"error": "Max retries exceeded"}, True


def run_filter(mode: str, provider: str, model_id: str, auto_resume: bool = False) -> dict[str, Any]:
    load_dotenv(ROOT_DIR / ".env")
    groq_client: Groq | None = None
    gemini_key: str | None = None

    if provider == "gemini":
        gemini_key = os.getenv("GEMINI_API_KEY")
        if not gemini_key:
            raise RuntimeError("GEMINI_API_KEY is missing from .env")
    else:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is missing from .env")
        groq_client = Groq(api_key=api_key)

    all_clean = load_jsonl(CLEAN_PATH)

    if mode == "pilot":
        out_path = PILOT_OUT_PATH
        if out_path.exists():
            out_path.unlink()
        queue = select_pilot_sample(all_clean, n=100, seed=42)
    else:
        out_path = FULL_OUT_PATH
        queue = sort_for_full_run(all_clean)

    existing = load_jsonl(out_path)
    done_ids = {r["id"] for r in existing}
    pending = [r for r in queue if r["id"] not in done_ids]

    print(
        f"[filter] Starting mode={mode} provider={provider} model={model_id} | "
        f"already_done={len(done_ids)} | pending={len(pending)} | total={len(queue)}",
        flush=True,
    )

    total_tokens = 0
    prompt_tokens = 0
    completion_tokens = 0
    last_headers: dict[str, Any] = {}
    stopped_daily = False
    batches_run = 0

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i : i + BATCH_SIZE]
        if provider == "gemini":
            assert gemini_key is not None
            parsed, meta, hit_daily = call_batch_gemini(gemini_key, model_id, batch, auto_resume=auto_resume)
        else:
            assert groq_client is not None
            parsed, meta, hit_daily = call_batch_groq(groq_client, model_id, batch, auto_resume=auto_resume)

        if hit_daily or parsed is None:
            stopped_daily = True
            last_headers.update(meta)
            break

        batches_run += 1
        total_tokens += meta["total_tokens"]
        prompt_tokens += meta["prompt_tokens"]
        completion_tokens += meta["completion_tokens"]
        last_headers = meta

        by_n: dict[int, dict[str, Any]] = {}
        for pos, obj in enumerate(parsed, 1):
            n_val = obj.get("n")
            if isinstance(n_val, int) and 1 <= n_val <= len(batch) and n_val not in by_n:
                by_n[n_val] = obj
            elif pos not in by_n and pos <= len(batch):
                by_n[pos] = obj

        batch_out: list[dict[str, Any]] = []
        for idx, rec in enumerate(batch, 1):
            res = by_n.get(idx)
            if not res:
                rel_raw = "unsure"
                reason = "Model omitted item; defaulted to unsure"
            else:
                rel_raw = str(res.get("relevant", "unsure")).strip().lower()
                if rel_raw not in {"yes", "no", "unsure"}:
                    rel_raw = "unsure"
                reason_words = str(res.get("reason", "")).strip().split()[:10]
                reason = " ".join(reason_words)

            rel_final, rescued = apply_safety_net(rec.get("text", ""), rel_raw)
            batch_out.append(
                {
                    "id": rec["id"],
                    "source": rec.get("source"),
                    "year": rec.get("year"),
                    "rating": rec.get("rating"),
                    "language": rec.get("language"),
                    "is_native_hardware": rec.get("is_native_hardware", False),
                    "relevant": rel_final,
                    "relevant_raw": rel_raw,
                    "rescued_by_rule": rescued,
                    "reason": reason,
                    "model": model_id,
                }
            )

        append_jsonl(batch_out, out_path)
        done_ids.update(r["id"] for r in batch_out)

        if mode == "full" and (batches_run == 1 or batches_run % 10 == 0 or len(done_ids) == len(queue)):
            print(
                f"[filter:{provider}] batch {batches_run} | done {len(done_ids)}/{len(queue)} | "
                f"tokens={total_tokens:,}",
                flush=True,
            )

        if provider == "gemini":
            # Gentle pacing (~14 RPM) for Gemini free tier
            time.sleep(4.2)
        else:
            rem_tok = meta.get("ratelimit_remaining_tokens")
            if rem_tok is not None and str(rem_tok).isdigit() and int(rem_tok) < 4000:
                wait_s = parse_reset_seconds(meta.get("ratelimit_reset_tokens"))
                time.sleep(min(wait_s + 1.0, 62.0))
            else:
                time.sleep(0.5)

    results = load_jsonl(out_path)
    by_source: dict[str, Counter[str]] = defaultdict(Counter)
    by_model: Counter[str] = Counter()
    overall: Counter[str] = Counter()
    rescued_count = 0
    for r in results:
        by_source[str(r.get("source"))][r["relevant"]] += 1
        by_model[str(r.get("model"))] += 1
        overall[r["relevant"]] += 1
        if r.get("rescued_by_rule"):
            rescued_count += 1

    avg_tokens_per_item = (total_tokens / max(1, batches_run * BATCH_SIZE)) if batches_run else 0.0
    summary = {
        "mode": mode,
        "provider": provider,
        "model": model_id,
        "processed_total": len(results),
        "queue_total": len(queue),
        "stopped_daily_limit": stopped_daily,
        "overall_counts": dict(overall),
        "rescued_by_rule_count": rescued_count,
        "counts_by_source": {k: dict(v) for k, v in sorted(by_source.items())},
        "counts_by_model": dict(by_model),
        "tokens_this_run": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "avg_tokens_per_item": round(avg_tokens_per_item, 1),
        },
        "last_rate_limit_info": {
            k: v for k, v in last_headers.items() if "ratelimit" in k or k in {"retry_after_sec", "error"}
        },
        "output_file": str(out_path),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 3 AI relevance filter (Gemini or Groq)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pilot", action="store_true", help="Run 100-item stratified pilot")
    group.add_argument("--full", action="store_true", help="Run full dataset (2021+ first)")
    parser.add_argument("--provider", choices=["gemini", "groq"], default="gemini")
    parser.add_argument("--model", default=None, help="Override model ID")
    parser.add_argument("--auto-resume", action="store_true", help="Sleep for daily reset and continue")
    args = parser.parse_args()
    model_id = args.model or (DEFAULT_GEMINI_MODEL if args.provider == "gemini" else DEFAULT_GROQ_MODEL)
    run_filter("pilot" if args.pilot else "full", provider=args.provider, model_id=model_id, auto_resume=args.auto_resume)


if __name__ == "__main__":
    main()
