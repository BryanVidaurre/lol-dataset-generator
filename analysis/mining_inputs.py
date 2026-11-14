"""Safe, explicit input selection for later experiments; no models are fitted."""
import json
from pathlib import Path

import duckdb

from analysis.prepare_mining import literal


def predictor_query(directory, split='train'):
    """Only allow listed predictors, never metadata or labels. Train by default."""
    if split not in ('train', 'validation', 'test'):
        raise ValueError('Choose train, validation or test; purged is excluded')
    directory = Path(directory)
    manifest = json.loads((directory / 'feature_manifest.json').read_text(encoding='utf-8'))
    features = manifest['strategic_states.parquet']['candidate_predictors']
    forbidden = set(manifest['forbidden_predictors'])
    if forbidden.intersection(features) or any(not c.replace('_', '').isalnum() for c in features):
        raise ValueError('Invalid predictor manifest')
    return ('SELECT ' + ','.join('"' + c + '"' for c in features) +
            f' FROM read_parquet({literal(directory / "strategic_states.parquet")}) WHERE split={literal(split)}')


def iter_sequence_prefixes(path, *, cutoff_ms, split='train', batch_size=64):
    """Yield itemset sequences up to a required cutoff, one match/team at a time.

    Itemset-aware PrefixSpan/SPMF can consume `itemsets`. Do not flatten ties
    into an artificial temporal order. Targets must be joined separately.
    """
    if type(cutoff_ms) is not int or cutoff_ms < 0 or batch_size < 1:
        raise ValueError('A nonnegative integer cutoff and positive batch size are required')
    if split not in ('train', 'validation', 'test'):
        raise ValueError('Invalid split')
    db = duckdb.connect()
    try:
        cursor = db.execute(f'SELECT match_id,team_id,timestamps_ms,itemsets FROM read_parquet({literal(path)}) WHERE split={literal(split)}')
        while batch := cursor.fetchmany(batch_size):
            for mid, team, stamps, itemsets in batch:
                keep = [i for i, stamp in enumerate(stamps) if stamp <= cutoff_ms]
                yield {'match_id': mid, 'team_id': team,
                       'timestamps_ms': [stamps[i] for i in keep],
                       'itemsets': [itemsets[i] for i in keep]}
    finally:
        db.close()
