from pathlib import Path

def test_scrollfix_presents_one_pair_instead_of_many_expanders():
    src = (Path(__file__).resolve().parents[1] / 'app' / 'ui.py').read_text()
    assert "st.selectbox('Treffer auswählen (Details darunter)'" in src
    assert "st.button('← Vorheriger Treffer'" in src
    assert "st.button('Nächster Treffer →'" in src
    assert 'with st.expander(title)' not in src
    assert "if st.checkbox('🔎 Feldweise Diagnose anzeigen'" in src


def test_streamlit_version_has_2026_expander_fix():
    requirements = (Path(__file__).resolve().parents[1] / 'requirements.txt').read_text()
    assert 'streamlit>=1.63,<2' in requirements
