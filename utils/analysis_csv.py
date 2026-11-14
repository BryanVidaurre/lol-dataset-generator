"""CSV readers for analysis: patch versions are categorical text, never floats."""


def read_analysis_csv(path, **kwargs):
    import pandas as pd
    supplied = kwargs.pop('dtype', {})
    if not isinstance(supplied, dict):
        raise TypeError('dtype must be a column-to-type mapping')
    protected = {'match_id': 'string', 'patch': 'string', 'patch_group': 'string'}
    for key, value in supplied.items():
        if key in protected and value not in ('string', str, 'str', 'object'):
            raise ValueError(f'{key} must be read as text')
    return pd.read_csv(path, dtype={**supplied, **protected}, **kwargs)
