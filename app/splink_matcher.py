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
