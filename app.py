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
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header('⚖️ GLJ Citation Extractor')
    st.markdown(
        'A tool for extracting legal citations from law review article footnotes.'
    )
    st.divider()

    st.subheader('How it works')
    st.markdown(
        '1. **Upload** a Word (`.docx`) or PDF article\n'
        '2. The app reads every footnote from the document\n'
        '3. Each footnote is **split** into individual citations wherever it finds:\n'
        '   - A semicolon (`;`)\n'
        '   - An `id.` short-cite\n'
        '   - A `see also` signal\n'
        '   - A `(quoting` parenthetical\n'
        '   - A `see supra` or `see infra` cross-reference\n'
        '   - A `see, e.g.`, `see generally`, or `but see` signal\n'
        '4. **Download** the results as an Excel file — one citation per row'
    )
    st.divider()

    st.subheader('Privacy')
    st.markdown(
        '**Your document stays on your device.** Citations are extracted '
        'using a local regex pipeline — no document content is sent to any '
        'AI service or external server.'
    )
    st.divider()

    st.subheader('Tips')
    st.markdown(
        '- **Word files:** footnotes must be in the document\'s footnote pane '
        '(Insert → Footnote), not typed inline.\n'
        '- **PDFs:** footnotes must appear at the bottom of each page and start '
        'with a number.'
    )
    st.divider()

    st.markdown(
        '[![GitHub](https://img.shields.io/badge/GitHub-sec178%2Fglj--citation--extractor-blue?logo=github)]'
        '(https://github.com/sec178/glj-citation-extractor)'
    )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
st.title('⚖️ GLJ Citation Extractor')
st.markdown(
    'Upload a **Word (.docx)** or **PDF** article to extract every footnote citation '
    'into a clean Excel file — one source per row.'
)
st.divider()

uploaded = st.file_uploader(
    'Upload your article',
    type=['pdf', 'docx'],
)

if uploaded is None:
    st.info('Upload a file above to get started.')
    st.stop()

with st.spinner('Extracting citations…'):
    try:
        df, metadata = extract_citations(uploaded.read(), uploaded.name)
    except Exception as e:
        st.error(f'Extraction failed: {e}')
        st.stop()

if df.empty:
    st.warning(
        'No footnotes were detected in this file.\n\n'
        '**For Word documents:** make sure footnotes are in the document\'s '
        'footnote pane (Insert → Footnote), not typed inline.\n\n'
        '**For PDFs:** footnotes must appear in the bottom portion of each page '
        'and start with a number.'
    )
    st.stop()

col1, col2 = st.columns(2)
col1.metric('Footnotes processed', metadata['total_footnotes'])
col2.metric('Citations extracted', len(df))

st.divider()
st.subheader('Preview')

# Force text wrapping in the dataframe cells
st.markdown(
    '<style>'
    '.stDataFrame [role="gridcell"] { white-space: normal !important;'
    ' word-break: break-word !important; }'
    '</style>',
    unsafe_allow_html=True,
)

st.dataframe(
    df[['citation']].rename(columns={'citation': 'Sources'}),
    use_container_width=True,
    height=420,
    column_config={
        'Sources': st.column_config.TextColumn('Sources', width='large'),
    },
)

st.divider()
excel_bytes = build_excel(df)
base_name = uploaded.name.rsplit('.', 1)[0]
st.download_button(
    label='📥 Download Excel',
    data=excel_bytes,
    file_name=f'{base_name}_sources.xlsx',
    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    type='primary',
)
