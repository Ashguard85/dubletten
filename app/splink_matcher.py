"""Splink 5 record linkage between existing SQLite CRM A/B records.

No network calls: input is loaded from local SQLite into in-process DuckDB.
No CRM data is included in the Docker build or written to the Git repository.
"""
from __future__ import annotations

from collections import Counter
import json
import logging
from importlib.metadata import version as package_version
import math

import pandas as pd

from engine import BLOCKS, now, norm_name, score_pair

LOGGER = logging.getLogger(__name__)
COLUMNS = ('unique_id', 'name', 'street', 'postal', 'city', 'uid', 'email', 'phone',
           'zip_name', 'zip_street', 'city_name', 'name_head')


def _load(db, project_id):
    """Keep source PK intact; only normalised matching columns enter Splink."""
    result = {}
    for side in ('A', 'B'):
        rows = db.execute('''SELECT pk, original_json, name,street,postal,city,uid,email,phone,
                              zip_name,zip_street,city_name,name_head,street_number
                              FROM records WHERE project_id=? AND side=?''', (project_id, side))
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


def _cap_block_fanout(frames, max_candidates):
    """Disable overly broad keys in both sources, and preflight candidate growth.

    Upper bound is the sum of pairs over all rules, not their deduplicated union.
    This is deliberately conservative to prevent unexpectedly large joins.
    """
    candidate_upper_bound = 0
    blocked = []
    for field, fanout in BLOCKS:
        a, b = frames['A'][field], frames['B'][field]
        counts_a = Counter(x for x in a if x is not None)
        counts_b = Counter(x for x in b if x is not None)
        ignored = {key for key in counts_a.keys() & counts_b.keys()
                   if counts_a[key] * counts_b[key] > fanout}
        for side in ('A', 'B'):
            if ignored:
                frames[side].loc[frames[side][field].isin(ignored), field] = None
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


def _comparisons(cl, *, baseline=False):
    """Fixed m/u weights only for tiny test datasets without reliable EM training.

    These illustrative priors are NOT empirically calibrated probabilities.
    Normal-size datasets use estimated u/m parameters instead.
    """
    def cmp(creator, m, u):
        return creator.configure(m_probabilities=m, u_probabilities=u) if baseline else creator

    return [
        cmp(cl.JaroWinklerAtThresholds('name', [0.92, 0.84]),
            [0.72, 0.17, 0.08, 0.03], [0.0001, 0.0006, 0.004, 0.9953]),
        cmp(cl.LevenshteinAtThresholds('street', [2, 5]),
            [0.75, 0.10, 0.10, 0.05], [0.0003, 0.001, 0.005, 0.9937]),
        cmp(cl.ExactMatch('postal'), [0.88, 0.12], [0.012, 0.988]),
        cmp(cl.JaroWinklerAtThresholds('city', [0.95, 0.85]),
            [0.85, 0.09, 0.04, 0.02], [0.03, 0.01, 0.04, 0.92]),
        cmp(cl.ExactMatch('uid'), [0.94, 0.06], [0.0001, 0.9999]),
        cmp(cl.ExactMatch('email'), [0.88, 0.12], [0.0004, 0.9996]),
        cmp(cl.ExactMatch('phone'), [0.84, 0.16], [0.001, 0.999]),
    ]


def _new_linker(frames, *, baseline, prior):
    import splink.comparison_library as cl
    from splink import DuckDBAPI, Linker, SettingsCreator, block_on

    api = DuckDBAPI()
    inputs = [api.register(frames[k], dataset_display_name=f'CRM_{k}') for k in ('A', 'B')]
    settings = SettingsCreator(
        link_type='link_only',
        unique_id_column_name='unique_id',
        probability_two_random_records_match=prior,
        comparisons=_comparisons(cl, baseline=baseline),
        blocking_rules_to_generate_predictions=[block_on(f) for f, _ in BLOCKS],
        retain_matching_columns=False,
        retain_intermediate_calculation_columns=False,
    )
    return Linker(inputs, settings)


def _train(linker, n_a, n_b, progress):
    """Learn u from random pairs and m with two different EM blocking rules."""
    from splink import block_on
    progress('Splink: u-Parameter per Zufallsstichprobe schätzen')
    linker.training.estimate_u_using_random_sampling(
        max_pairs=min(300_000, n_a * n_b), seed=42, min_count_per_level=15
    )
    progress('Splink: m-Parameter mit EM trainieren (PLZ)')
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on('postal'), max_pairs=250_000
    )
    progress('Splink: weitere EM-Schätzung (Namenspräfix)')
    linker.training.estimate_parameters_using_expectation_maximisation(
        block_on('name_head'), max_pairs=250_000
    )
           

def run_splink_matching(db, project_id, minimum_probability=0.05,
                        max_candidates=400_000, progress=None):
    """Generate, score and persist cross-CRM pairs with Splink 5.

    Returns (upper bound on candidate comparisons, kept pairs).
    Failures never silently change engine to RapidFuzz. Since UI only invokes
    this on newly imported projects, its transaction is straightforward.
    """
    progress = progress or (lambda message: None)
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
        raise ValueError('Beide CRM-Datenquellen müssen Datensätze enthalten.')
    upper, block_stats = _cap_block_fanout(frames, max_candidates)
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
    if tiny_dataset:
        progress('Splink: kleine Testmenge, feste Modellparameter (nicht kalibriert)')
        linker = _new_linker(frames, baseline=True, prior=prior)
        mode = 'feste_priors_kleine_stichprobe'
    else:
        linker = _new_linker(frames, baseline=False, prior=prior)
        try:
            _train(linker, n_a, n_b, progress)
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
            'blocking_pair_upper_bound': upper, 'blocking': block_stats}
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
        db.execute('UPDATE projects SET candidate_count=?, match_model_info=? WHERE project_id=?',
                   (kept, json.dumps(info, ensure_ascii=False), project_id))
    return upper, kept
