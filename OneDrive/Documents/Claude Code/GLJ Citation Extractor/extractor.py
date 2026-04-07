"""
GLJ Citation Extractor — core extraction and cleaning logic.

Pipeline:
  1. Extract footnotes from a .docx or .pdf file
  2. Strip prose / commentary, keeping only citation strings
     (uses Claude API when a client is supplied, regex heuristics otherwise)
  3. Clean parentheticals  (column C logic)
  4. Strip citation signals (column D logic)
  5. Split on semicolons and explode into individual citation rows
  6. Return a DataFrame ready for Excel export
"""

import json
import re
import io
from pathlib import Path

import pandas as pd


_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'

# ---------------------------------------------------------------------------
# Step 1: Document extraction
# ---------------------------------------------------------------------------

def extract_footnotes_docx(file_bytes: bytes) -> list[tuple[int, str]]:
    """Return [(footnote_number, text), ...] from a .docx file."""
    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    footnotes = []

    try:
        fn_part = doc.part.footnotes
        fn_xml = fn_part._element
    except Exception:
        return []

    for fn_elem in fn_xml.findall(f'.//{{{_W}}}footnote'):
        fn_id_str = fn_elem.get(f'{{{_W}}}id', '')
        try:
            fn_id = int(fn_id_str)
        except ValueError:
            continue
        if fn_id <= 0:
            continue

        paragraphs = fn_elem.findall(f'.//{{{_W}}}p')
        text_parts = []
        for p in paragraphs:
            for t in p.findall(f'.//{{{_W}}}t'):
                text_parts.append(t.text or '')
        text = ' '.join(''.join(text_parts).split())
        if text:
            footnotes.append((fn_id, text))

    return footnotes


def extract_footnotes_pdf(file_bytes: bytes) -> list[tuple[int, str]]:
    """
    Extract footnotes from a PDF by scanning the bottom ~38% of each page
    for lines that begin with a footnote number.
    """
    import pdfplumber

    footnote_map: dict[int, str] = {}
    current_fn: int | None = None
    current_text: list[str] = []
    footnote_start_re = re.compile(r'^(\d{1,4})[\.\)\s]\s*(.+)', re.DOTALL)

    def flush(fn_id, text_parts):
        if fn_id is not None and text_parts:
            combined = ' '.join(text_parts).strip()
            footnote_map[fn_id] = (footnote_map.get(fn_id, '') + ' ' + combined).strip()

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            height = page.height
            region = page.within_bbox((0, height * 0.62, page.width, height))
            text = region.extract_text() or ''
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = footnote_start_re.match(line)
                if m:
                    flush(current_fn, current_text)
                    current_fn = int(m.group(1))
                    current_text = [m.group(2).strip()]
                elif current_fn is not None:
                    current_text.append(line)

    flush(current_fn, current_text)
    return sorted(footnote_map.items())


# ---------------------------------------------------------------------------
# Step 2a: Prose stripping — AI path
# ---------------------------------------------------------------------------

_AI_PROMPT = """\
You are processing footnotes from a law review article. \
For each footnote below, extract ONLY the legal citations. \
Discard all prose, commentary, transition sentences, and quoted speech from sources.

Legal citations include:
- Case citations: e.g., "Smith v. Jones, 100 F.3d 200 (2d Cir. 2007)"
- Journal articles: e.g., "Jane Doe, Title, 18 EUR. J. INT'L L. 815 (2007)"
- Books: e.g., "John Smith, Book Title 45 (2007)"
- Statutes: e.g., "42 U.S.C. § 1983"
- Treaties, regulations, international instruments

Do NOT include:
- Prose sentences or commentary (e.g., "Another piece echoed a similar line:")
- Quoted speech lifted from a source
- Introductory signal words (See, Cf., etc.) — omit those from each citation string

Return a JSON object: keys are footnote numbers (as strings), values are lists of \
citation strings. Example:
{{"1": ["Smith v. Jones, 100 F.3d 200 (2007)"], "2": ["Doe, Title, 18 J. 100 (2007)", "Roe, 410 U.S. 113 (1973)"]}}

Return ONLY the JSON object — no markdown, no explanation.

Footnotes:
{footnotes}"""


