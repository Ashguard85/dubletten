"""Validated, serialisable Splink settings. No user-provided SQL expressions."""
import hashlib
import json

BLOCK_LIMITS = {
    'uid': 2500, 'email': 1500, 'phone': 500,
    'zip_name': 450, 'zip_street': 250, 'city_name': 200,
    'name_head': 100, 'street_name': 150,
}
DEFAULT_BLOCKS = ['uid','email','phone','zip_name','zip_street','city_name','name_head','street_name']
DEFAULTS = {
    'split_house_number': True,
    'name_thresholds': [0.92,0.84],
    'street_distances': [2,5],
    'u_max_pairs': 300000,
    'em_max_pairs': 250000,
    'min_probability': 0.05,
    'blocking_fields': DEFAULT_BLOCKS[:],
}


def validate_config(config=None):
    c = {k: list(v) if isinstance(v, list) else v for k,v in DEFAULTS.items()}
    if config:
        unknown = set(config)-set(c)
        if unknown:
            raise ValueError(f'Unbekannte Splink-Einstellung: {sorted(unknown)}')
        c.update(config)
    if not isinstance(c['split_house_number'], bool):
        raise ValueError('split_house_number muss true/false sein')
    thresholds=c['name_thresholds']
    if not (isinstance(thresholds,list) and len(thresholds)==2 and
            all(isinstance(n,(int,float)) and 0.5<=n<=0.99 for n in thresholds) and
            thresholds[0]>thresholds[1]):
        raise ValueError('Namensschwellen müssen absteigend im Bereich 0.5–0.99 liegen.')
    distances=c['street_distances']
    if not (isinstance(distances,list) and len(distances)==2 and
            all(type(n) is int and 1<=n<=15 for n in distances) and
            distances[0]<distances[1]):
        raise ValueError('Strassendistanzen müssen aufsteigend zwischen 1 und 15 liegen.')
    if not type(c['u_max_pairs']) is int or not 10000 <= c['u_max_pairs'] <= 2_000_000:
        raise ValueError('u-Stichprobe ausserhalb des erlaubten Bereichs.')
    if not type(c['em_max_pairs']) is int or not 10000 <= c['em_max_pairs'] <= 1_000_000:
        raise ValueError('EM-Maximum ausserhalb des erlaubten Bereichs.')
    if not isinstance(c['min_probability'],(float,int)) or not 0.001<=c['min_probability']<=0.99:
        raise ValueError('Minimale Wahrscheinlichkeit ausserhalb des erlaubten Bereichs.')
    rules=c['blocking_fields']
    if not isinstance(rules,list) or len(rules)<2 or len(rules)!=len(set(rules)) or any(f not in BLOCK_LIMITS for f in rules):
        raise ValueError('Mindestens zwei bekannte Blocking-Felder auswählen.')
    c['name_thresholds']=[float(x) for x in thresholds]
    return c


def model_signature(config):
    """Fields that change the meaning of a pretrained Splink model."""
    c=validate_config(config)
    spec={k:c[k] for k in ('split_house_number','name_thresholds','street_distances','blocking_fields')}
    return hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()[:16]
