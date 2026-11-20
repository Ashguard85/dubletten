"""Safe display-only Swiss address helpers for focus mode.

These functions never mutate original input or make a swisstopo lookup. They
only explain evidence already stored in the offline address-checking job.
"""
from __future__ import annotations

import json
import re
import unicodedata
from html import escape

FIELDS = (
    ('strasse', 'Strasse'), ('hausnummer', 'Hausnummer'),
    ('plz', 'PLZ'), ('ort', 'Ort'),
)


def _text(value):
    return '' if value is None else str(value)


def _eq(left, right):
    """Presentation match: ignore only case and trivial white-space variations."""
    def norm(value):
        return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', _text(value)).strip()).casefold()
    return norm(left) == norm(right)


def original_fields(row, mapping=None):
    """Extract source values by original column names, not normalized fields."""
    try:
        raw = json.loads(row['original_json'] or '{}')
        addr = json.loads(row['normalized_json'] or '{}')
    except (TypeError, json.JSONDecodeError, KeyError) as exc:
        raise ValueError('Originaldaten sind nicht lesbar.') from exc
    mapping = mapping or {}
    def read(field, fallback):
        column = mapping.get(field)
        if column and column in raw:
            return _text(raw[column])
        return _text(addr.get(fallback, ''))
    return {
        'strasse': read('street', 'street_source'),
        'hausnummer': read('house_number', 'house_source'),
        'plz': read('postcode', 'postal_source'),
        'ort': read('city', 'city_source'),
        'land': read('country', 'country'),
        'original': raw,
        'normalized': addr,
    }


def original_address_lines(source):
    """Build a readable source excerpt, retaining original spelling and order.

    An explicitly separate house field is appended once. If the street field
    already contains the same number, avoid doubling it. Source JSON remains
    fully available unmodified in the optional full-original expander.
    """
    street = source['strasse'].strip()
    house = source['hausnummer'].strip()
    # No guess about number when separate source house column was blank.
    if house and not re.search(r'(?:^|[\s,])' + re.escape(house) + r'\s*$', street, re.I):
        street = f'{street} {house}' if street else house
    place = ' '.join(x.strip() for x in (source['plz'], source['ort']) if x.strip())
    return (street or '—', place or '—')


def display_values(addr, street_without_number):
    return {
        'strasse': _text(street_without_number),
        'hausnummer': _text(addr.get('house', '')),
        'plz': _text(addr.get('postal_source') or addr.get('postal', '')),
        'ort': _text(addr.get('city_source') or addr.get('city', '')),
    }


def comparison_rows(before, after, result, official=False, manually_edited=False):
    """Explain four field comparisons with uncertainty, not fabricated proof.

    green=verified identical official data; amber=check/change proposal;
    red=explicitly conflicting input; neutral=insufficient evidence.
    """
    data = []
    kind = _text(result)
    for key, label in FIELDS:
        old, new = _text(before.get(key, '')), _text(after.get(key, ''))
        tone, verdict = 'neutral', 'Nicht einzeln bestätigt'
        if manually_edited:
            if not _eq(old, new):
                tone, verdict = 'amber', 'Manuell geändert'
            else:
                tone, verdict = 'neutral', 'Keine Änderung'
        elif official:
            if not old or not new:
                tone, verdict = 'amber', 'Bitte ergänzen'
            elif _eq(old, new):
                tone, verdict = 'green', 'Amtlich übereinstimmend'
            else:
                tone, verdict = 'amber', 'Amtlicher Vorschlag'
        elif kind == 'amtlich_aktuell':
            if old:
                tone, verdict = 'green', 'Amtlich gefunden'
        elif kind == 'hausnummer_unbekannt':
            if key == 'strasse' and old:
                tone, verdict = 'green', 'Strasse im PLZ-Bereich'
            elif key == 'hausnummer':
                tone, verdict = 'amber', 'Nummer nicht gefunden'
        elif kind == 'plz_abweichend' and key == 'plz':
            tone, verdict = 'amber', 'PLZ bitte prüfen'
        elif kind == 'ort_abweichend' and key == 'ort':
            tone, verdict = 'amber', 'Ort bitte prüfen'
        elif kind == 'eingabe_konflikt' and key == 'hausnummer':
            tone, verdict = 'red', 'Widerspruch in Eingabe'
        if not _eq(old, new) and tone == 'neutral':
            tone, verdict = 'amber', 'Vorschlag prüfen'
        data.append(dict(key=key, label=label, before=old or '—',
                         after=new or '—', tone=tone, verdict=verdict))
    return data


def house_suggestions(hint, limit=16):
    """Extract only the literal house-number suggestions already in the hint."""
    if not hint:
        return []
    match = re.search(r'(?:mögliche|moegliche)\s+Hausnummern\s*:\s*([^\n.;]+)',
                      _text(hint), flags=re.I)
    if not match:
        return []
    suggestions = []
    for value in match.group(1).split(','):
        value = value.strip()
        if re.fullmatch(r'\d{1,5}\s?[A-Za-z]?(?:\s*[-/]\s*\d{1,5}\s?[A-Za-z]?)?', value):
            if value not in suggestions:
                suggestions.append(value)
        if len(suggestions) == limit:
            break
    return suggestions


def safe(value):
    """Use on every source-derived value going into custom Streamlit HTML."""
    return escape(_text(value), quote=True)
