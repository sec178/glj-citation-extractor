"""
GLJ Citation Extractor — core extraction and cleaning logic.

Pipeline (mirrors the Excel template):
  1. Extract footnotes from a .docx or .pdf file
  2. Clean parentheticals  (column C logic)
  3. Strip citation signals (column D logic)
  4. Split on semicolons and explode into individual citation rows
  5. Return a DataFrame ready for Excel export
"""

import re
import io
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Step 1: Document extraction
# ---------------------------------------------------------------------------

def extract_footnotes_docx(file_bytes: bytes) -> list[tuple[int, str]]:
    """Return [(footnote_number, text), ...] from a .docx file."""
    from docx import Document
    from docx.oxml.ns import qn
    from lxml import etree

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
        # Skip separator/continuation footnotes (id <= 0)
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


_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def extract_footnotes_pdf(file_bytes: bytes) -> list[tuple[int, str]]:
    """
    Extract footnotes from a PDF.

    Strategy: on each page, lines in the bottom ~30% that begin with a digit
    (possibly preceded by a superscript-style marker) are considered footnote
    text.  Consecutive lines belonging to the same footnote number are joined.
    """
    import pdfplumber

    footnote_map: dict[int, str] = {}
    current_fn: int | None = None
    current_text: list[str] = []

    footnote_start_re = re.compile(r'^(\d{1,4})[\.\)\s]\s*(.+)', re.DOTALL)

    def flush(fn_id, text_parts):
        if fn_id is not None and text_parts:
            combined = ' '.join(text_parts).strip()
            if fn_id in footnote_map:
                footnote_map[fn_id] += ' ' + combined
            else:
                footnote_map[fn_id] = combined

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            height = page.height
            # Extract bottom 35% of the page where footnotes typically live
            footnote_region = page.within_bbox((0, height * 0.62, page.width, height))
            text = footnote_region.extract_text() or ''

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
# Step 2: Clean parentheticals  (mirrors column C formula)
# ---------------------------------------------------------------------------

_PAREN_RE = re.compile(r'\([^)#@~]{20,}\)')


def clean_parentheticals(text: str) -> str:
    """
    Remove long parentheticals (20+ chars) unless they contain
    'quoting', 'citing', or 'West, Westlaw'.
    """
    text = (text
            .replace('quoting', '\x00Q\x00')
            .replace('citing',  '\x00C\x00')
            .replace('West, Westlaw', '\x00W\x00'))
    text = _PAREN_RE.sub('', text)
    text = (text
            .replace('\x00Q\x00', 'quoting')
            .replace('\x00W\x00', 'West, Westlaw')
            .replace('\x00C\x00', 'citing'))
    return text


# ---------------------------------------------------------------------------
# Step 3: Strip citation signals  (mirrors column D formula)
# ---------------------------------------------------------------------------

# Order matters: longer / more-specific strings first
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
    """Strip leading legal citation signals and trim whitespace."""
    for sig in _SIGNALS:
        text = text.replace(sig, '')
    # Collapse multiple spaces and trim
    return re.sub(r'  +', ' ', text).strip()


# ---------------------------------------------------------------------------
# Step 4: Final checklist filters (automatic portion only)
# ---------------------------------------------------------------------------

