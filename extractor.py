"""
GLJ Citation Extractor — core extraction and cleaning logic.

Pipeline:
  1. Extract footnotes from a .docx or .pdf file
  2. Strip prose / commentary, keeping only citation strings
     (uses Claude API when a client is supplied, regex heuristics otherwise)
  3. Clean parentheticals  (column C of Cleaning and Formatting tab)
  4. Strip citation signals (column D of Cleaning and Formatting tab)
  5. Split on semicolons into individual citation rows
  6. Track id. citations and resolve them to their prior source
  7. Exclude other standalone short-cites (supra, infra, bare pincites)
  8. Strip pincites to produce canonical source form (for consolidation)
  9. Return a DataFrame ready for Excel export + metadata dict
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
    """
    Return [(footnote_number, text), ...] from a .docx file.

    Reads footnotes.xml directly from the zip rather than going through the
    python-docx API, which does not expose a .footnotes attribute in all versions.
    """
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            if 'word/footnotes.xml' not in z.namelist():
                return []
            xml_bytes = z.read('word/footnotes.xml')
    except Exception:
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    footnotes = []
    for fn_elem in root.findall(f'.//{{{_W}}}footnote'):
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

    Guards against false positives (volume/page numbers mid-citation being
    mistaken for footnote starts) by requiring each new footnote number to
    be strictly greater than the last seen footnote number.
    """
    import pdfplumber

    footnote_map: dict[int, str] = {}
    current_fn: int | None = None
    current_text: list[str] = []
    max_fn_seen: int = 0
    footnote_start_re = re.compile(r'^(\d{1,4})[\.\)]\s+\S')

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
                    candidate = int(m.group(0).split('.')[0].split(')')[0].strip())
                    if candidate > max_fn_seen:
                        flush(current_fn, current_text)
                        current_fn = candidate
                        max_fn_seen = candidate
                        rest = re.sub(r'^\d{1,4}[\.\)]\s+', '', line).strip()
                        current_text = [rest] if rest else []
                    elif current_fn is not None:
                        current_text.append(line)
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
- Statutes: e.g., "42 U.S.C. § 1983", "Fed. R. Crim. P. 11"
- Treaties, regulations, international instruments
- Online/web sources: e.g., "Jane Doe, Article Title, PUB. NAME (Jan. 1, 2024), https://..."

Do NOT include:
- Prose sentences or commentary (e.g., "Another piece echoed a similar line:")
- Quoted speech lifted from a source
- Standalone short-cites: "id.", "id. at X", "supra note X", "infra note X", "at X"
- Introductory signal words (See, Cf., etc.) — omit those from each citation string

Keep URLs that are part of citations for online/web sources. \
Keep parentheticals only if they contain "quoting" or "citing" or are short (under 20 chars).

