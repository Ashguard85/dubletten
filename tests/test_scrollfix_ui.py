from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_v16_has_three_modes_and_no_stack_of_detail_expanders():
    main = (ROOT / 'app' / 'ui.py').read_text()
    views = (ROOT / 'app' / 'review_ui.py').read_text()
    assert 'render_review(db, pid, is_splink, selected_model, bool(installed), ai_enabled=ai_enabled)' in main
    assert "VIEW_NAMES = ('Data Grid', 'Split-View', 'Review-Cockpit')" in views
    assert "st.segmented_control('Ansicht'" in views
    assert 'st.dataframe(df,' in views
    assert 'st.container(height=630, border=True)' in views
    assert '_cockpit(db,pid,rows,' in views
    assert 'with st.expander(title)' not in views
    assert "st.markdown('**Manuelle Entscheidung**')" in views


def test_streamlit_version_contains_review_apis_and_expander_fix():
    requirements = (ROOT / 'requirements.txt').read_text()
    assert 'streamlit>=1.63,<2' in requirements
