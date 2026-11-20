from pathlib import Path
p=Path('app/engine.py')
s=p.read_text()
s=s.replace('    db.commit()\n    return db\n', '    from migration import ensure_schema\n    ensure_schema(db)\n    db.commit()\n    return db\n',1)
s=s.replace("def import_project(db, title, frame_a, map_a, frame_b, map_b, engine='rapidfuzz'):","""def import_project(db, title, frame_a, map_a, frame_b, map_b, engine='rapidfuzz',
                   mode='legacy', a_ref_column='', a_exclude_column='',
                   exclude_values=(), empty_reference_markers=()):""")
s=s.replace("    validate_mapping(frame_a, map_a, 'A')", """    if mode not in ('legacy','dual'):
        raise ValueError('Ungültiger Abgleichmodus')
    if mode == 'dual' and (not a_ref_column or a_ref_column not in frame_a.columns):
        raise ValueError('Für die zwei CRM-Workflows muss die B-Referenzspalte aus A zugeordnet werden.')
    if a_exclude_column and a_exclude_column not in frame_a.columns:
        raise ValueError('Die ausgewählte Ausschlussspalte existiert nicht in CRM A.')
    validate_mapping(frame_a, map_a, 'A')""")
s=s.replace("        cur = db.execute('INSERT INTO projects(title,created_at,a_count,b_count,engine) VALUES(?,?,?,?,?)',\n                         (title[:140].strip() or 'CRM-Abgleich', now(), len(frame_a), len(frame_b), engine))", """        cur = db.execute('INSERT INTO projects(title,created_at,a_count,b_count,engine,mode,ref_column) VALUES(?,?,?,?,?,?,?)',
                         (title[:140].strip() or 'CRM-Abgleich', now(), len(frame_a), len(frame_b), engine,mode,a_ref_column))""")
s=s.replace('        for side, frame, mapping in [(\'A\', frame_a, map_a), (\'B\', frame_b, map_b)]:\n',"""        markers = {plain(v).casefold() for v in empty_reference_markers if plain(v)}
        exclusions = {plain(v).casefold() for v in exclude_values if plain(v)}
        for side, frame, mapping in [('A', frame_a, map_a), ('B', frame_b, map_b)]:
""")
s=s.replace("                n = to_record(raw)\n                batch.append((pid,side,raw['id'],", """                n = to_record(raw)
                b_ref = plain(original.get(a_ref_column,'')) if side=='A' and mode=='dual' else ''
                if b_ref.casefold() in markers:
                    b_ref = ''
                in_scope = int(not (side=='A' and a_exclude_column and
                                    plain(original.get(a_exclude_column,'')).casefold() in exclusions))
                batch.append((pid,side,raw['id'],""")
s=s.replace("n['street_number'], n['street_name'], n['house_number'], n['address_conflict']))", "n['street_number'], n['street_name'], n['house_number'], n['address_conflict'], b_ref, in_scope))")
s=s.replace("name_head,street_number,street_name,house_number,address_conflict) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", "name_head,street_number,street_name,house_number,address_conflict,b_reference,in_scope) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
# Most important: blocking should only process A without B reference; B always full.
s=s.replace("                for row in db.execute(f\"SELECT pk,{field} FROM records WHERE project_id=? AND side=? AND {field} != ''\", (pid,side)):", """                dual = db.execute('SELECT mode FROM projects WHERE project_id=?', (pid,)).fetchone()['mode'] == 'dual'
                restriction = " AND b_reference='' AND in_scope=1" if side=='A' and dual else ''
                for row in db.execute(f"SELECT pk,{field} FROM records WHERE project_id=? AND side=? AND {field} != ''"+restriction, (pid,side)):""")
s=s.replace("    if state=='bestaetigt':\n            clash", """    if state=='bestaetigt':
            from migration import is_dual, reference_conflict
            if is_dual(db,pid) and reference_conflict(db,pid,a_pk,b_pk):
                raise ValueError('Konflikt: Diese B-ID ist bereits als Referenz bei einer anderen A-Adresse hinterlegt.')
            clash""")
p.write_text(s)

p=Path('app/splink_matcher.py'); s=p.read_text()
s=s.replace("        rows = db.execute('''SELECT pk, original_json, name,street,postal,city,uid,email,phone,\n                              zip_name,zip_street,city_name,name_head,street_number,street_name,house_number\n                              FROM records WHERE project_id=? AND side=?''', (project_id, side))", """        from migration import is_dual
        extra = " AND b_reference='' AND in_scope=1" if side=='A' and is_dual(db,project_id) else ''
        rows = db.execute('''SELECT pk, original_json, name,street,postal,city,uid,email,phone,
                              zip_name,zip_street,city_name,name_head,street_number,street_name,house_number
                              FROM records WHERE project_id=? AND side=?'''+extra, (project_id, side))""")
s=s.replace("    if not n_a or not n_b:\n        raise ValueError('Beide CRM-Datenquellen müssen Datensätze enthalten.')", """    if not n_a or not n_b:
        from migration import is_dual
        if is_dual(db,project_id) and n_a==0:
            with db:
                db.execute('DELETE FROM pairs WHERE project_id=?',(project_id,))
                db.execute('UPDATE projects SET candidate_count=0,match_model_info=? WHERE project_id=?',
                           (json.dumps({'engine':'splink5','mode':'keine_A_ohne_referenz'}),project_id))
            return 0,0
        raise ValueError('Beide CRM-Datenquellen müssen Datensätze enthalten.')""")
p.write_text(s)
