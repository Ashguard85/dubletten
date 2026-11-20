#!/usr/bin/env python3
"""Idempotently add the standalone focus workspace to an existing app/ui.py.

Runs in the LOCAL Git working tree and is committed before the Portainer build.
No database access, no network calls and no unrelated UI changes.
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path

IMPORT = 'from focus_ui import render_focus_workspace'
NAV = "    choices.append('🎯 Fokusmodus')"
DISPATCH = '''if selected == '🎯 Fokusmodus':
    if not st.session_state.get('_focus_active', False):
        st.session_state['_focus_active'] = True
        st.rerun()
    render_focus_workspace(db)
    st.stop()
if st.session_state.get('_focus_active', False):
    st.session_state['_focus_active'] = False
    st.rerun()

'''


def patch_file(ui_file):
    path = Path(ui_file)
    original = path.read_text(encoding='utf-8')
    source = original
    if IMPORT not in source:
        if 'import streamlit as st\n' not in source:
            raise RuntimeError('Streamlit-Import nicht gefunden. Keine Datei verändert.')
        source=source.replace('import streamlit as st\n', 'import streamlit as st\n'+IMPORT+'\n',1)
    if NAV not in source:
        # Exactly the workspace-choices assignment, not any other choices array.
        match=re.search(r'^([ \t]*)choices\s*=\s*\[\s*[\'\"]\+ Neuer Abgleich[^\n]*\n',source,re.M)
        if not match:
            raise RuntimeError('Arbeitsbereich-Auswahl nicht gefunden. Keine Datei verändert.')
        source=source[:match.end()] + NAV+'\n' + source[match.end():]
    if DISPATCH not in source:
        anchor='if show_swisstopo:\n'
        # Insert just before the handler, leaving the rest untouched.
        if anchor not in source:
            raise RuntimeError('swisstopo-Handler nicht gefunden. Keine Datei verändert.')
        source=source.replace(anchor,DISPATCH+anchor,1)
    # Hide the full dashboard header only during focused review, saving space.
    marker = "if not st.session_state.get('_focus_active', False):\n    st.title('🔎 CRM-Dublettenprüfung')"
    if marker not in source:
        lines = source.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.startswith("st.title('🔎 CRM-Dublettenprüfung')"):
                j = i + 1
                while j < len(lines) and (lines[j].startswith('st.caption(') or
                                            lines[j].startswith('st.info(')):
                    j += 1
                block = ''.join(lines[i:j])
                wrapped = "if not st.session_state.get('_focus_active', False):\n" + \
                          ''.join('    ' + l for l in lines[i:j])
                source = ''.join(lines[:i]) + wrapped + ''.join(lines[j:])
                break
    if source != original:
        path.write_text(source,encoding='utf-8')
    return source!=original


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--file',default='app/ui.py',help='Vorhandene Streamlit-UI')
    args=parser.parse_args()
    changed=patch_file(args.file)
    print('Fokusmodus integriert.' if changed else 'Fokusmodus war bereits integriert; unverändert.')


if __name__=='__main__':
    main()
