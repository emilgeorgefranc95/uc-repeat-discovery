"""Dedicated extractor for Urban Company public Shareholder Letters & Earnings Call Transcripts.

Reads all 8 public PDFs (Q3 FY26 -> Q2 FY27), extracts verbatim page-cited passages
relevant to repeat usage, retention, core categories vs. InstaHelp, seasonality,
city/household segments, and customer-side product levers, and synthesizes a
structured, strictly cited JSON + Markdown summary without polluting customer review counts.

Outputs:
- data/processed/filings_passages.jsonl   (verbatim page-cited passages, plain code)
- data/processed/filings_insights.json    (structured quarterly metrics & themes for Dashboard/Part 2)
- docs/filings_summary.md                 (readable executive brief with exact PDF page citations)

Usage:
    python -m pipeline.extract_filings
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from groq import APIConnectionError, APITimeoutError, Groq, RateLimitError
from pypdf import PdfReader

from collect.utils import ROOT_DIR

FILINGS_DIR = ROOT_DIR / "Urban Company public shareholder letters : earnings-call transcripts"
PASSAGES_OUT = ROOT_DIR / "data/processed/filings_passages.jsonl"
INSIGHTS_OUT = ROOT_DIR / "data/processed/filings_insights.json"
SUMMARY_MD_OUT = ROOT_DIR / "docs/filings_summary.md"

SYNTHESIS_MODEL = "openai/gpt-oss-120b"

TOPIC_PATTERNS: dict[str, re.Pattern[str]] = {
    "repeat_and_retention": re.compile(
        r"\b(repeat|retention|cohort|frequency|returning|loyal|transacting\s+users?|atu|"
        r"cross[- ]?category|multi[- ]?category|new\s+users?|new\s+customers?|90[- ]?day|"
        r"order\s+frequency|orders\s+per\s+user|repeat\s+ntv|repeat\s+customer)\b",
        re.IGNORECASE,
    ),
    "core_vs_instahelp": re.compile(
        r"\b(india\s+consumer\s+services|core\s+categor|insta\s?help|quick\s+service|"
        r"housekeeping|native\s+ro|native\s+lock|water\s+purifier|smart\s+lock)\b",
        re.IGNORECASE,
    ),
    "categories_and_seasonality": re.compile(
        r"\b(seasonality|seasonal|summer|monsoon|festive|diwali|winter|salon|deep\s+clean|"
        r"cleaning|ac\s+service|air\s+condition|appliance|plumber|electrician|carpenter|"
        r"painting|pest\s+control|spa|massage|beauty|home\s+repairs)\b",
        re.IGNORECASE,
    ),
    "cities_and_segments": re.compile(
        r"\b(tier[- ]?[123]|top\s+\d+\s+cities|metro|non[- ]?metro|cities|geograph|"
        r"household|affluent|urban\s+household|penetration|bangalore|mumbai|delhi|ncr)\b",
        re.IGNORECASE,
    ),
    "customer_experience_and_product": re.compile(
        r"\b(nps|net\s+promoter|rating|fulfilment|fulfillment|cancellation|rebook|"
        r"slot\s+availability|supply\s+constraint|trust|quality|warranty|plus\s+membership|"
        r"subscription|repeat\s+rate|churn|discovery|app\s+experience|ai\b|chatbot)\b",
        re.IGNORECASE,
    ),
}


def classify_pdf(path: Path) -> tuple[str, str]:
    """Return (quarter_label, doc_type) from filename."""
    name = path.name
    if "Q3" in name or "31012026" in name:
        quarter = "Q3 FY26"
    elif "Q4_FY26" in name or "Q4 FY26" in name:
        quarter = "Q4 FY26"
    elif "Q1_FY27" in name or "Q1 FY27" in name:
        quarter = "Q1 FY27"
    elif "Q2_FY27" in name or "Q2 FY27" in name:
        quarter = "Q2 FY27"
    else:
        quarter = "Unknown"

    doc_type = "earnings_transcript" if "transcript" in name.lower() else "shareholder_letter"
    return quarter, doc_type


def clean_pdf_text(text: str) -> str:
    """Normalize PDF spacing, fixing word-per-line transcript layouts while preserving paragraphs."""
    text = text.replace("\x00", " ")
    non_empty_lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if non_empty_lines:
        avg_words_per_line = sum(len(ln.split()) for ln in non_empty_lines) / len(non_empty_lines)
        if avg_words_per_line < 2.5:
            # Word-per-line PDF extraction (e.g. 'Now,\n \nI\n \nwould...') -> collapse to clean prose
            return re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_passages(page_text: str) -> list[str]:
    """Split a PDF page into readable chunks (~60-220 words)."""
    raw_paras = [p.strip() for p in re.split(r"\n\s*\n", page_text) if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_words = 0

    for p in raw_paras:
        p_flat = re.sub(r"\s+", " ", p).strip()
        w = len(p_flat.split())
        if w < 8:
            continue
        if buf_words + w <= 220:
            buf.append(p_flat)
            buf_words += w
        else:
            if buf:
                chunks.append(" ".join(buf))
            # If single paragraph is huge (e.g., dense transcript page), split by sentence chunks
            if w > 240:
                sents = re.split(r"(?<=[.!?])\s+", p_flat)
                s_buf, s_w = [], 0
                for s in sents:
                    sw = len(s.split())
                    if s_w + sw <= 180:
                        s_buf.append(s)
                        s_w += sw
                    else:
                        if s_buf:
                            chunks.append(" ".join(s_buf))
                        s_buf, s_w = [s], sw
                if s_buf:
                    chunks.append(" ".join(s_buf))
                buf, buf_words = [], 0
            else:
                buf, buf_words = [p_flat], w
    if buf:
        chunks.append(" ".join(buf))
    return chunks


def extract_all_passages() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Deterministically extract all topic-matched verbatim passages across the 8 PDFs."""
    pdfs = sorted(FILINGS_DIR.glob("*.pdf"))
    if not pdfs:
        raise RuntimeError(f"No PDFs found in {FILINGS_DIR}")

    passages: list[dict[str, Any]] = []
    doc_stats: list[dict[str, Any]] = []
    topic_counts: Counter[str] = Counter()

    for pdf_path in pdfs:
        quarter, doc_type = classify_pdf(pdf_path)
        reader = PdfReader(str(pdf_path))
        matched_in_doc = 0

        for page_idx, page in enumerate(reader.pages, 1):
            txt = clean_pdf_text(page.extract_text() or "")
            if not txt:
                continue
            for chunk_idx, chunk in enumerate(split_passages(txt), 1):
                matched_topics = [
                    topic for topic, pat in TOPIC_PATTERNS.items() if pat.search(chunk)
                ]
                if not matched_topics:
                    continue
                # Score by number of distinct topics + keyword density + presence of numbers/%
                kw_hits = sum(len(TOPIC_PATTERNS[t].findall(chunk)) for t in matched_topics)
                has_numbers = bool(re.search(r"\b\d+(?:\.\d+)?%|\b₹|\bRs\.?|\bCr\b|\bmillion\b|\bcrore\b", chunk, re.IGNORECASE))
                score = kw_hits * 2 + len(matched_topics) * 3 + (4 if has_numbers else 0)

                matched_in_doc += 1
                for t in matched_topics:
                    topic_counts[t] += 1

                passages.append(
                    {
                        "id": f"{quarter.replace(' ', '_')}_{doc_type}_p{page_idx}_c{chunk_idx}",
                        "quarter": quarter,
                        "doc_type": doc_type,
                        "file": pdf_path.name,
                        "page": page_idx,
                        "topics": matched_topics,
                        "score": score,
                        "has_numbers": has_numbers,
                        "text": chunk,
                    }
                )

        doc_stats.append(
            {
                "quarter": quarter,
                "doc_type": doc_type,
                "file": pdf_path.name,
                "pages": len(reader.pages),
                "matched_passages": matched_in_doc,
            }
        )

    PASSAGES_OUT.parent.mkdir(parents=True, exist_ok=True)
    with PASSAGES_OUT.open("w", encoding="utf-8") as f:
        for p in passages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    meta = {
        "pdfs_scanned": len(pdfs),
        "total_pages": sum(d["pages"] for d in doc_stats),
        "total_matched_passages": len(passages),
        "topic_counts": dict(topic_counts.most_common()),
        "documents": doc_stats,
    }
    return passages, meta


