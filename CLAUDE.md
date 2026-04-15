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
from extractor import extract_citations
with open('path/to/article.pdf', 'rb') as f:
    data = f.read()
df, meta = extract_citations(data, 'article.pdf')
print(df[['footnote_num','citation']].to_string())
print('Total footnotes:', meta['total_footnotes'])
"
```

## Architecture

Two files only — no AI dependencies, no external API calls:

- **`app.py`** — Streamlit UI. Handles file upload, sidebar (how it works, privacy note, tips), metrics, preview dataframe with text wrapping, and Excel download button.
- **`extractor.py`** — All extraction and export logic. Pure regex and local libraries only. No Streamlit imports, no network calls.

### Extraction pipeline (`extractor.py`)

1. **Footnote extraction**
   - `.docx`: `extract_footnotes_docx` reads `word/footnotes.xml` directly from the zip archive using the standard WordprocessingML namespace.
   - `.pdf`: `extract_footnotes_pdf` scans the bottom 38% of each page with pdfplumber, collecting lines that begin with a strictly increasing footnote number to guard against mid-citation numbers (e.g., `1 (1984)`) being mistaken for new footnotes.

2. **Citation splitting** — `split_citations` uses a single capturing-group regex (`_SPLIT_RE`) to split each footnote on the following signals. The matched delimiter is **preserved and prepended** to the following fragment so reviewers can see how each citation was introduced. Bare semicolons are used as separators only and are not prepended.

   | Signal | Matches |
   |---|---|
   | `;` | Semicolon (separator only, not prepended) |
   | `id.` / `Id.` | Short-cite (`\b` word boundary prevents false matches like "Madrid.") |
   | `see also` / `See also` | |
   | `(quoting` / `(Quoting` | |
   | `see supra` / `See supra` | |
   | `see infra` / `See infra` | |
   | `see, e.g.` / `See, e.g.` / `See e.g.` | Comma is optional (`?,`) |
   | `see generally` / `See generally` | |
   | `but see` / `But see` | |

3. **Filtering** — Fragments with only one word are dropped.

### `extract_citations` return value

Returns `(df, metadata)` where:
- `df` columns: `footnote_num`, `footnote_text`, `citation`
- `metadata = {'total_footnotes': N}`

### Excel output (`build_excel`)

Single sheet **Sources** with:
- Dark blue header row
- One citation per row, `wrap_text=True` and `vertical='top'` alignment
- Column A width set to 100

### UI (`app.py`)

- Sidebar: How it works, Privacy note, Tips, GitHub badge
- Main area: title, privacy callout (`st.info`), file uploader, metrics (footnotes processed + citations extracted), preview dataframe with CSS text wrapping, Excel download button

## Privacy / No AI

Documents are **never sent to any AI service or external API**. The entire pipeline runs locally using:
- Python's built-in `zipfile` and `xml.etree.ElementTree` (for `.docx`)
- `pdfplumber` (for `.pdf`)
- `re` (regex splitting)
- `openpyxl` (Excel export)

## Dependencies

```
streamlit>=1.35.0
pdfplumber>=0.11.0
openpyxl>=3.1.0
pandas>=2.2.0
```

Install: `pip install -r requirements.txt`

## Files in this repo

```
app.py                   # Streamlit entry point
extractor.py             # Core extraction and export logic
requirements.txt         # Python dependencies
CLAUDE.md                # This file
For Reference/           # Example files — excluded from repo via .gitignore
```

## Deployment

Deploy to Streamlit Community Cloud:

1. Ensure `.gitignore` excludes `For Reference/`, `__pycache__/`, and `.streamlit/secrets.toml`
2. Push repo to GitHub (public or private)
3. Go to [streamlit.io/cloud](https://streamlit.io/cloud) → **New app** → select repo/branch → set main file to `app.py`
4. No secrets or build config needed — Streamlit Cloud auto-installs from `requirements.txt`