def _ai_strip_prose(
    batch: list[tuple[int, str]],
    client,
) -> dict[int, list[str]]:
    """
    Send a batch of (fn_num, text) to Claude and return {fn_num: [citations]}.
    Falls back to empty dict on any error (caller will use regex fallback).
    """
    payload = '\n\n'.join(f'[{n}] {t}' for n, t in batch)
    try:
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=4096,
            messages=[{'role': 'user', 'content': _AI_PROMPT.format(footnotes=payload)}],
        )
        raw = resp.content[0].text.strip()
        # Strip accidental markdown fences
        raw = re.sub(r'^```(?:json)?\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        data = json.loads(raw)
        return {int(k): v for k, v in data.items()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Step 2b: Prose stripping — regex fallback
# ---------------------------------------------------------------------------

# Split on sentence-ending periods that are NOT part of:
#   - Abbreviations / reporters (single capital letter before period, e.g. "F." "U." "J.")
#   - Known abbreviations ending in period (handled by negative lookbehind)
#   - Numbers (e.g. "815,")
# Pattern: period (or ?/!) followed by whitespace then an uppercase letter,
# but NOT preceded by a single uppercase letter (reporter abbreviation)
# and NOT preceded by a digit.
_SENTENCE_BOUNDARY = re.compile(
    r'(?<![A-Z\d])'        # not preceded by single capital or digit
    r'(?<!\b[A-Z])'        # not a single-letter abbreviation
    r'[.!?]'               # sentence-ending punctuation
    r'[\u201d"\']?'        # optional closing quote
    r'\s+'                 # whitespace
    r'(?=[A-Z])'           # followed by capital (new sentence)
)

# Common legal abbreviation patterns — these periods should NOT be split on
_ABBREV_RE = re.compile(
    r'\b(?:Vol|No|pp|et al|ed|eds|rev|pub|dept|univ|assoc|corp|inc|ltd'
    r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec'
    r'|U\.S|F\.[23]d|F\.Supp|S\.Ct|L\.Ed'
    r'|id|ibid|supra|infra|cf|viz|e\.g|i\.e)\.',
    re.IGNORECASE,
)


def _looks_like_citation(text: str) -> bool:
    """Heuristic: does this segment look like a legal citation rather than prose?"""
    t = text.strip()

    # Strong positive signals
    if re.search(r'\(\s*\d{4}\s*\)', t):                          # year in parens
        return True
    if re.search(r'\d+\s+[A-Z][A-Z.\u2019\']{2,}\s+\d+', t):    # vol REPORTER page
        return True
    if '\u00a7' in t or '§' in t:                                  # section symbol
        return True
    if re.search(r'\b\d+\s+U\.S\.C\.', t):                        # federal statute
        return True

    # Negative: quoted speech from a source
    if t.startswith(('"', '\u201c', '\u2018', "'")):
        return False
    # Prose verbs that never appear in bare citations
    if re.search(
        r'\b(echoed|argued|stated|noted|observed|wrote|described|found|held'
        r'|suggested|concluded|remarked|acknowledged|recognized|emphasized'
        r'|explained|pointed out|highlighted|asserted|contended|opined'
        r'|declared|proclaimed)\b',
        t, re.IGNORECASE,
    ):
        return False
    # Colon introducing a quotation — prose intro sentence
    if re.search(r':\s*[\u201c"]', t):
        return False

    # Default: keep — avoid discarding uncertain citations
    return True


def _split_on_both_delimiters(text: str) -> list[str]:
    """
    Split a footnote on BOTH semicolons AND sentence-boundary periods,
    mirroring the Excel template's Text-to-Columns (semicolon) step while
    also handling prose sentences embedded between citations.

    Returns a flat list of raw segments before any cleaning.
    """
    # First split on semicolons (primary delimiter per the template)
    semi_parts = [p.strip() for p in text.split(';') if p.strip()]

    segments: list[str] = []
    for part in semi_parts:
        # Within each semicolon-delimited chunk, further split on sentence
        # boundaries — but only where the period is NOT a known abbreviation
        sub = _SENTENCE_BOUNDARY.split(part)
        segments.extend(s.strip() for s in sub if s.strip())

    return segments


def _regex_strip_prose(text: str) -> list[str]:
    """
    Split a footnote on both semicolons and sentence-boundary periods,
    then return only citation-like segments.
    Falls back to returning the whole text when nothing is recognised.
    """
    segments = _split_on_both_delimiters(text)
    citations = [s for s in segments if _looks_like_citation(s)]
    return citations if citations else [text.strip()]


# ---------------------------------------------------------------------------
# Step 3: Clean parentheticals  (column C)
# ---------------------------------------------------------------------------

_PAREN_RE = re.compile(r'\([^)#@~]{20,}\)')


def clean_parentheticals(text: str) -> str:
    """Remove long parentheticals (20+ chars) unless they contain quoting/citing/Westlaw."""
    text = (text
            .replace('quoting', '\x00Q\x00')
            .replace('citing',  '\x00C\x00')
            .replace('West, Westlaw', '\x00W\x00'))
    text = _PAREN_RE.sub('', text)
    return (text
            .replace('\x00Q\x00', 'quoting')
            .replace('\x00W\x00', 'West, Westlaw')
            .replace('\x00C\x00', 'citing'))


# ---------------------------------------------------------------------------
# Step 4: Strip citation signals  (column D)
# ---------------------------------------------------------------------------

_SIGNALS = [
    '[1]',
    'See, e.g., ',
    'see, e.g., ',
    'see also ',
    'See also ',
    'But see ',
    'but see ',
    'See ',
    'generally ',
    'Cf. ',
    'cf. ',
    'E.g., ',
    'e.g., ',
    'see ',
]


def clean_signals(text: str) -> str:
    """Strip leading legal citation signals and normalise whitespace."""
    for sig in _SIGNALS:
        text = text.replace(sig, '')
    return re.sub(r'  +', ' ', text).strip()


# ---------------------------------------------------------------------------
# Step 5: Noise filter
# ---------------------------------------------------------------------------

def is_likely_noise(text: str) -> bool:
    t = text.strip()
    if len(t) < 10:
        return True
    if re.fullmatch(r'at \d+[\.,]?', t):
        return True
    return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def extract_citations(
    file_bytes: bytes,
    filename: str,
    anthropic_client=None,
    on_progress=None,
) -> pd.DataFrame:
    """
    Full pipeline.

    Parameters
    ----------
    file_bytes        : raw bytes of the uploaded document
    filename          : original filename (used to detect .docx / .pdf)
    anthropic_client  : optional anthropic.Anthropic instance; enables AI
                        prose-stripping.  Pass None to use regex heuristics.
    on_progress       : optional callable(done: int, total: int) for progress reporting

    Returns DataFrame with columns:
      footnote_num, raw_citation, citation, needs_review
    """
    ext = Path(filename).suffix.lower()
    if ext == '.docx':
        raw_footnotes = extract_footnotes_docx(file_bytes)
    elif ext == '.pdf':
        raw_footnotes = extract_footnotes_pdf(file_bytes)
    else:
        raise ValueError(f'Unsupported file type: {ext}')

    if not raw_footnotes:
        return pd.DataFrame(columns=['footnote_num', 'raw_citation', 'citation', 'needs_review'])

    # ---- Step 2: prose stripping ----------------------------------------
    # Map fn_num → list of raw citation strings (prose removed)
    citation_strings: dict[int, list[str]] = {}

    if anthropic_client is not None:
        BATCH = 30
        for i in range(0, len(raw_footnotes), BATCH):
            batch = raw_footnotes[i:i + BATCH]
            ai_result = _ai_strip_prose(batch, anthropic_client)
            for fn_num, text in batch:
                if fn_num in ai_result and ai_result[fn_num]:
                    citation_strings[fn_num] = ai_result[fn_num]
                else:
                    # AI returned nothing for this footnote — use regex
                    citation_strings[fn_num] = _regex_strip_prose(text)
            if on_progress:
                on_progress(min(i + BATCH, len(raw_footnotes)), len(raw_footnotes))
    else:
        for fn_num, text in raw_footnotes:
            citation_strings[fn_num] = _regex_strip_prose(text)
        if on_progress:
            on_progress(len(raw_footnotes), len(raw_footnotes))

    # ---- Steps 3-4-5: clean each citation string -------------------------
    raw_lookup = {fn_num: text for fn_num, text in raw_footnotes}
    records = []

    for fn_num, cit_list in citation_strings.items():
        raw_text = raw_lookup[fn_num]
        for raw_cit in cit_list:
            # Column C: remove long parentheticals
            step_c = clean_parentheticals(raw_cit)
            # Column D: strip signals
            step_d = clean_signals(step_c)
            # Final semicolon split — catches any remaining compound citations
            # (the regex path already split on semicolons+periods, but AI may
            # return multi-cite strings joined by semicolons)
            parts = [p.strip() for p in step_d.split(';') if p.strip()]
            for part in parts:
                needs_review = (
                    is_likely_noise(part)
                    or 'quoting' in part
                    or 'citing' in part
                    or 'forthcoming' in part.lower()
                    or 'on file with' in part.lower()
                    or re.search(r'\bid\b\.?', part) is not None
                    or 'supra' in part.lower()
                    or 'infra' in part.lower()
                )
                records.append({
                    'footnote_num': fn_num,
                    'raw_citation': raw_text,
                    'citation':     part,
                    'needs_review': needs_review,
                })

    df = pd.DataFrame(records)
    if df.empty:
        return df

    df = df.drop_duplicates(subset='citation', keep='first').reset_index(drop=True)
    df = df.sort_values('citation').reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

def build_excel(df: pd.DataFrame) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    HEADER_BG = 'C00000'
    HEADER_FG = 'FFFFFF'
    REVIEW_BG = 'FFF2CC'
    ALT_BG    = 'F2F2F2'
    FONT_NAME = 'Arial'
    thin = Side(border_style='thin', color='BFBFBF')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def hdr(ws, row, col, value):
        c = ws.cell(row=row, column=col, value=value)
        c.font      = Font(name=FONT_NAME, bold=True, color=HEADER_FG, size=10)
        c.fill      = PatternFill('solid', fgColor=HEADER_BG)
        c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        c.border    = border

    def dat(ws, row, col, value, bg=None, bold=False):
        c = ws.cell(row=row, column=col, value=value)
        c.font      = Font(name=FONT_NAME, size=10, bold=bold)
        c.alignment = Alignment(vertical='top', wrap_text=True)
        c.border    = border
        if bg:
            c.fill  = PatternFill('solid', fgColor=bg)

    # Sheet 1: Citations
    ws = wb.active
    ws.title = 'Citations'
    ws.freeze_panes = 'A2'
    for c, (h, w) in enumerate(zip(
        ['Footnote #', 'Citation', 'Needs Review', 'Raw Footnote Text'],
        [12, 80, 14, 80],
    ), start=1):
        hdr(ws, 1, c, h)
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.row_dimensions[1].height = 20

    for i, row in df.iterrows():
        r = i + 2
        bg = REVIEW_BG if row['needs_review'] else (ALT_BG if i % 2 == 0 else None)
        dat(ws, r, 1, int(row['footnote_num']), bg)
        dat(ws, r, 2, row['citation'],          bg)
        dat(ws, r, 3, 'Yes' if row['needs_review'] else '', bg, bold=row['needs_review'])
        dat(ws, r, 4, row['raw_citation'],       bg)

    # Sheet 2: Needs Review
    ws2 = wb.create_sheet('Needs Review')
    ws2.freeze_panes = 'A2'
    rev_df = df[df['needs_review']].reset_index(drop=True)
    for c, (h, w) in enumerate(zip(
        ['Footnote #', 'Citation', 'Review Reason', 'Raw Footnote Text'],
        [12, 80, 30, 80],
    ), start=1):
        hdr(ws2, 1, c, h)
        ws2.column_dimensions[get_column_letter(c)].width = w
    ws2.row_dimensions[1].height = 20

    def review_reason(t: str) -> str:
        reasons = []
        if is_likely_noise(t):            reasons.append('Too short / noise')
        if re.search(r'\bid\b\.?', t):    reasons.append('Id. short-cite')
        if 'supra' in t.lower():          reasons.append('Supra reference')
        if 'infra' in t.lower():          reasons.append('Infra reference')
        if 'quoting' in t:                reasons.append('Contains "quoting"')
        if 'citing' in t:                 reasons.append('Contains "citing"')
        if 'forthcoming' in t.lower():    reasons.append('Forthcoming source')
        if 'on file with' in t.lower():   reasons.append('On-file source')
        return '; '.join(reasons) or 'Manual check'

    for i, row in rev_df.iterrows():
        r = i + 2
        bg = REVIEW_BG if i % 2 == 0 else 'FEE9AA'
        dat(ws2, r, 1, int(row['footnote_num']), bg)
        dat(ws2, r, 2, row['citation'],           bg)
        dat(ws2, r, 3, review_reason(row['citation']), bg)
        dat(ws2, r, 4, row['raw_citation'],        bg)

    # Sheet 3: Summary
    ws3 = wb.create_sheet('Summary')
    ws3.column_dimensions['A'].width = 35
    ws3.column_dimensions['B'].width = 15
    hdr(ws3, 1, 1, 'Metric')
    hdr(ws3, 1, 2, 'Count')
    for r, (label, val) in enumerate([
        ('Total footnotes processed',  df['footnote_num'].nunique()),
        ('Total individual citations',  len(df)),
        ('Citations needing review',    int(df['needs_review'].sum())),
        ('Clean citations',             int((~df['needs_review']).sum())),
    ], start=2):
        dat(ws3, r, 1, label)
        dat(ws3, r, 2, val)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