def is_likely_noise(text: str) -> bool:
    """
    Return True for rows that are almost certainly not useful citations:
    - Very short strings (< 10 chars after cleaning)
    - Pure page numbers / 'at NNN' short cites
    - Blank
    """
    t = text.strip()
    if len(t) < 10:
        return True
    # Pure "at NNN" short-cite fragments
    if re.fullmatch(r'at \d+[\.\,]?', t):
        return True
    return False


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def extract_citations(
    file_bytes: bytes,
    filename: str,
) -> pd.DataFrame:
    """
    Full pipeline: extract → clean → split → explode → return DataFrame.

    Columns returned:
      footnote_num   – original footnote number
      raw_citation   – text as extracted from the document
      citation       – fully cleaned individual citation string
      needs_review   – True when the row may need manual attention
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

    records = []
    for fn_num, raw_text in raw_footnotes:
        # Column C: remove long parentheticals
        step_c = clean_parentheticals(raw_text)
        # Column D: strip signals
        step_d = clean_signals(step_c)
        # Split on semicolons (Text-to-Columns step)
        parts = [p.strip() for p in step_d.split(';')]
        for part in parts:
            if not part:
                continue
            needs_review = (
                is_likely_noise(part)
                or 'quoting' in part
                or 'citing' in part
                or 'forthcoming' in part.lower()
                or 'on file with' in part.lower()
                or re.search(r'\bid\b\.?', part) is not None   # id. short-cites
                or 'supra' in part.lower()
                or 'infra' in part.lower()
            )
            records.append({
                'footnote_num':  fn_num,
                'raw_citation':  raw_text,
                'citation':      part,
                'needs_review':  needs_review,
            })

    df = pd.DataFrame(records)

    # De-duplicate identical citations (keep first occurrence)
    df = df.drop_duplicates(subset='citation', keep='first').reset_index(drop=True)

    # Sort alphabetically by citation (mirrors Step 5 checklist)
    df = df.sort_values('citation').reset_index(drop=True)

    return df


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

def build_excel(df: pd.DataFrame) -> bytes:
    """
    Write the citation DataFrame to an Excel workbook and return the bytes.

    Sheets:
      'Citations'     – all citations, colour-coded
      'Needs Review'  – subset flagged for manual attention
    """
    from openpyxl import Workbook
    from openpyxl.styles import (
        Font, PatternFill, Alignment, Border, Side, GradientFill
    )
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    # ---- Colour palette ----
    HEADER_BG   = 'C00000'   # dark red (GLJ-style)
    HEADER_FG   = 'FFFFFF'
    REVIEW_BG   = 'FFF2CC'   # light yellow
    ALT_BG      = 'F2F2F2'   # light grey for alternating rows
    FONT_NAME   = 'Arial'

    thin = Side(border_style='thin', color='BFBFBF')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    def make_header_cell(ws, row, col, value):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font        = Font(name=FONT_NAME, bold=True, color=HEADER_FG, size=10)
        cell.fill        = PatternFill('solid', fgColor=HEADER_BG)
        cell.alignment   = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border      = border
        return cell

    def make_data_cell(ws, row, col, value, bg=None, bold=False, wrap=True):
        cell = ws.cell(row=row, column=col, value=value)
        cell.font        = Font(name=FONT_NAME, size=10, bold=bold)
        cell.alignment   = Alignment(vertical='top', wrap_text=wrap)
        cell.border      = border
        if bg:
            cell.fill    = PatternFill('solid', fgColor=bg)
        return cell

    # ================================================================
    # Sheet 1: Citations
    # ================================================================
    ws = wb.active
    ws.title = 'Citations'
    ws.freeze_panes = 'A2'

    headers = ['Footnote #', 'Citation', 'Needs Review', 'Raw Footnote Text']
    col_widths = [12, 80, 14, 80]

    for c, (h, w) in enumerate(zip(headers, col_widths), start=1):
        make_header_cell(ws, 1, c, h)
        ws.column_dimensions[get_column_letter(c)].width = w

    ws.row_dimensions[1].height = 20

    for r_idx, row in df.iterrows():
        excel_row = r_idx + 2
        bg = REVIEW_BG if row['needs_review'] else (ALT_BG if r_idx % 2 == 0 else None)
        make_data_cell(ws, excel_row, 1, int(row['footnote_num']), bg=bg)
        make_data_cell(ws, excel_row, 2, row['citation'],          bg=bg)
        review_val = 'Yes' if row['needs_review'] else ''
        make_data_cell(ws, excel_row, 3, review_val, bg=bg, bold=row['needs_review'])
        make_data_cell(ws, excel_row, 4, row['raw_citation'],      bg=bg)

    # ================================================================
    # Sheet 2: Needs Review
    # ================================================================
    ws2 = wb.create_sheet('Needs Review')
    ws2.freeze_panes = 'A2'

    review_df = df[df['needs_review']].reset_index(drop=True)
    review_headers = ['Footnote #', 'Citation', 'Review Reason', 'Raw Footnote Text']
    review_widths  = [12, 80, 30, 80]

    for c, (h, w) in enumerate(zip(review_headers, review_widths), start=1):
        make_header_cell(ws2, 1, c, h)
        ws2.column_dimensions[get_column_letter(c)].width = w

    ws2.row_dimensions[1].height = 20

    def review_reason(citation_text: str) -> str:
        reasons = []
        if is_likely_noise(citation_text):
            reasons.append('Too short / noise')
        if re.search(r'\bid\b\.?', citation_text):
            reasons.append('Id. short-cite')
        if 'supra' in citation_text.lower():
            reasons.append('Supra reference')
        if 'infra' in citation_text.lower():
            reasons.append('Infra reference')
        if 'quoting' in citation_text:
            reasons.append('Contains "quoting"')
        if 'citing' in citation_text:
            reasons.append('Contains "citing"')
        if 'forthcoming' in citation_text.lower():
            reasons.append('Forthcoming source')
        if 'on file with' in citation_text.lower():
            reasons.append('On-file source')
        return '; '.join(reasons) if reasons else 'Manual check'

    for r_idx, row in review_df.iterrows():
        excel_row = r_idx + 2
        bg = REVIEW_BG if r_idx % 2 == 0 else 'FEE9AA'
        make_data_cell(ws2, excel_row, 1, int(row['footnote_num']), bg=bg)
        make_data_cell(ws2, excel_row, 2, row['citation'],          bg=bg)
        make_data_cell(ws2, excel_row, 3, review_reason(row['citation']), bg=bg)
        make_data_cell(ws2, excel_row, 4, row['raw_citation'],      bg=bg)

    # ================================================================
    # Sheet 3: Summary
    # ================================================================
    ws3 = wb.create_sheet('Summary')

    summary_data = [
        ('Total footnotes processed', df['footnote_num'].nunique()),
        ('Total individual citations', len(df)),
        ('Citations needing review',  int(df['needs_review'].sum())),
        ('Clean citations',           int((~df['needs_review']).sum())),
    ]

    ws3.column_dimensions['A'].width = 35
    ws3.column_dimensions['B'].width = 15

    make_header_cell(ws3, 1, 1, 'Metric')
    make_header_cell(ws3, 1, 2, 'Count')

    for r, (label, val) in enumerate(summary_data, start=2):
        make_data_cell(ws3, r, 1, label)
        make_data_cell(ws3, r, 2, val)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