def select_top_passages_by_quarter(passages: list[dict[str, Any]], max_per_quarter: int = 16) -> dict[str, list[dict[str, Any]]]:
    """Select the highest-signal repeat/retention/core-category passages per quarter."""
    by_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in passages:
        # Boost passages that directly mention repeat/retention/cohort/frequency/core vs instahelp
        boost = 0
        if "repeat_and_retention" in p["topics"]:
            boost += 12
        if "core_vs_instahelp" in p["topics"]:
            boost += 6
        if "categories_and_seasonality" in p["topics"]:
            boost += 4
        if "cities_and_segments" in p["topics"]:
            boost += 4
        by_q[p["quarter"]].append({**p, "_rank_score": p["score"] + boost})

    selected: dict[str, list[dict[str, Any]]] = {}
    for q in ("Q3 FY26", "Q4 FY26", "Q1 FY27", "Q2 FY27"):
        pool = sorted(by_q.get(q, []), key=lambda x: x["_rank_score"], reverse=True)
        # Balance shareholder letter + earnings transcript for each quarter
        letters = [p for p in pool if p["doc_type"] == "shareholder_letter"][: max_per_quarter // 2]
        calls = [p for p in pool if p["doc_type"] == "earnings_transcript"][: max_per_quarter // 2]
        combined = letters + calls
        selected[q] = combined
    return selected


def synthesize_quarter(client: Groq, quarter: str, passages: list[dict[str, Any]]) -> dict[str, Any]:
    """Use Groq gpt-oss-120b to extract strictly cited facts from one quarter's passages."""
    context_lines = []
    for i, p in enumerate(passages, 1):
        cite = f"{p['quarter']} | {p['doc_type']} | p.{p['page']}"
        context_lines.append(f"[{i}] ({cite}) {p['text']}")
    context_blob = "\n\n".join(context_lines)

    sys_prompt = """You are a rigorous financial & product research analyst extracting facts from Urban Company's official Shareholder Letters and Earnings Call Transcripts.

STRICT RULES:
- Use ONLY facts explicitly stated in the numbered passages provided. NEVER invent numbers, percentages, or claims.
- If a topic is not mentioned in the passages for this quarter, write "Not disclosed in extracted passages".
- Every claim or metric MUST include its exact citation like "(Q4 FY26, shareholder_letter, p.12)" and a short verbatim snippet from the passage.

Return ONLY valid JSON with this exact schema:
{
  "quarter": "<quarter>",
  "repeat_and_retention_metrics": [
    {"metric": "<name>", "value": "<exact number/statement>", "citation": "<Q, doc_type, p.X>", "verbatim_quote": "<short exact quote>"}
  ],
  "core_services_vs_instahelp": [
    {"point": "<summary>", "citation": "<Q, doc_type, p.X>", "verbatim_quote": "<short exact quote>"}
  ],
  "category_and_seasonality_patterns": [
    {"point": "<summary>", "citation": "<Q, doc_type, p.X>", "verbatim_quote": "<short exact quote>"}
  ],
  "cities_and_customer_segments": [
    {"point": "<summary>", "citation": "<Q, doc_type, p.X>", "verbatim_quote": "<short exact quote>"}
  ],
  "customer_experience_and_product_levers": [
    {"point": "<summary>", "citation": "<Q, doc_type, p.X>", "verbatim_quote": "<short exact quote>"}
  ],
  "management_blind_spots_for_part2_and_interviews": [
    "<short note on what management did NOT disclose that customer reviews/interviews must uncover>"
  ]
}"""

    for attempt in range(1, 7):
        try:
            resp = client.chat.completions.create(
                model=SYNTHESIS_MODEL,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": f"Quarter: {quarter}\n\nPassages:\n{context_blob}"},
                ],
                temperature=0.0,
                max_completion_tokens=3072,
                reasoning_effort="low",
                response_format={"type": "json_object"},
            )
            raw = (resp.choices[0].message.content or "").strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE | re.DOTALL).strip()
            data = json.loads(raw)
            usage = resp.usage
            data["_tokens"] = int(usage.total_tokens or 0) if usage else 0
            return data
        except (RateLimitError, APIConnectionError, APITimeoutError, json.JSONDecodeError) as e:
            if attempt == 6:
                raise
            wait_s = 32.0 if isinstance(e, RateLimitError) else 5.0 * attempt
            print(f"[{quarter}] {type(e).__name__} (attempt {attempt}/6); waiting {wait_s:.0f}s...", flush=True)
            time.sleep(wait_s)
    raise RuntimeError(f"Failed synthesizing {quarter}")


