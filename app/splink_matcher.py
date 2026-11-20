"""Splink 5 record linkage between existing SQLite CRM A/B records.

No network calls: input is loaded from local SQLite into in-process DuckDB.
No CRM data is included in the Docker build or written to the Git repository.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from uuid import uuid4
import random
import json
import logging
from importlib.metadata import version as package_version
import math

import pandas as pd

from engine import now, score_pair
from match_config import BLOCK_LIMITS, DEFAULT_BLOCKS, validate_config, model_signature

LOGGER = logging.getLogger(__name__)
COLUMNS = ('unique_id', 'name', 'street', 'postal', 'city', 'uid', 'email', 'phone',
           'zip_name', 'zip_street', 'city_name', 'name_head', 'street_name', 'house_number')


def _load(db, project_id):
    """Keep source PK intact; only normalised matching columns enter Splink."""
    result = {}
    for side in ('A', 'B'):
        from migration import is_dual
        extra = " AND b_reference='' AND in_scope=1" if side=='A' and is_dual(db,project_id) else ''
        rows = db.execute('''SELECT pk, original_json, name,street,postal,city,uid,email,phone,
                              zip_name,zip_street,city_name,name_head,street_number,street_name,house_number
                              FROM records WHERE project_id=? AND side=?'''+extra, (project_id, side))
        data = []
        for row in rows:
            obj = {k: row[k] for k in COLUMNS if k != 'unique_id'}
            obj['unique_id'] = row['pk']
            # Missing values must be NULL, never empty strings: comparing empty
            # values as exact matches produces misleading model evidence.
            obj = {key: (value if value != '' else None) for key, value in obj.items()}
            data.append(obj)
        result[side] = pd.DataFrame.from_records(data, columns=COLUMNS)
    return result


def _cap_block_fanout(frames, max_candidates, fields=None):
    """Disable overly broad keys in both sources, and preflight candidate growth.

    Upper bound is the sum of pairs over all rules, not their deduplicated union.
    This is deliberately conservative to prevent unexpectedly large joins.
    """
    candidate_upper_bound = 0
    blocked = []
    for field in (fields or DEFAULT_BLOCKS):
        fanout = BLOCK_LIMITS[field]
        a, b = frames['A'][field], frames['B'][field]
        counts_a = Counter(x for x in a if x is not None)
        counts_b = Counter(x for x in b if x is not None)
        ignored = {key for key in counts_a.keys() & counts_b.keys()
                   if counts_a[key] * counts_b[key] > fanout}
        # Maintain separate, fanout-filtered blocking keys. Never erase useful
        # similarity evidence from e.g. email, UID, street_name columns.
        for side in ('A', 'B'):
            frames[side]['block_' + field] = frames[side][field].where(
                ~frames[side][field].isin(ignored), other=None)
        n = sum(counts_a[key] * counts_b[key] for key in counts_a.keys() & counts_b.keys()
                if key not in ignored)
        candidate_upper_bound += n
        blocked.append({'field': field, 'max_pairs_per_key': fanout,
                        'disabled_ambiguous_keys': len(ignored), 'pair_upper_bound': n})
        if candidate_upper_bound > max_candidates:
            raise ValueError(
                f'Splink-Blocking würde bis zu {candidate_upper_bound:,} Paare erzeugen '
                f'(Limit {max_candidates:,}). Bitte Dublettenabgleich in Teilmengen ausführen '
                'oder strengere Blocking-Regeln einstellen.'
            )
    return candidate_upper_bound, blocked


def _comparisons(cl, *, baseline=False, config=None):
    """Comparison set: optional house-number evidence independent from street."""
    cfg=validate_config(config)
    def cmp(creator, m, u):
        return creator.configure(m_probabilities=m, u_probabilities=u) if baseline else creator

    comparisons = [
        cmp(cl.JaroWinklerAtThresholds('name', cfg['name_thresholds']),
            [0.72,0.17,0.08,0.03], [0.0001,0.0006,0.004,0.9953]),
        cmp(cl.LevenshteinAtThresholds('street_name' if cfg['split_house_number'] else 'street',
                                      cfg['street_distances']),
            [0.75,0.10,0.10,0.05], [0.0003,0.001,0.005,0.9937]),
        cmp(cl.ExactMatch('postal'), [0.88,0.12], [0.012,0.988]),
        cmp(cl.JaroWinklerAtThresholds('city', [0.95,0.85]),
            [0.85,0.09,0.04,0.02], [0.03,0.01,0.04,0.92]),
        cmp(cl.ExactMatch('uid'), [0.94,0.06], [0.0001,0.9999]),
        cmp(cl.ExactMatch('email'), [0.88,0.12], [0.0004,0.9996]),
        cmp(cl.ExactMatch('phone'), [0.84,0.16], [0.001,0.999]),
    ]
    if cfg['split_house_number']:
        comparisons.insert(2, cmp(cl.ExactMatch('house_number'), [0.91,0.09], [0.02,0.98]))
    return comparisons


def _new_linker(frames, *, baseline, prior, config=None, saved_model_path=None):
    import splink.comparison_library as cl
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on

    cfg=validate_config(config)
    api = DuckDBAPI()
    inputs = [api.register(frames[k], dataset_display_name=f'CRM_{k}') for k in ('A','B')]
    if saved_model_path:
        return Linker(inputs, settings=str(saved_model_path))
    settings = SettingsCreator(
        link_type='link_only', unique_id_column_name='unique_id',
        probability_two_random_records_match=prior,
        comparisons=_comparisons(cl, baseline=baseline, config=cfg),
        blocking_rules_to_generate_predictions=[block_on('block_' + f) for f in cfg['blocking_fields']],
        retain_matching_columns=False,
        retain_intermediate_calculation_columns=False,
    )
    return Linker(inputs, settings)


def _train(linker, n_a, n_b, progress, config=None):
    """Statistical u and EM-based m estimation, with bounded sample sizes."""
    from splink import block_on
    cfg=validate_config(config)
    progress('Splink: u-Parameter mittels Zufallsstichprobe schätzen')
    linker.training.estimate_u_using_random_sampling(
        max_pairs=min(cfg['u_max_pairs'], n_a*n_b), seed=42, min_count_per_level=15
    )
    progress('Splink: EM-Schätzung mit PLZ-Blocking')
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on('postal'), max_pairs=cfg['em_max_pairs'])
    progress('Splink: EM-Schätzung mit Namenspräfix')
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on('name_head'), max_pairs=cfg['em_max_pairs'])


def _models_dir(db):
    db_path = db.execute('PRAGMA database_list').fetchone()['file']
    folder = Path(db_path).resolve().parent / 'models'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def available_models(db):
    return [dict(r) for r in db.execute("""SELECT m.model_id,m.project_id,m.created_at,m.config_json,
        m.model_path,m.training_type,m.statistics_json,p.title
        FROM trained_models m JOIN projects p ON m.project_id=p.project_id
        ORDER BY m.model_id DESC""")]


def _resolve_model(db, model_id):
    row = db.execute('SELECT * FROM trained_models WHERE model_id=?',(int(model_id),)).fetchone()
    if row is None:
        raise ValueError('Gespeichertes Splink-Modell nicht gefunden.')
    path=Path(row['model_path']).resolve()
    if not path.is_relative_to(_models_dir(db).resolve()) or not path.is_file():
        raise ValueError('Splink-Modell liegt nicht im privaten Modellverzeichnis.')
    return path, validate_config(json.loads(row['config_json'])), dict(row)


def _save_model(db, linker, project_id, config, training_type, statistics):
    """Persist a JSON artifact under the existing local volume; never in Git."""
    folder = _models_dir(db)
    final=folder / f'projekt_{project_id}_{uuid4().hex[:12]}.json'
    tmp=folder / f'.{final.name}.tmp'
    try:
        settings=linker.misc.save_model_to_json()
        with tmp.open('x', encoding='utf-8') as out:
            json.dump(settings,out,ensure_ascii=False,indent=2)
        tmp.chmod(0o600)
        tmp.replace(final)
        db.execute("""INSERT INTO trained_models(project_id,created_at,config_json,model_path,
            statistics_json,training_type) VALUES(?,?,?,?,?,?)""",
            (project_id, now(), json.dumps(validate_config(config)), str(final),
             json.dumps(statistics,ensure_ascii=False), training_type))
        return db.execute('SELECT last_insert_rowid()').fetchone()[0]
    except Exception:
        tmp.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise


def _review_labels(db, project_id):
    return [(r['a_pk'],r['b_pk'],r['state']) for r in db.execute("""
         SELECT a_pk,b_pk,state FROM pairs WHERE project_id=?
         AND state IN ('bestaetigt','abgelehnt') ORDER BY a_pk,b_pk""",(project_id,))]


def train_model_from_reviews(db, project_id, config=None, progress=None):
    """Train NEW model artifact from confirmed labels, without altering existing pairs.

    Splink's direct m-estimation treats every supplied label as a positive
    match. Rejected pairs are therefore kept OUT of training, and used only
    for holdout review. Labelled examples are candidate-biased; metrics from
    them cannot be generalised to all records.
    """
    cfg=validate_config(config)
    progress=progress or (lambda _:None)
    labels=_review_labels(db,project_id)
    positive=[(a,b) for a,b,state in labels if state=='bestaetigt']
    negative=[(a,b) for a,b,state in labels if state=='abgelehnt']
    if len(positive)<20:
        raise ValueError('Mindestens 20 manuell bestätigte Paare für Training erforderlich.')
    rng=random.Random(42)
    rng.shuffle(positive)
    holdout_n=max(4,round(0.20*len(positive)))
    train_positive=positive[holdout_n:]
    validation_positive=positive[:holdout_n]
    frames=_load(db,project_id)
    bound, stats=_cap_block_fanout(frames, 400_000, cfg['blocking_fields'])
    n_a,n_b=len(frames['A']),len(frames['B'])
    prior=max(1e-7,min(0.10,0.5/max(n_a,n_b)))
    linker=_new_linker(frames,baseline=False,prior=prior,config=cfg)
    progress('u-Parameter aus Stichprobe schätzen')
    linker.training.estimate_u_using_random_sampling(
        max_pairs=min(cfg['u_max_pairs'], n_a*n_b),seed=42,min_count_per_level=15)
    progress(f'm-Parameter aus {len(train_positive)} bestätigten Paaren schätzen')
    labels_df=pd.DataFrame([{'source_dataset_l':'CRM_A','unique_id_l':a,
                             'source_dataset_r':'CRM_B','unique_id_r':b}
                            for a,b in train_positive])
    linker.table_management.register_table(labels_df,'crm_confirmed_training_labels',overwrite=True)
    linker.training.estimate_m_from_pairwise_labels('crm_confirmed_training_labels')
    # Validation only against held-out, manually labelled candidates. No training leakage.
    holdout={pair:True for pair in validation_positive}
    holdout.update({pair:False for pair in negative})
    positives_flagged=negatives_flagged=0
    if holdout and bound:
        progress('Zurückgehaltene manuelle Entscheidungen prüfen (nur Stichprobe)')
        prediction=linker.inference.predict(threshold_match_probability=cfg['min_probability'])
        rel=prediction.as_duckdbpyrelation().project('unique_id_l,unique_id_r,source_dataset_l,source_dataset_r')
        for l,r,src_l,src_r in rel.iter_rows(batch_size=2000):
            pair=(int(l),int(r)) if src_l=='CRM_A' else (int(r),int(l))
            if pair in holdout:
                if holdout[pair]: positives_flagged+=1
                else: negatives_flagged+=1
    results={'positive_training':len(train_positive),'positive_holdout':len(validation_positive),
             'negative_holdout':len(negative), 'positive_holdout_detected':positives_flagged,
             'negative_holdout_flagged':negatives_flagged,
             'blocked_upper_bound':bound,'blocking':stats,
             'threshold':cfg['min_probability'], 'warning':'Nicht repräsentative, nur manuell geprüfte Kandidaten; keine globale Präzision/Recall.'}
    with db:
        model_id=_save_model(db,linker,project_id,cfg,'manuelle_labels',results)
    return model_id,results


def run_splink_matching(db, project_id, minimum_probability=0.05,
                        max_candidates=400_000, progress=None, config=None, model_id=None):
    """Generate, score and persist cross-CRM pairs with Splink 5.

    Returns (upper bound on candidate comparisons, kept pairs).
    Failures never silently change engine to RapidFuzz. Since UI only invokes
    this on newly imported projects, its transaction is straightforward.
    """
    progress = progress or (lambda message: None)
    cfg = validate_config(config)
    if model_id is not None:
        model_path, saved_cfg, _ = _resolve_model(db, model_id)
        if model_signature(saved_cfg) != model_signature(cfg):
            raise ValueError('Das ausgewählte Modell verwendet andere Vergleichs-/Blocking-Regeln.')
    else:
        model_path = None
    if not 0 < minimum_probability < 1:
        raise ValueError('Splink-Mindestwahrscheinlichkeit muss zwischen 0 und 1 liegen.')
    try:
        import splink
    except ImportError as exc:
        raise RuntimeError('Splink 5 ist nicht installiert. Image mit requirements.txt neu bauen.') from exc
    splink_version = package_version('splink')
    if not splink_version.startswith('5.'):
        raise RuntimeError(f'Splink 5 erforderlich, gefunden: {splink_version}')

    progress('Lade normalisierte CRM-Daten aus der lokalen Datenbank')
    frames = _load(db, project_id)
    n_a, n_b = len(frames['A']), len(frames['B'])
    if not n_a or not n_b:
        from migration import is_dual
        if is_dual(db,project_id) and n_a==0:
            with db:
                db.execute('DELETE FROM pairs WHERE project_id=?',(project_id,))
                db.execute('UPDATE projects SET candidate_count=0,match_model_info=? WHERE project_id=?',
                           (json.dumps({'engine':'splink5','mode':'keine_A_ohne_referenz'}),project_id))
            return 0,0
        raise ValueError('Beide CRM-Datenquellen müssen Datensätze enthalten.')
    upper, block_stats = _cap_block_fanout(frames, max_candidates, cfg['blocking_fields'])
    if upper == 0:
        with db:
            db.execute('UPDATE projects SET candidate_count=0, match_model_info=? WHERE project_id=?',
                       (json.dumps({'engine': 'splink5', 'mode': 'keine_kandidaten',
                                    'blocking': block_stats}), project_id))
        return 0, 0

    # The prior is an explicit practical assumption, not a learned calibration.
    prior = max(1e-7, min(0.10, 0.5 / max(n_a, n_b)))
    tiny_dataset = min(n_a, n_b) < 500 or n_a * n_b < 250_000
    # A tiny dataset cannot support meaningful EM; use openly labelled priors.
    if model_path is not None:
        progress(f'Gespeichertes Modell #{model_id} laden (kein erneutes Training)')
        linker = _new_linker(frames, baseline=False, prior=prior, config=cfg, saved_model_path=model_path)
        mode = 'gespeichertes_modell'
    elif tiny_dataset:
        progress('Splink: kleine Testmenge, feste Modellparameter (nicht kalibriert)')
        linker = _new_linker(frames, baseline=True, prior=prior, config=cfg)
        mode = 'feste_priors_kleine_stichprobe'
    else:
        linker = _new_linker(frames, baseline=False, prior=prior, config=cfg)
        try:
            _train(linker, n_a, n_b, progress, cfg)
        except Exception as exc:
            raise RuntimeError(
                'Splink-Training fehlgeschlagen; kein heuristischer Ersatz wurde '
                'still verwendet. Bitte Logs und Qualität der Daten prüfen. '
                f'Details: {str(exc)[:450]}'
            ) from exc
        mode = 'u_sampling_em_training'

    progress('Splink: CRM A mit CRM B probabilistisch vergleichen')
    predictions = linker.inference.predict(threshold_match_probability=minimum_probability)
    # Only retrieve the required few columns to avoid materialising wide rows.
    rel = predictions.as_duckdbpyrelation().project(
        'unique_id_l, unique_id_r, source_dataset_l, source_dataset_r, match_probability'
    )
    ref_a = {int(r['pk']): r for r in db.execute(
        'SELECT pk,name,street,postal,city,uid,email,phone,street_number FROM records '
        'WHERE project_id=? AND side="A"', (project_id,))}
    ref_b = {int(r['pk']): r for r in db.execute(
        'SELECT pk,name,street,postal,city,uid,email,phone,street_number FROM records '
        'WHERE project_id=? AND side="B"', (project_id,))}
    info = {'engine': 'splink5', 'mode': mode, 'splink_version': splink_version,
            'training_prior_assumption': prior, 'min_probability': minimum_probability,
            'blocking_pair_upper_bound': upper, 'blocking': block_stats,
            'config': cfg, 'reused_model_id': model_id}
    kept = 0
    with db:
        db.execute('DELETE FROM pairs WHERE project_id=?', (project_id,))
        # Transaction: if storing any output fails, roll back all new pair rows.
        # DuckDB 2.0 removed fetchmany() from DuckDBPyRelation. iter_rows()
        # executes the relation once and streams rows in bounded batches;
        # rel.limit(n, offset).fetchall() would re-execute the query per batch.
        to_write = []
        for left_pk, right_pk, left_source, right_source, probability in rel.iter_rows(batch_size=2000):
            if left_source == 'CRM_A' and right_source == 'CRM_B':
                a_pk, b_pk = int(left_pk), int(right_pk)
            elif left_source == 'CRM_B' and right_source == 'CRM_A':
                a_pk, b_pk = int(right_pk), int(left_pk)
            else:
                raise RuntimeError('Splink hat kein gültiges A/B-Datensatzpaar geliefert.')
            prob = float(probability)
            if not math.isfinite(prob):
                raise RuntimeError('Splink lieferte eine nicht endliche Matchwahrscheinlichkeit.')
            heuristic, reason = score_pair(ref_a[a_pk], ref_b[b_pk])
            # UI score is 100 x model probability; it is not a validated
            # precision estimate. The original heuristic is shown in reason.
            score = round(100 * prob)
            to_write.append((project_id, a_pk, b_pk, score,
                             f'Splink 5 · RapidFuzz {heuristic}/100 · {reason}',
                             now(), prob))
            if len(to_write) == 2000:
                db.executemany('''INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at,
                                   match_probability) VALUES(?,?,?,?,?,?,?)''', to_write)
                kept += len(to_write)
                to_write.clear()
        if to_write:
            db.executemany('''INSERT INTO pairs(project_id,a_pk,b_pk,score,reason,updated_at,
                               match_probability) VALUES(?,?,?,?,?,?,?)''', to_write)
            kept += len(to_write)
        if model_path is None:
            model_saved = _save_model(db, linker, project_id, cfg, mode, {'blocking_upper_bound':upper})
            info['saved_model_id'] = model_saved
        db.execute('UPDATE projects SET candidate_count=?, match_model_info=? WHERE project_id=?',
                   (kept, json.dumps(info, ensure_ascii=False), project_id))
    return upper, kept
