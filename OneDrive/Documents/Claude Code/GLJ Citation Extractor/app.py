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

st.title('⚖️ GLJ Citation Extractor')
st.markdown(
    'Upload a **Word (.docx)** or **PDF** article. '
    'Each footnote is split into individual citations on semicolons, `id.`, and `see also`. '
    'Download the results as an Excel file.'
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
st.dataframe(df[['footnote_num', 'citation']], use_container_width=True, height=420)

st.divider()
excel_bytes = build_excel(df)
base_name = uploaded.name.rsplit('.', 1)[0]
st.download_button(
    label='📥 Download Excel',
    data=excel_bytes,
    file_name=f'{base_name} — Citations.xlsx',
    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    type='primary',
)