def render_markdown(meta: dict[str, Any], quarterly: list[dict[str, Any]]) -> str:
    lines = [
        "# Urban Company Public Filings Summary (Q3 FY26 – Q2 FY27)",
        "",
        f"> Extracted from **{meta['pdfs_scanned']} official PDFs** ({meta['total_pages']} pages, **{meta['total_matched_passages']} topic-matched passages**) kept separate from customer review counts so corporate commentary never distorts customer percentages.",
        "",
        "## Documents Analyzed",
        "",
        "| Quarter | Document Type | File | Pages | Matched Passages |",
        "|---|---|---|---:|---:|",
    ]
    for d in meta["documents"]:
        lines.append(
            f"| {d['quarter']} | `{d['doc_type']}` | `{d['file']}` | {d['pages']} | {d['matched_passages']} |"
        )

    for qdata in quarterly:
        q = qdata.get("quarter", "Unknown")
        lines.extend(["", "---", "", f"## {q}"])

        lines.append("### 1. Repeat & Retention Metrics")
        for m in qdata.get("repeat_and_retention_metrics", []):
            lines.append(f"- **{m.get('metric')}:** {m.get('value')} *({m.get('citation')})* — *\"{m.get('verbatim_quote')}\"*")

        for sec_key, heading in [
            ("core_services_vs_instahelp", "### 2. Core Consumer Services vs. InstaHelp"),
            ("category_and_seasonality_patterns", "### 3. Category & Seasonality Patterns"),
            ("cities_and_customer_segments", "### 4. Cities & Customer Segments"),
            ("customer_experience_and_product_levers", "### 5. Customer Experience & Product Levers"),
        ]:
            lines.append("")
            lines.append(heading)
            for item in qdata.get(sec_key, []):
                lines.append(f"- {item.get('point')} *({item.get('citation')})* — *\"{item.get('verbatim_quote')}\"*")

        blind = qdata.get("management_blind_spots_for_part2_and_interviews", [])
        if blind:
            lines.append("")
            lines.append("### 6. Gaps / Blind Spots (What Filings Do NOT Explain)")
            for b in blind:
                lines.append(f"- {b}")

    return "\n".join(lines) + "\n"


