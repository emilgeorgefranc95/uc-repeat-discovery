# Urban Company 90-Day Repeat Discovery Engine

An AI-powered consumer discovery pipeline and 8-page Streamlit evidence dashboard that analyzes public Indian home-service reviews and community discussions to understand what drives—and blocks—repeat bookings within 90 days of a core-category booking (excluding InstaHelp).

## Key Highlights

- **Zero-cost public data stack:** 32,769 public items collected across Google Play Store (balanced 6,000/star 1★–5★), Apple App Store, YouTube comments, and Indian city/household Reddit threads, plus 8 public Urban Company quarterly filings kept strictly separate as context.
- **Explicit bias handling:** Accounts for both angry-review over-representation (balanced star pulling + out-of-scope supply/price separation + source-by-source voluntary vs. involuntary splits) and silent non-returner zero-trace bias (off-platform Reddit & YouTube habits + live *"Exclude curated Reddit"* toggle).
- **Verified evidence & audit trail:** 5,110 tagged / 4,925 relevant core posts, 98.7% verified verbatim quotes (`rapidfuzz`), 76.1% inter-model core agreement, 151 deterministic audit rule fixes, and 9 human overrides in `data/manual/corrections.csv`.
- **Grounded RAG ("Ask the Data"):** Hybrid `fastembed` (`BAAI/bge-small-en-v1.5`) + codebook-tag reranking + source-diversity cap, synthesized with Gemini (`gemini-3.1-flash-lite`) strictly from the top 8 retrieved posts with inline citations and a mandatory bias check.

## Project Structure

```text
.
├── app/
│   └── streamlit_app.py         # 8-page Streamlit dashboard (reads ONLY data/export/)
├── data/
│   ├── export/                  # Public-safe, anonymized parquet, embeddings & summary JSON
│   └── manual/
│       └── corrections.csv      # Audited human tag overrides applied during export
├── collect/                     # Stage 1 & 5 public data collectors (PII-scrubbed)
├── pipeline/                    # Stages 2–6: clean, filter, tag, filings extract, export
├── docs/
│   ├── evidence_log_notes.md    # Running log of every filter, prompt, rule & AI fix
│   └── filings_summary.md       # Public shareholder letter / transcript context summary
├── config.yaml                  # Sources, keywords & InstaHelp exclusions
├── codebook.yaml                # Fixed Stage 4 structured tagging schema
├── requirements.txt             # Pinned minimal dependencies for Streamlit Community Cloud
└── requirements-pipeline.txt    # Additional local pipeline/collector dependencies
```

## Run the Dashboard Locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Add GEMINI_API_KEY to .env (or Streamlit Secrets) for "Ask the Data"
streamlit run app/streamlit_app.py
```

## Refresh Data Later

```bash
python -m pipeline.export --wait-for-ai 30
git add data/export/ data/manual/corrections.csv
git commit -m "Refresh exported discovery data"
git push
```
