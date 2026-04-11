"""
GLJ Citation Extractor — Streamlit app entry point.
"""

import streamlit as st
from extractor import extract_citations, build_excel

st.set_page_config(
    page_title='GLJ Citation Extractor',
    page_icon='⚖️',
    layout='wide',
)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title('⚖️ GLJ Citation Extractor')
st.markdown(
    'Upload a **Word (.docx)** or **PDF** article and this tool will extract, '
    'clean, and de-duplicate every legal citation from the footnotes — '
    'then export them to an Excel workbook ready for source collection.'
)
st.divider()

# ---------------------------------------------------------------------------
# Sidebar: options
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header('Options')

    st.subheader('Extraction mode')
    mode = st.radio(
        'Extraction mode',
        options=['Standard', 'AI Assist', 'AI Only'],
        label_visibility='collapsed',
        help=(
            '**Standard** — rule-based regex only; no API key required.\n\n'
            '**AI Assist** — regex runs first; Claude handles footnotes regex cannot parse.\n\n'
            '**AI Only** — Claude processes every footnote; regex is a safety net if Claude '
            'returns nothing for a footnote.'
        ),
    )

    api_key = ''
    if mode in ('AI Assist', 'AI Only'):
        api_key = st.text_input(
            'Anthropic API key',
            type='password',
            help='Required for AI Assist and AI Only modes.',
        )
        if api_key:
            if mode == 'AI Assist':
                st.success('AI Assist enabled — Claude will handle footnotes regex cannot parse.')
            else:
                st.success('AI Only enabled — Claude will process every footnote.')
        else:
            st.warning('Enter an Anthropic API key above to activate this mode.')
    else:
        st.info(
            'Standard mode — citations parsed using the rule-based regex heuristics '
            '(Cleaning and Formatting tab logic).'
        )

    st.divider()
    show_raw = st.checkbox('Show raw footnote text in preview', value=False)
    show_review_only = st.checkbox('Preview "Needs Review" rows only', value=False)
    st.divider()
    st.markdown(
        '**Excluded automatically** (never appear in output):\n'
        '- `supra note X` references\n'
        '- `infra note X` references\n'
        '- Bare pincites (`at X`)\n\n'
        '**id. citations** are tracked and counted toward their prior source.\n\n'
        '**Supra note X** and **Infra note X** citations are resolved to '
        'the source cited in footnote X and counted toward it.\n\n'
        '**Flagged for review** (appear on Needs Review sheet):\n'
        '- Citations containing *quoting* or *citing*\n'
        '- *Forthcoming* / *on-file* sources\n'
        '- `Compare...with...` double citations\n'
        '- Possible short case cites\n'
        '- AI Assist used (regex found nothing; Claude extracted the citations)\n'
        '- AI safety net used (AI Only mode; Claude returned nothing; regex applied)\n'
        '- Unresolved *id.*, *supra*, or *infra* citations\n\n'
        '**Sources are consolidated** by base citation — '
        'different pincites of the same source count as one unique source.'
    )

# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------
uploaded = st.file_uploader(
    'Upload your article',
    type=['pdf', 'docx'],
    help='The file should contain footnotes in the standard location '
         '(footnote pane for Word; bottom of each page for PDF).',
)

if uploaded is None:
    st.info('Upload a file above to get started.')
    st.stop()

# ---------------------------------------------------------------------------
# Run extraction
# ---------------------------------------------------------------------------
anthropic_client = None
ai_primary = False
if mode in ('AI Assist', 'AI Only') and api_key:
    try:
        import anthropic
        anthropic_client = anthropic.Anthropic(api_key=api_key)
        ai_primary = (mode == 'AI Only')
    except ImportError:
        st.warning('`anthropic` package not installed. Falling back to Standard mode.')

progress_bar = st.progress(0, text='Extracting citations…')

def on_progress(done: int, total: int):
    pct = done / total if total else 1.0
    label = f'Processing footnotes… {done}/{total}'
    progress_bar.progress(pct, text=label)

with st.spinner('Extracting and cleaning citations…'):
    try:
        df, metadata = extract_citations(
            uploaded.read(),
            uploaded.name,
            anthropic_client=anthropic_client,
            ai_primary=ai_primary,
            on_progress=on_progress,
        )
    except Exception as e:
        st.error(f'Extraction failed: {e}')
        st.stop()

progress_bar.empty()

if df.empty:
    st.warning(
        'No footnotes were detected in this file.\n\n'
        '**For Word documents:** make sure footnotes are in the document\'s '
        'footnote pane (Insert → Footnote), not typed inline.\n\n'
        '**For PDFs:** footnotes must appear in the bottom portion of each page '
        'and start with a number.'
    )
    st.stop()

# ---------------------------------------------------------------------------
# Summary metrics
# ---------------------------------------------------------------------------
total_fn   = metadata.get('total_footnotes', int(df['footnote_num'].nunique()))
real_df    = df[~df['is_id_cite']]
id_df      = df[df['is_id_cite']]
total_cit  = len(real_df)
needs_rev  = int(df['needs_review'].sum())
unique_src = int(real_df['canonical_citation'].nunique())
id_count   = len(id_df)

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric('Footnotes found',      total_fn)
col2.metric('Individual citations', total_cit)
col3.metric('Unique sources',       unique_src)
col4.metric('Short cites tracked', id_count,
            help='id., supra note, and infra note citations resolved to their source and counted toward it')
col5.metric('Need review',          needs_rev,
            delta=f'{needs_rev/max(total_cit,1)*100:.0f}%',
            delta_color='inverse')

st.divider()

# ---------------------------------------------------------------------------
# Preview table  (non-id. citations only)
# ---------------------------------------------------------------------------
preview_df = real_df.copy()
if show_review_only:
    preview_df = preview_df[preview_df['needs_review']]

display_cols = ['footnote_num', 'citation', 'needs_review', 'review_reason']
if show_raw:
    display_cols.append('raw_citation')

preview_df = preview_df[display_cols].rename(columns={
    'footnote_num':  'Footnote #',
    'citation':      'Citation',
    'needs_review':  'Needs Review',
    'review_reason': 'Review Reason',
    'raw_citation':  'Raw Footnote Text',
})

st.subheader(f'Preview — {len(preview_df):,} rows')

def highlight_review(row):
    if row.get('Needs Review'):
        return ['background-color: #FFF2CC'] * len(row)
    return [''] * len(row)

styled = preview_df.style.apply(highlight_review, axis=1)
st.dataframe(styled, use_container_width=True, height=420)

# ---------------------------------------------------------------------------
# Download button
# ---------------------------------------------------------------------------
st.divider()
with st.spinner('Building Excel workbook…'):
    excel_bytes = build_excel(df, metadata)

base_name = uploaded.name.rsplit('.', 1)[0]
out_name  = f'{base_name} — Citations.xlsx'

st.download_button(
    label='📥 Download Excel workbook',
    data=excel_bytes,
    file_name=out_name,
    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    type='primary',
)

st.caption(
    'The workbook contains three sheets: '
    '**Unique Sources** (all sources deduplicated and consolidated by base citation, '
    'with times-cited count and footnote numbers — includes id. citations counted toward '
    'the source they resolve to), '
    '**Summary** (total footnotes processed and total individual citations), and '
    '**Needs Review** (flagged rows with reason codes).'
)