def main() -> None:
    load_dotenv(ROOT_DIR / ".env")
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing from .env")

    passages, meta = extract_all_passages()
    selected_by_q = select_top_passages_by_quarter(passages, max_per_quarter=12)

    client = Groq(api_key=api_key)
    quarterly_results: list[dict[str, Any]] = []
    total_tokens = 0

    for idx, q in enumerate(("Q3 FY26", "Q4 FY26", "Q1 FY27", "Q2 FY27")):
        q_passages = selected_by_q.get(q, [])
        if not q_passages:
            continue
        if idx > 0:
            time.sleep(25)  # Respect 8,000 TPM free-tier pacing across quarters
        res = synthesize_quarter(client, q, q_passages)
        total_tokens += res.get("_tokens", 0)
        quarterly_results.append(res)

    output_obj = {
        "metadata": {**meta, "synthesis_model": SYNTHESIS_MODEL, "synthesis_tokens": total_tokens},
        "quarterly_insights": quarterly_results,
    }
    INSIGHTS_OUT.parent.mkdir(parents=True, exist_ok=True)
    INSIGHTS_OUT.write_text(json.dumps(output_obj, indent=2, ensure_ascii=False), encoding="utf-8")

    md = render_markdown(meta, quarterly_results)
    SUMMARY_MD_OUT.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_MD_OUT.write_text(md, encoding="utf-8")

    print(
        json.dumps(
            {
                "pdfs_scanned": meta["pdfs_scanned"],
                "total_pages": meta["total_pages"],
                "total_matched_passages": meta["total_matched_passages"],
                "topic_counts": meta["topic_counts"],
                "quarters_synthesized": [q.get("quarter") for q in quarterly_results],
                "synthesis_model": SYNTHESIS_MODEL,
                "synthesis_tokens": total_tokens,
                "passages_file": str(PASSAGES_OUT),
                "insights_json": str(INSIGHTS_OUT),
                "summary_md": str(SUMMARY_MD_OUT),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
