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

    st.subheader('AI-assisted extraction')
    api_key = st.text_input(
        'Anthropic API key (optional)',
        type='password',
        help='Provide a key to use Claude for more accurate prose/citation '
             'separation. Leave blank to use the built-in regex heuristics.',
    )
    if api_key:
        st.success('AI mode enabled — Claude will strip prose from footnotes.')
    else:
        st.info('Using regex heuristics. Add an API key for better accuracy.')

    st.divider()
    show_raw = st.checkbox('Show raw footnote text in preview', value=False)
    show_review_only = st.checkbox('Preview "Needs Review" rows only', value=False)
    st.divider()
    st.markdown(
        '**Review flags** are applied automatically to:\n'
        '- `id.` short-cites\n'
        '- `supra` / `infra` references\n'
        '- Citations containing *quoting* or *citing*\n'
        '- *Forthcoming* / *on-file* sources\n'
        '- Very short / noise fragments\n\n'
        'These rows are highlighted yellow in the export and '
        'collected on a dedicated **Needs Review** sheet.'
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
if api_key:
    try:
        import anthropic
        anthropic_client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        st.warning('`anthropic` package not installed. Falling back to regex mode.')

progress_bar = st.progress(0, text='Extracting citations…')

def on_progress(done: int, total: int):
    pct = done / total if total else 1.0
    label = f'Processing footnotes… {done}/{total}'
    progress_bar.progress(pct, text=label)

with st.spinner('Extracting and cleaning citations…'):
    try:
        df = extract_citations(
            uploaded.read(),
            uploaded.name,
            anthropic_client=anthropic_client,
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
total_fn   = int(df['footnote_num'].nunique())
total_cit  = len(df)
needs_rev  = int(df['needs_review'].sum())
clean_cit  = total_cit - needs_rev

col1, col2, col3, col4 = st.columns(4)
col1.metric('Footnotes found',        total_fn)
col2.metric('Individual citations',   total_cit)
col3.metric('Clean citations',        clean_cit)
col4.metric('Need review',            needs_rev,
            delta=f'{needs_rev/total_cit*100:.0f}%' if total_cit else '0%',
            delta_color='inverse')

st.divider()

# ---------------------------------------------------------------------------
# Preview table
# ---------------------------------------------------------------------------
preview_df = df.copy()
if show_review_only:
    preview_df = preview_df[preview_df['needs_review']]

display_cols = ['footnote_num', 'citation', 'needs_review']
if show_raw:
    display_cols.append('raw_citation')

preview_df = preview_df[display_cols].rename(columns={
    'footnote_num':  'Footnote #',
    'citation':      'Citation',
    'needs_review':  'Needs Review',
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
    excel_bytes = build_excel(df)

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
    'The workbook contains three sheets: **Citations** (all results), '
    '**Needs Review** (flagged rows with reason codes), and **Summary** '
    '(aggregate counts).'
)
