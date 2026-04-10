# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
cd "GLJ Citation Extractor"
streamlit run app.py
```

The app runs at `http://localhost:8501` by default.

## Testing extraction logic directly

```bash
python -c "
import sys; sys.stdout.reconfigure(encoding='utf-8')
from extractor import extract_citations, build_excel
with open('path/to/article.pdf', 'rb') as f:
    data = f.read()
df, meta = extract_citations(data, 'article.pdf')
print(df[['footnote_num','citation','is_id_cite','needs_review']].to_string())
print('Total footnotes:', meta['total_footnotes'])
"
```

## Architecture

Two files only:

- **`app.py`** — Streamlit UI. Handles file upload, sidebar options (AI toggle + API key input, show-raw checkbox, show-review-only checkbox), progress bar, five summary metrics, preview table, and download button. Calls `extract_citations` and `build_excel` from `extractor.py`.
- **`extractor.py`** — All extraction and export logic. No Streamlit imports.

### Extraction pipeline (`extractor.py`)

1. **Footnote extraction** — `extract_footnotes_docx` reads from the Word footnote XML pane; `extract_footnotes_pdf` scans the bottom 38% of each page using pdfplumber, requiring strictly increasing footnote numbers to guard against mid-citation numbers (e.g., `1 (1984)`) being mistaken for new footnotes.

2. **Prose stripping** — Two paths:
   - *AI path*: batches of 30 footnotes sent to `claude-haiku-4-5-20251001` via the Anthropic API. The prompt instructs Claude to return a JSON dict of `{fn_num: [citation_strings]}`. Footnotes where AI returns no result fall back to regex (`extraction_method = 'ai_fallback'`), and are flagged in Needs Review.
   - *Regex path* (`_regex_strip_prose`): splits on semicolons only (matching the source-collect template), then filters each segment through `_looks_like_citation`. Defaults to **excluding** ambiguous text (precision over recall).

3. **Cleaning** — Applied to each citation string per the **Cleaning and Formatting tab** of the Source Collect Revised Process Template:
   - `clean_parentheticals` (column C): replaces `quoting`/`citing`/`West, Westlaw` with placeholders, strips parentheticals ≥ 20 chars via regex, then restores placeholders.
   - `clean_signals` (column D): removes `See, e.g.,`, `See`, `see also`, `But see`, `generally`, `Cf.`, `see` etc., then trims whitespace.
   - `_truncate_trailing_prose`: removes body text that bled into footnote content in PDFs.
   - `_balance_parens`: removes only the excess unbalanced closing parens.

4. **id. citation tracking** — `id.` short-cites are NOT dropped. Instead, each is resolved to `last_canonical` (the most recently seen non-id. source) and stored as a row with `is_id_cite=True`. These rows count toward that source's **Times Cited** and **Cited in Footnote(s)** in the Unique Sources sheet. Unresolved id. rows (no prior source found) are flagged in Needs Review.

5. **Short-cite exclusion** — `supra note X`, `infra note X`, and bare `at X` pincites are dropped entirely. Only `id.` is tracked (see above).

6. **Pincite stripping** — `_strip_pincite` removes supplemental page numbers (e.g., `, 205` in `100 F.3d 200, 205 (2d Cir. 2007)`) to produce a **canonical citation form** used for source consolidation. Two cites of the same source at different pages appear as one unique source.

7. **Needs-review flagging** — `needs_review_reason` returns reason strings for: `quoting`, `citing`, `forthcoming`, `on file with`, `Compare...with...`, bare pincites, short case cites (reporter `at` pattern without a `v.`), AI fallback used, and unresolved id. citations.

### `extract_citations` return value

Returns `(df, metadata)` where:
- `df` columns: `footnote_num`, `raw_citation`, `citation`, `canonical_citation`, `is_id_cite`, `needs_review`, `review_reason`, `extraction_method`
- `metadata = {'total_footnotes': N}` — total footnotes found in the document (including those that yielded no citations)

### UI metrics (app.py)

Five metrics displayed after extraction:
1. **Footnotes found** — from `metadata['total_footnotes']`
2. **Individual citations** — non-id. rows only
3. **Unique sources** — distinct `canonical_citation` values among non-id. rows
4. **id. citations tracked** — count of `is_id_cite=True` rows
5. **Need review** — count + percentage of flagged rows

### Excel output (`build_excel(df, metadata)`)

Three sheets:
- **Unique Sources** — all sources alphabetically sorted; columns: Citation (canonical form), Times Cited (direct + id.), Cited in Footnote(s) (sorted footnote numbers)
- **Summary** — two metrics: Total footnotes processed, Total individual citations
- **Needs Review** — flagged rows with `review_reason` column; yellow highlight

### Known regex-mode limitations

- Two citations in one footnote separated by `. See generally` (not `;`) appear as one joined row.
- Explanatory footnotes that contain `§` in prose may slip through because `§` is a positive citation signal.
- Short case cites (`Rahimi, 602 U.S. at 702`) are flagged but not excluded — they require the full case name to confirm they are short cites.

All three are handled correctly by the AI mode.

## Dependencies

```
streamlit>=1.35.0
python-docx>=1.1.0
pdfplumber>=0.11.0
openpyxl>=3.1.0
pandas>=2.2.0
lxml>=5.2.0
anthropic>=0.25.0
```

Install: `pip install -r requirements.txt`

## Files in this repo

```
app.py                   # Streamlit entry point
extractor.py             # Core extraction and export logic
requirements.txt         # Python dependencies
CLAUDE.md                # This file
For Reference/           # Example files (not deployed — add to .gitignore)
```

The `For Reference/` folder contains example PDFs and spreadsheets used during development. It should be excluded from the public GitHub repo via `.gitignore`.

## Deployment

Deploy to Streamlit Community Cloud for free public sharing:

1. **Prepare the repo**
   - Ensure `.gitignore` excludes `For Reference/`, `__pycache__/`, and `.streamlit/secrets.toml`
   - Push the repo to GitHub (public or private)

2. **Connect to Streamlit Cloud**
   - Go to [streamlit.io/cloud](https://streamlit.io/cloud) and sign in with GitHub
   - Click **New app** → select the repo and branch → set **Main file path** to `app.py`

3. **Secrets (AI mode only)**
   - In the Streamlit Cloud app settings, open **Secrets** and add:
     ```toml
     ANTHROPIC_API_KEY = "sk-ant-..."
     ```
   - The app reads the key from user input at runtime, so this is optional — users can paste their own key. The secret is only needed if you want to pre-populate it.

4. **No build config needed** — Streamlit Cloud auto-installs from `requirements.txt`.

### Local `.streamlit/secrets.toml` (optional, for local dev)

```toml
# .streamlit/secrets.toml  — never commit this file
ANTHROPIC_API_KEY = "sk-ant-..."
```

Add `.streamlit/secrets.toml` to `.gitignore` if you use it locally.
