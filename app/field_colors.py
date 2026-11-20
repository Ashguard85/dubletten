"""Evidence-labelled, accessible CRM field comparisons for review views.

Colors describe field-level evidence ONLY, never identity or automated approval.
Source strings are escaped before being inserted in the HTML display.
"""
from __future__ import annotations

from html import escape
from rapidfuzz import fuzz
from engine import (norm, norm_name, norm_street, norm_house_number,
                    norm_phone, norm_uid, split_street_house, plain)

CSS = '''<style>
.crm-evidence{border:1px solid #dbe5e9;border-radius:10px;overflow:hidden;margin:.25rem 0 .45rem}
.crm-evidence-head,.crm-evidence-row{display:grid;grid-template-columns:18% 29% 29% 24%;gap:0;align-items:center}
.crm-evidence-head{background:#e8eff3;color:#455d6d;font-size:.7rem;font-weight:700;padding:.33rem .55rem}
.crm-evidence-row{border-top:1px solid #e1e9ed;font-size:.83rem;min-height:39px;padding:.25rem .55rem}
.crm-evidence-label{font-weight:700;color:var(--text-color,#263d4c);padding-right:.3rem}
.crm-evidence-value{padding:.36rem .45rem;overflow-wrap:anywhere;white-space:pre-line;border-radius:5px;margin-right:.22rem}
.crm-evidence-pill{display:inline-block;max-width:100%;padding:.3rem .38rem;border-radius:5px;
    text-align:center;font-weight:700;font-size:.76rem;line-height:1.22;overflow-wrap:anywhere}
.crm-ev-green{background:#e3f4e9;color:#176a3b}
.crm-ev-amber{background:#fff1d6;color:#895607}
.crm-ev-red{background:#fae3e3;color:#9c252d}
.crm-ev-neutral{background:#eaf0f4;color:#476171}
.crm-evidence-caption{font-size:.72rem;color:#6d8292;padding:.3rem .55rem;background:#f6f9fa}
@media (max-width:750px){
 .crm-evidence-head,.crm-evidence-row{grid-template-columns:19% 27% 28% 26%;font-size:.7rem}
 .crm-evidence-pill{font-size:.68rem}.crm-evidence-value{padding:.25rem .15rem}
}
</style>'''

LABELS = {
    'green': '✓ identisch', 'amber': '≈ prüfen',
    'red': '≠ abweichend', 'neutral': '– keine Aussage',
}


def _norm_by(field, value):
    if field == 'Firma / Person':
        return norm_name(value)
    if field == 'Strasse':
        return norm_street(value)
    if field == 'Hausnummer':
        return norm_house_number(value)
    if field == 'E-Mail':
        return plain(value).lower()
    if field == 'Telefon':
        return norm_phone(value)
    if field == 'UID':
        return norm_uid(value)
    return norm(value)


def classify(field, left, right):
    """Return (color, label) with conservative, nonprobabilistic meanings.

    Empty fields do not prove a mismatch.  Only an exact nonempty string gets
    green; a normalization-only match is amber to remain visibly reviewable.
    """
    a,b = plain(left), plain(right)
    if not a and not b:
        return 'neutral', '– keine Werte'
    if not a or not b:
        return 'neutral', '– fehlt'
    if a == b:
        return 'green', LABELS['green']
    na, nb = _norm_by(field,a), _norm_by(field,b)
    if na and nb and na == nb:
        return 'amber', '≈ normalisiert'
    # Numeric identifiers, email and postal code cannot be fuzzy-verified.
    if field in ('Hausnummer','PLZ','UID','E-Mail','Telefon','Land'):
        return 'red', LABELS['red']
    if na and nb and fuzz.ratio(na,nb) >= 80:
        return 'amber', '≈ ähnlich'
    return 'red', LABELS['red']


