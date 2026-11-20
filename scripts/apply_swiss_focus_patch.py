#!/usr/bin/env python3
"""Integrate Swiss-address focus link into the already installed v1.8.4 app.

Idempotent: edits only two narrowly identified anchors, makes no DB/network
changes and aborts without writing when an expected anchor is absent.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROUTE = '''if st.session_state.get('_focus_route_swiss'):
    if not st.session_state.get('_focus_active', False):
        st.session_state['_focus_active'] = True
        st.rerun()
    render_focus_workspace(db)
    st.stop()
'''

BUTTON = '''        if jobs:
            if st.button('🎯 Fokusmodus für die Adressprüfung starten',
                         key='address_launch_focus', type='primary'):
                selected_job = st.session_state.get('address_service_selected_job')
                known = {j['job_id'] for j in jobs}
                job_id = (selected_job.get('job_id') if isinstance(selected_job, dict)
                          and selected_job.get('job_id') in known else jobs[0]['job_id'])
                st.session_state['_focus_route_swiss'] = job_id
                st.session_state['_focus_active'] = True
                st.rerun()
'''


def patch_files(main_file, address_file):
    main_path, address_path = Path(main_file), Path(address_file)
    main = main_path.read_text(encoding='utf-8')
    address = address_path.read_text(encoding='utf-8')
    original_main, original_address = main, address

    # Require 1.8.4 integration before touching either file.
    focus_dispatch = "if selected == '🎯 Fokusmodus':\n"
    if focus_dispatch not in main or 'from focus_ui import render_focus_workspace' not in main:
        raise RuntimeError('Zuerst den bestehenden v1.8.4-Fokusmodus in app/ui.py installieren. Keine Datei verändert.')
    if 'with review_tab:' not in address:
        raise RuntimeError('Adressprüfung-Reiter nicht gefunden. Keine Datei verändert.')
    if ROUTE not in main:
        main = main.replace(focus_dispatch, ROUTE + focus_dispatch, 1)
    if BUTTON not in address:
        match = re.search(r'^([ \t]*)with review_tab:\s*\n', address, flags=re.M)
        if not match:
            raise RuntimeError('Adressprüfungs-Reiter nicht erkennbar. Keine Datei verändert.')
        if len(match.group(1)) != 4:
            raise RuntimeError('Unerwartete Einrückung im Adressprüfungs-Reiter. Keine Datei verändert.')
        address = address[:match.end()] + BUTTON + address[match.end():]
    # Compile both new sources before any write.
    compile(main, str(main_path), 'exec')
    compile(address, str(address_path), 'exec')
    if main != original_main:
        main_path.write_text(main, encoding='utf-8')
    if address != original_address:
        address_path.write_text(address, encoding='utf-8')
    return main != original_main, address != original_address


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui-file', default='app/ui.py')
    parser.add_argument('--address-file', default='app/address_service_ui.py')
    args = parser.parse_args()
    changed = patch_files(args.ui_file, args.address_file)
    print('Schweizer Fokusmodus integriert.' if any(changed) else 'Bereits integriert – keine Änderung.')


if __name__ == '__main__':
    main()
