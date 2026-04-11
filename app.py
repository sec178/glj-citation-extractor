"""
GLJ Citation Extractor — Streamlit app entry point.
"""

import pandas as pd
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
    st.markdown(
        '**Excluded from output:**\n'
        '- `id.` / `id. at X` short-cites\n'
        '- `supra note X` / `infra note X` references\n'
        '- Bare pincites (`at X`)\n'
        '- Citation signals (`See`, `Cf.`, `Compare`, etc.)\n'
        '- Long parentheticals (20+ chars), unless they contain *quoting* or *citing*\n\n'
        '**Sources are deduplicated** by base citation — '
        'different pincites of the same source appear once.'
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
unique_src = int(real_df['canonical_citation'].nunique())

col1, col2 = st.columns(2)
col1.metric('Footnotes processed', total_fn)
col2.metric('Unique sources found', unique_src)

st.divider()

# ---------------------------------------------------------------------------
# Preview — alphabetical list of unique cleaned citations
# ---------------------------------------------------------------------------
sources = sorted(
    real_df['canonical_citation'].dropna().unique(),
    key=str.lower,
)
preview_df = pd.DataFrame({'Citation': sources})

st.subheader(f'Sources — {len(sources):,}')
st.dataframe(preview_df, use_container_width=True, height=420)

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
    'The workbook contains one sheet — **Sources** — with every unique cleaned citation '
    'in alphabetical order, one per row.'
)