def pair_rows(a, b, a_house='', b_house='', extra=False):
    """Build comparison rows from the original source fields, without mutation."""
    def name(raw):
        firm=plain(raw.get('company'))
        person=' '.join(x for x in (plain(raw.get('first_name')), plain(raw.get('last_name'))) if x)
        return firm or person

    def street_and_house(raw, given_house):
        street=plain(raw.get('street'))
        explicit=plain(raw.get('house_number'))
        # Split the original *display* value, retaining original casing.
        detected = norm_house_number(given_house or explicit)
        if not detected:
            _, detected, _ = split_street_house(street, explicit)
        source=street.replace('–','-').replace('—','-')
        import re
        last=re.fullmatch(r'(.+?)[,\s]+(\d{1,5}\s?[a-zA-Z]?(?:\s*[-/]\s*\d{1,5}\s?[a-zA-Z]?)?)',source)
        if last and norm_house_number(last.group(2))==detected:
            street=street[:len(last.group(1))]
        else:
            first=re.fullmatch(r'(\d{1,5}\s?[a-zA-Z]?)[,\s]+(.+)',source)
            if first and norm_house_number(first.group(1))==detected:
                street=street[len(first.group(1)):].lstrip(' ,')
        return street, explicit or detected

    sa,ha=street_and_house(a,a_house)
    sb,hb=street_and_house(b,b_house)
    pairs=[('Firma / Person',name(a),name(b)),('Strasse',sa,sb),
           ('Hausnummer',ha,hb),('PLZ',a.get('postcode',''),b.get('postcode','')),
           ('Ort',a.get('city',''),b.get('city',''))]
    if extra:
        pairs += [('E-Mail',a.get('email',''),b.get('email','')),
                  ('Telefon',a.get('phone',''),b.get('phone','')),
                  ('UID',a.get('uid',''),b.get('uid','')),
                  ('Vorname',a.get('first_name',''),b.get('first_name','')),
                  ('Nachname',a.get('last_name',''),b.get('last_name','')),
                  ('Land',a.get('country',''),b.get('country',''))]
    return [dict(field=f,a=plain(l),b=plain(r),tone=classify(f,l,r)[0],label=classify(f,l,r)[1])
            for f,l,r in pairs]


def reference_rows(differences):
    """Respect the deterministic statuses stored by the reference engine."""
    evidence={'identisch':('green','✓ identisch'),
              'normalisiert_gleich':('amber','≈ normalisiert'),
              'abweichend':('red','≠ abweichend'),
              'fehlt':('neutral','– fehlt')}
    result=[]
    for d in differences:
        tone,label = evidence.get(d.get('status'),('neutral','– unbekannt'))
        result.append(dict(field=plain(d.get('bezeichnung',d.get('feld',''))),
                           a=plain(d.get('a_wert','')),b=plain(d.get('b_wert','')),
                           tone=tone,label=label))
    return result


def html_table(rows, label_a='CRM A', label_b='CRM B', note='Farbige Feldbewertung; keine automatische Dublettenfreigabe.'):
    """Escape all imported values. Always convey the finding in text as well as color."""
    safe=lambda v:escape(str(v if v is not None else ''), quote=True)
    body=''.join(
        f'<div class="crm-evidence-row">'
        f'<span class="crm-evidence-label">{safe(r["field"])}</span>'
        f'<span class="crm-evidence-value">{safe(r["a"] or "—")}</span>'
        f'<span class="crm-evidence-value">{safe(r["b"] or "—")}</span>'
        f'<span><span class="crm-evidence-pill crm-ev-{r["tone"] if r["tone"] in LABELS else "neutral"}">'
        f'{safe(r["label"])}</span></span></div>'
        for r in rows
    )
    return (f'<div class="crm-evidence" role="table" aria-label="CRM-Feldvergleich">'
            f'<div class="crm-evidence-head"><span>Feld</span><span>{safe(label_a)}</span>'
            f'<span>{safe(label_b)}</span><span>Bewertung</span></div>{body}'
            f'<div class="crm-evidence-caption">{safe(note)}</div></div>')
