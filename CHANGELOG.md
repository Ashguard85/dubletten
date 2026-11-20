# 1.8.4 – Gemeinsame Fokusprüfung

- Neue modulare Streamlit-Fokusansicht für alle drei wesentlichen Prüfabläufe.
- Filter-Setup vor dem Bearbeiten; kompakte Einzelfall-Karte ohne grosse Ergebnislisten.
- Fragmentbasierte Navigation ohne vollständigen Seiten-Neuaufbau.
- Gleiche manuelle `set_review`-Entscheide für CRM-Dubletten wie bisher.
- Separat revisionsfähige Entscheidungen bei bestehender A→B-Referenz (neue Tabelle `focus_reference_decisions`).
- Schweizer Vorher-/Nachher-Bearbeitung nutzt das vorhandene `address_service`-Modul.
- Strenge swisstopo-Massenfreigabe bleibt optional verfügbar.
- CSV-Exporte nur auf Anforderung erzeugt, keine unnötige Datenlast je Klick.
- Integrationsscript erhält die originale `ui.py` sowie KI-Schalter und übrige Ansichten.