Return a JSON object: keys are footnote numbers (as strings), values are lists of \
citation strings. If a footnote contains only prose or only short-cites, return an empty list for it.
Example:
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
        raw = re.sub(r'^```(?:json)?\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        data = json.loads(raw)
        return {int(k): v for k, v in data.items()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Step 2b: Prose stripping — regex fallback
# ---------------------------------------------------------------------------

def _looks_like_citation(text: str) -> bool:
    """
    Heuristic: does this segment look like a legal citation rather than prose?

    Requires at least one strong positive signal — defaults to False to avoid
    keeping prose fragments that happen to lack exclusion markers.
    """
    t = text.strip()

    # ---- Hard exclusions first ----------------------------------------
    # Supra/infra note references — resolved separately from raw text
    if re.search(r'\bsupra\s+note\s+\d+|\binfra\s+note\s+\d+', t, re.IGNORECASE):
        return False
    if t.startswith(('"', '\u201c', '\u2018', "'")):
        return False
    if t and t[0].islower():
        return False
    if re.match(
        r'^(Hence|Therefore|Thus|Indeed|Moreover|Furthermore|Additionally'
        r'|Importantly|However|Nevertheless|Consequently|As a result'
        r'|In addition|For example|For instance|In other words|That is'
        r'|Notably|Specifically|In particular|Before proceeding'
        r'|Note that|This exclusion|This approach|This argument|This holding'
        r'|The court|The statute|The law|The rule|Under this|Under the)\b',
        t, re.IGNORECASE,
    ):
        return False
    if re.search(
        r'\b(echoed|argued|stated|noted|observed|wrote|described|found|held'
        r'|suggested|concluded|remarked|acknowledged|recognized|emphasized'
        r'|explained|highlighted|asserted|contended|opined'
        r'|declared|proclaimed|should note)\b',
        t, re.IGNORECASE,
    ):
        return False
    if re.search(r':\s*[\u201c"]', t):
        return False
    if re.match(r'^[a-z\(\)\.\,\s]{0,30}[\.\)]{1,3}\s*$', t, re.IGNORECASE) and len(t) < 30:
        return False

    # ---- Strong positive signals ----------------------------------------
    if re.search(r'\(\s*\d{4}\s*\)', t):
        return True
    if re.search(r'\d+\s+[A-Z][A-Z.\u2019\']{2,}\s+\d+', t):
        return True
    if '\u00a7' in t or '§' in t:
        return True
    if re.search(r'\b\d+\s+U\.S\.C\.', t):
        return True
    if re.search(r'\bFed\.\s*R\.', t):
        return True
    if re.search(r'https?://', t):
        return True
    if re.search(r'\bv\.\s+[A-Z]', t):
        return True
    if re.search(r'\b\d+\s+(F\.\d?d?|U\.S\.|S\.Ct\.|L\.Ed\.)', t):
        return True
    if re.search(r',\s+\d{2,3}\s+[A-Z]', t):
        return True
    if re.search(r'\bL\.\s*Rev\b|\bJ\.\b|\bQ\.\b', t):
        return True

    return False


def _regex_strip_prose(text: str) -> list[str]:
    """
    Clean and split a footnote into individual citation strings.

    Improvement over the original: cleaning (footnote-number stripping,
    parenthetical removal, signal stripping) happens *before* the
    citation-detection heuristic runs, so _looks_like_citation sees
    clean text rather than signal-laden prose.
    """
    text = strip_footnote_number(text)
    parts = [p.strip() for p in text.split(';') if p.strip()]
    results = []
    for part in parts:
        seg = clean_parentheticals(part)
        seg = clean_signals(seg).strip()
        if _ID_CITE.match(seg):
            continue
        if len(seg) < 10:
            continue
        if _looks_like_citation(seg):
            results.append(seg)
    if results:
        return results
    # Fallback: treat the whole footnote as one citation
    whole = clean_signals(clean_parentheticals(text)).strip()
    if len(whole) >= 10 and not _ID_CITE.match(whole) and _looks_like_citation(whole):
        return [whole]
    return []


# ---------------------------------------------------------------------------
# Step 3: Clean parentheticals  (column C — Cleaning and Formatting tab)
# ---------------------------------------------------------------------------

_PAREN_KEEP = ['quoting', 'citing', 'West, Westlaw']
_PAREN_THRESHOLD = 20


def clean_parentheticals(text: str) -> str:
    """
    Remove parentheticals longer than 20 chars unless they contain
    quoting / citing / West, Westlaw.  Uses a replacer function so no
    placeholder characters are needed.
    """
    if not isinstance(text, str):
        return text

    def _replacer(m):
        content = m.group(1)
        if len(content) >= _PAREN_THRESHOLD:
            if any(k.lower() in content.lower() for k in _PAREN_KEEP):
                return m.group(0)
            return ''
        return m.group(0)

    cleaned = re.sub(r'\(([^()]*)\)', _replacer, text)
    return re.sub(r' {2,}', ' ', cleaned).strip()


# ---------------------------------------------------------------------------
# Step 4: Strip citation signals  (column D — Cleaning and Formatting tab)
# ---------------------------------------------------------------------------

_SIGNALS = [
    # More-specific variants must precede shorter overlapping ones
    'See, e.g., ',
    'see, e.g., ',
    'See, e.g.',
    'see, e.g.',
    'See also ',
    'see also ',
    'But See ',
    'But see ',
    'but see ',
    'See generally ',
    'see generally ',
    'Compare ',
    'compare ',
    'E.g., ',
    'e.g., ',
    'Cf., ',
    'Cf. ',
    'cf. ',
    'generally ',
    'See ',
    'see ',
]


def clean_signals(text: str) -> str:
    """
    Strip leading legal citation signals and normalise whitespace.
    Matches the Cleaning and Formatting tab column D formula.
    """
    if not isinstance(text, str):
        return text
    for sig in _SIGNALS:
        text = text.replace(sig, '')
    return re.sub(r'\s+', ' ', text).strip()


def strip_footnote_number(text: str) -> str:
    """
    Remove a leading footnote number marker if present.
    Handles formats like: "30. ", "30) ", "30.\t"
    Does NOT strip numbers that are part of a citation (e.g., "18 U.S.C. § 922").
    """
    if not isinstance(text, str):
        return text
    return re.sub(r'^\d{1,4}[\.\)]\s+', '', text).strip()


def _balance_parens(text: str) -> str:
    """
    Remove unbalanced trailing closing parentheses left by nested-paren stripping.
    Only removes the EXCESS closing parens beyond what was opened.
    """
    opens  = text.count('(')
    closes = text.count(')')
    excess = closes - opens
    if excess > 0:
        chars = list(text)
        removed = 0
        for i in range(len(chars) - 1, -1, -1):
            if chars[i] == ')' and removed < excess:
                chars[i] = ''
                removed += 1
            if removed == excess:
                break
        text = ''.join(chars)
    return text.strip()


def _truncate_trailing_prose(text: str) -> str:
    """
    Remove body-text prose that bled into a citation after PDF extraction.
    """
    year_paren_re = re.compile(
        r'\((?:[A-Za-z0-9\s\.,\u2013\-\']+)?\d{4}[^)]*\)'
    )
    url_bracket_re = re.compile(r'\[https?://[^\]]+\]')

    end_pos = -1
    for pat in (year_paren_re, url_bracket_re):
        for m in pat.finditer(text):
            end_pos = max(end_pos, m.end())

    if end_pos > 0:
        tail = text[end_pos:]
        has_citation_signal = bool(
            re.search(r'\(\s*\d{4}\s*\)', tail)
            or re.search(r'https?://', tail)
            or re.search(r'[§\u00a7]', tail)
            or re.search(r'\bv\.\s+[A-Z]', tail)
            or re.search(r',\s+\d+\s+[A-Z]', tail)
        )
        if not has_citation_signal:
            text = text[:end_pos].strip()

    text = re.sub(r'^[\)\]\s\.]+(?=[A-Z])', '', text)
    text = _balance_parens(text)
    text = re.sub(r'\s+\.\s*$', '', text)

    return text.strip()


# ---------------------------------------------------------------------------
# Step 5: Short-cite detection
# ---------------------------------------------------------------------------

_STANDALONE_SHORT_CITE = re.compile(
    r'^\s*('
    r'(see\s+)?id\.?(\s+at\s+[\w,\s\u2013\u2014\-\.]+)?'
    r'|at\s+\d+[\d,\s\u2013\-]*'
    r')\s*[.,]?\s*$',
    re.IGNORECASE,
)

_ID_CITE = re.compile(r'^\s*(see\s+)?id\.?(\s+at\s+[\w,\s\u2013\u2014\-\.]+)?\s*[.,]?\s*$', re.IGNORECASE)

# Supra/infra note references — detected from raw text and resolved to their source
_SUPRA_NOTE_RE = re.compile(r'supra\s+note\s+(\d+)', re.IGNORECASE)
_INFRA_NOTE_RE = re.compile(r'infra\s+note\s+(\d+)', re.IGNORECASE)


def is_short_cite(text: str) -> bool:
    """Return True if the segment is entirely an id. short-cite or bare pincite."""
    return bool(_STANDALONE_SHORT_CITE.match(text.strip()))


def is_id_cite(text: str) -> bool:
    """Return True if the segment is specifically an id. short-cite."""
    return bool(_ID_CITE.match(text.strip()))


def _find_supra_infra(raw_text: str) -> list[tuple[str, int]]:
    """
    Find all supra/infra note references in a footnote's raw text.
    Returns a deduplicated list of (cite_type, note_num) tuples,
    where cite_type is 'supra' or 'infra'.
    """
    results: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for pattern, cite_type in ((_SUPRA_NOTE_RE, 'supra'), (_INFRA_NOTE_RE, 'infra')):
        for m in pattern.finditer(raw_text):
            note_num = int(m.group(1))
            key = (cite_type, note_num)
            if key not in seen:
                seen.add(key)
                results.append((cite_type, note_num))
    return results


def _resolve_cross_ref(
    canonicals: list[str],
    cite_type: str,
    note_num: int,
) -> tuple[str, str]:
    """
    Resolve a supra/infra reference to a canonical citation.
    Returns (canonical_citation, review_reason).
    """
    label = f'{cite_type} note {note_num}'
    if not canonicals:
        return '', f'Unresolved {label} — referenced footnote has no extracted citations'
    if len(canonicals) == 1:
        return canonicals[0], ''
    return canonicals[0], (
        f'{label} references a footnote with multiple citations; resolved to first — verify'
    )


# ---------------------------------------------------------------------------
# Step 6: Noise filter
# ---------------------------------------------------------------------------

def is_likely_noise(text: str) -> bool:
    t = text.strip()
    if len(t) < 10:
        return True
    if re.fullmatch(r'at \d+[\.,]?', t):
        return True
    return False


# ---------------------------------------------------------------------------
# Step 8: Pincite stripping — canonical source form
# ---------------------------------------------------------------------------

def _strip_pincite(text: str) -> str:
    """
    Strip pincite (supplemental page number) from a citation to produce
    a canonical form used to consolidate sources cited at different pages.

    E.g.:
      "Smith v. Jones, 100 F.3d 200, 205 (2d Cir. 2007)"
       → "Smith v. Jones, 100 F.3d 200 (2d Cir. 2007)"

      "Doe, Title, 18 J. Int'l L. 815, 820 (2007)"
       → "Doe, Title, 18 J. Int'l L. 815 (2007)"
    """
    # After a digit (end of start page), strip ", digits[-digits]" before a year-paren
    cleaned = re.sub(
        r'(\d),\s*\d[\d\-–]*(?=\s*\([^)]*\d{4}[^)]*\))',
        r'\1',
        text,
    )
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Needs-review classifier
# ---------------------------------------------------------------------------

def needs_review_reason(text: str) -> str:
    """
    Return a semicolon-separated string of review reasons, or '' if clean.
    Based on the Final Checklist in the Source Collect Revised Process Template.
    """
    t = text.strip()
    reasons = []
    if is_likely_noise(t):
        reasons.append('Too short / noise')
    # Checklist item 2: citing / quoting
    if re.search(r'\bquoting\b', t, re.IGNORECASE):
        reasons.append('Contains "quoting" — verify source')
    if re.search(r'\bciting\b', t, re.IGNORECASE):
        reasons.append('Contains "citing" — verify source')
    # Checklist item 8: forthcoming / on file
    if re.search(r'\bforthcoming\b', t, re.IGNORECASE):
        reasons.append('Forthcoming source')
    if re.search(r'\bon\s+file\s+with\b', t, re.IGNORECASE):
        reasons.append('On-file source')
    # Checklist item 4: double signal citations
    if re.search(r'\bCompare\b.+\bwith\b', t):
        reasons.append('Compare...with... (double citation — split into two rows)')
    # Bare pincite surviving the split
    if re.match(r'^at\s+\d+', t, re.IGNORECASE):
        reasons.append('Bare pincite')
    # Checklist item 6: short case cite ("at " pattern without full case name)
    if (re.search(r'\b\d+\s+(F\.\d?d?|U\.S\.|S\.Ct\.|L\.Ed\.)\s+at\s+\d+', t)
            and not re.search(r'\bv\.\s+[A-Z]', t)):
        reasons.append('Possible short case cite (no full case name)')
    return '; '.join(reasons)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def extract_citations(
    file_bytes: bytes,
    filename: str,
    anthropic_client=None,
    ai_primary: bool = False,
    on_progress=None,
) -> tuple[pd.DataFrame, dict]:
    """
    Full pipeline.

    Parameters
    ----------
    file_bytes        : raw bytes of the uploaded document
    filename          : original filename (used to detect .docx / .pdf)
    anthropic_client  : optional anthropic.Anthropic instance.
                        None  → Standard mode (regex only).
                        set   → AI Assist or AI Only mode (see ai_primary).
    ai_primary        : if True and anthropic_client is set, AI processes every
                        footnote first (AI Only mode); regex is a safety net for
                        footnotes AI cannot parse.
                        If False and anthropic_client is set, regex runs first and
                        AI handles only footnotes regex cannot parse (AI Assist mode).
    on_progress       : optional callable(done: int, total: int)

    Returns
    -------
    (df, metadata) where:
      df has columns:
        footnote_num, raw_citation, citation, canonical_citation,
        is_id_cite, needs_review, review_reason, extraction_method
      metadata = {'total_footnotes': N}

    Notes
    -----
    - is_id_cite=True rows represent id. citations resolved to a prior source.
      They contribute to Times Cited counts in the Unique Sources sheet.
    - extraction_method values:
        'regex'              — regex extracted the citations
        'ai'                 — AI extracted the citations
        'ai_regex_fallback'  — AI Only mode; AI returned nothing so regex was used
    """
    _empty = pd.DataFrame(columns=[
        'footnote_num', 'raw_citation', 'citation', 'canonical_citation',
        'is_id_cite', 'needs_review', 'review_reason', 'extraction_method',
    ])

    ext = Path(filename).suffix.lower()
    if ext == '.docx':
        raw_footnotes = extract_footnotes_docx(file_bytes)
    elif ext == '.pdf':
        raw_footnotes = extract_footnotes_pdf(file_bytes)
    else:
        raise ValueError(f'Unsupported file type: {ext}')

    if not raw_footnotes:
        return _empty, {'total_footnotes': 0}

    total_footnotes = len(raw_footnotes)

    # ---- Step 2: prose stripping ----------------------------------------
    citation_strings: dict[int, list[str]] = {}
    fn_methods: dict[int, str] = {}

    BATCH = 30

    if anthropic_client is not None and ai_primary:
        # ---- AI Only mode: AI processes every footnote; regex as safety net ----
        for i in range(0, len(raw_footnotes), BATCH):
            batch = raw_footnotes[i:i + BATCH]
            ai_result = _ai_strip_prose(batch, anthropic_client)
            for fn_num, text in batch:
                if fn_num in ai_result and ai_result[fn_num]:
                    citation_strings[fn_num] = ai_result[fn_num]
                    fn_methods[fn_num] = 'ai'
                else:
                    # AI returned nothing — fall back to regex and flag it
                    citation_strings[fn_num] = _regex_strip_prose(text)
                    fn_methods[fn_num] = 'ai_regex_fallback'
    else:
        # ---- Standard / AI Assist: regex runs first on every footnote ----
        for fn_num, text in raw_footnotes:
            result = _regex_strip_prose(text)
            citation_strings[fn_num] = result
            fn_methods[fn_num] = 'regex'

        if anthropic_client is not None:
            # AI Assist: send only the footnotes regex could not parse to Claude
            ai_needed = [(fn_num, text) for fn_num, text in raw_footnotes
                         if not citation_strings[fn_num]]
            if ai_needed:
                for i in range(0, len(ai_needed), BATCH):
                    batch = ai_needed[i:i + BATCH]
                    ai_result = _ai_strip_prose(batch, anthropic_client)
                    for fn_num, text in batch:
                        if fn_num in ai_result and ai_result[fn_num]:
                            citation_strings[fn_num] = ai_result[fn_num]
                            fn_methods[fn_num] = 'ai'
                        # else: both regex and AI found nothing — stays empty

    if on_progress:
        on_progress(len(raw_footnotes), len(raw_footnotes))

    # ---- Fallback: if nothing was extracted, use the cleaned full footnote ----
    for fn_num, text in raw_footnotes:
        if not citation_strings.get(fn_num):
            cleaned = clean_signals(clean_parentheticals(strip_footnote_number(text))).strip()
            if cleaned:
                citation_strings[fn_num] = [cleaned]
                fn_methods[fn_num] = 'raw_fallback'

    # ---- Steps 3-8: clean citations, track id./supra/infra references ------
    raw_lookup = {fn_num: text for fn_num, text in raw_footnotes}
    records = []
    last_canonical: str | None = None  # most recent resolved canonical, for id. resolution
    fn_canonicals: dict[int, list[str]] = {}  # {fn_num: [canonical_citations]}
    deferred_infra: list[dict] = []           # infra rows pending post-loop resolution

    for fn_num in sorted(citation_strings.keys()):
        cit_list = citation_strings[fn_num]
        raw_text = raw_lookup.get(fn_num, '')
        method = fn_methods.get(fn_num, 'regex')

        # Save last_canonical from previous footnote for id. resolution.
        # id. almost always refers to the prior footnote's source, so we
        # resolve ALL id. in this footnote against prev_last_canonical even
        # when real citations also appear in the same footnote.
        prev_last_canonical = last_canonical

        # --- Detect id. citations directly from the raw text ---
        # _regex_strip_prose and AI both exclude id. from cit_list, so we
        # must scan the raw segments ourselves.
        raw_segments = [s.strip() for s in raw_text.split(';') if s.strip()]
        for raw_seg in raw_segments:
            seg_clean = clean_signals(raw_seg.strip())
            if is_id_cite(seg_clean):
                resolved = prev_last_canonical
                reason = '' if resolved else 'Unresolved id. — no prior source identified'
                records.append({
                    'footnote_num':       fn_num,
                    'raw_citation':       raw_text,
                    'citation':           seg_clean.strip(),
                    'canonical_citation': resolved or '',
                    'is_id_cite':         True,
                    'needs_review':       resolved is None,
                    'review_reason':      reason,
                    'extraction_method':  method,
                })

        # --- Detect supra / infra note references from raw text ---
        # Supra resolves immediately (prior footnote data is available).
        # Infra is deferred until all footnotes have been processed.
        fn_canonicals.setdefault(fn_num, [])
        for cite_type, note_num in _find_supra_infra(raw_text):
            cite_text = f'{cite_type} note {note_num}'
            if cite_type == 'supra':
                canonicals = fn_canonicals.get(note_num, [])
                canon, reason = _resolve_cross_ref(canonicals, cite_type, note_num)
                records.append({
                    'footnote_num':       fn_num,
                    'raw_citation':       raw_text,
                    'citation':           cite_text,
                    'canonical_citation': canon,
                    'is_id_cite':         True,
                    'needs_review':       bool(reason),
                    'review_reason':      reason,
                    'extraction_method':  method,
                })
            else:  # infra — referenced footnote not yet processed
                deferred_infra.append({
                    'footnote_num':       fn_num,
                    'raw_citation':       raw_text,
                    'citation':           cite_text,
                    'is_id_cite':         True,
                    'extraction_method':  method,
                    '_infra_note':        note_num,
                })

        # --- Process real citations from prose-stripped cit_list ---
        for raw_cit in cit_list:
            # Column C: remove long parentheticals
            step_c = clean_parentheticals(raw_cit)
            # Column D: strip signals
            step_d = clean_signals(step_c)
            # Post-process: remove trailing prose / PDF artefacts
            step_d = _truncate_trailing_prose(step_d)
            # Split on semicolons (text-to-columns equivalent)
            parts = [p.strip() for p in step_d.split(';') if p.strip()]

            for part in parts:
                # id. / bare pincite: drop (id. was handled above from raw text)
                if is_short_cite(part):
                    continue
                # supra/infra: handled above from raw text — skip here to avoid duplication
                if _SUPRA_NOTE_RE.search(part) or _INFRA_NOTE_RE.search(part):
                    continue

                if is_likely_noise(part):
                    continue

                # Strip pincite to get canonical source form
                canonical = _strip_pincite(part)
                fn_canonicals[fn_num].append(canonical)
                reason = needs_review_reason(part)

                # Flag footnotes where the non-primary extractor had to be used
                if method == 'ai':
                    # AI Assist: regex found nothing, Claude stepped in
                    fallback_note = 'Regex found no citations — AI Assist used; verify extraction'
                    reason = (fallback_note + '; ' + reason).rstrip('; ') if reason else fallback_note
                elif method == 'ai_regex_fallback':
                    # AI Only: Claude returned nothing, regex stepped in
                    fallback_note = 'AI returned no result — regex safety net applied; verify extraction'
                    reason = (fallback_note + '; ' + reason).rstrip('; ') if reason else fallback_note

                records.append({
                    'footnote_num':       fn_num,
                    'raw_citation':       raw_text,
                    'citation':           part,
                    'canonical_citation': canonical,
                    'is_id_cite':         False,
                    'needs_review':       bool(reason),
                    'review_reason':      reason,
                    'extraction_method':  method,
                })
                # Update last known source for subsequent id. resolution
                last_canonical = canonical

    # --- Resolve deferred infra citations ---
    for record in deferred_infra:
        note_num = record.pop('_infra_note')
        canonicals = fn_canonicals.get(note_num, [])
        canon, reason = _resolve_cross_ref(canonicals, 'infra', note_num)
        record['canonical_citation'] = canon
        record['needs_review'] = bool(reason)
        record['review_reason'] = reason
        records.append(record)

    df = pd.DataFrame(records)
    if df.empty:
        return df, {'total_footnotes': total_footnotes}

    # Deduplicate within the same footnote by canonical form to avoid
    # inflating counts when the same source appears with two pincites in one footnote
    df = df.drop_duplicates(
        subset=['footnote_num', 'canonical_citation', 'is_id_cite'],
        keep='first',
    )
    df = df.sort_values('footnote_num').reset_index(drop=True)
    return df, {'total_footnotes': total_footnotes}


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

def build_excel(df: pd.DataFrame, metadata: dict | None = None) -> bytes:
    """
    Export a single-sheet alphabetical list of unique cleaned citations.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    real_df = df[~df['is_id_cite']]
    sources = sorted(
        real_df['canonical_citation'].dropna().unique(),
        key=lambda x: x.lower(),
    )

    HEADER_BG = 'C00000'
    FONT_NAME  = 'Arial'
    ALT_BG     = 'F2F2F2'
    thin   = Side(border_style='thin', color='BFBFBF')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()
    ws = wb.active
    ws.title = 'Sources'
    ws.freeze_panes = 'A2'
    ws.row_dimensions[1].height = 20
    ws.column_dimensions['A'].width = 90

    h = ws.cell(row=1, column=1, value='Citation')
    h.font      = Font(name=FONT_NAME, bold=True, color='FFFFFF', size=10)
    h.fill      = PatternFill('solid', fgColor=HEADER_BG)
    h.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
    h.border    = border

    for i, citation in enumerate(sources):
        r  = i + 2
        c  = ws.cell(row=r, column=1, value=citation)
        c.font      = Font(name=FONT_NAME, size=10)
        c.alignment = Alignment(vertical='top', wrap_text=True)
        c.border    = border
        if i % 2 == 0:
            c.fill = PatternFill('solid', fgColor=ALT_BG)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
