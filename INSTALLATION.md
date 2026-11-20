# CRM-Dubletten Lokal 1.8.4 – Fokusprüfung

## Ziel

Die vorhandenen, umfangreichen Ansichten **Data Grid**, **Split-View**, **Review-Cockpit**, **Referenzprüfung** und **Schweizer Adressprüfung** bleiben erhalten. Zusätzlich gibt es **einen gemeinsamen, schlanken Fokusmodus** für die tatsächliche Einzelfallprüfung:

1. Prüfart und Auftrag wählen.
2. Filter setzen und mit **„Prüfmodus starten“** bestätigen.
3. **Nur einen Datensatz** mit den entscheidenden Vergleichswerten sehen.
4. Direkt **Bestätigen / Ablehnen / Unklar / Überspringen** oder fachlich geprüft markieren.
5. Automatisch zum nächsten Datensatz, ohne die gesamte Streamlit-App neu aufzubauen.

Auf normalen Desktop-Bildschirmen ist die Hauptansicht bewusst kompakt; bei kleinen Displays, hoher Zoomstufe oder besonders langen Daten können dennoch Scrollbalken auftreten.

## Technische Ausgangslage

Die zuletzt gelieferte Version war **1.8.3**. Das öffentlich erreichbare GitHub-Repository `Ashguard85/dubletten` zeigt aktuell einen **älteren Code-Stand**. Deshalb liegt diesem Paket **keine überschreibende `app/ui.py`** bei. Der Integrationsschritt passt *deine tatsächlich vorhandene* UI gezielt an, ohne andere Funktionen oder den KI-Schalter durch ältere Dateien zu ersetzen.

### Schritt 1: Backup

Vor der Aktualisierung dein persistentes Datenverzeichnis sichern:

`/home/buebi/docker/crm-dubletten/data`

Die Datenbank für ein konsistentes Dateibackup möglichst bei gestopptem App-Container sichern oder über SQLite-Backup sichern.

### Schritt 2: Git-Projekt aktualisieren

In deinem **lokalen Checkout des aktuellen GitHub-Projekts**:

1. Dieses ZIP **ins Wurzelverzeichnis des Repositories** entpacken. Dabei werden `app/focus_ui.py`, `app/focus_data.py` und `scripts/apply_focus_patch.py` ergänzt.
2. Folgenden Befehl im Repository ausführen:

```bash
python3 scripts/apply_focus_patch.py --file app/ui.py
```

3. `git diff` überprüfen, insbesondere die drei kleinen Integrationsstellen in `app/ui.py`.
4. Committen und zu GitHub `main` pushen:

```bash
git add app/focus_ui.py app/focus_data.py app/ui.py scripts/apply_focus_patch.py

git commit -m "Add unified compact focus review mode"

git push origin main
```

**Wichtig:** Nicht das öffentlich sichtbare ältere `ui.py` als Ersatz für dein aktuelles `ui.py` verwenden! Der Patch ist idempotent und verändert keine Dateien, wenn erwartete Integrationspunkte fehlen.

#### Ohne Python-Patchscript: drei manuelle Änderungen

1. Bei den Python-Imports der bestehenden `app/ui.py` ergänzen: `from focus_ui import render_focus_workspace`.
2. Nach der Zeile, in der `choices = ['+ Neuer Abgleich', ...]` gebildet wird, ergänzen: `choices.append('🎯 Fokusmodus')` (innerhalb der Sidebar-Einrückung).
3. **Vor** dem bestehenden `if show_swisstopo:`-Handler ergänzen:

```python
if selected == '🎯 Fokusmodus':
    render_focus_workspace(db)
    st.stop()
```

Bei der manuellen Methode bleibt die bisherige grosse Seitenüberschrift sichtbar. Das Patchscript blendet sie für den Fokusmodus zusätzlich aus.

### Schritt 3: Portainer

Portainer → **Stacks → crm-dubletten → Editor → Update the stack**. Docker-Image aus Git erneut bauen und den Container neu erstellen lassen. **Die bisherige YAML, Ports und Volumes bleiben unverändert.**

Danach Browser hart aktualisieren und links bei **Projekt / Datenstand** den neuen Eintrag **🎯 Fokusmodus** auswählen.

### Schritt 4: Bedienung

- **CRM – fehlende Referenzen:** Alle bestehenden Dublettenkandidaten aus dem Projekt, gefiltert nach Status, Mindestscore, Suchbegriff und Risiko. Aktionen verwenden dieselbe vorhandene `pairs`-Tabelle wie die drei alten Ansichten. „Überspringen“ bewertet nichts und springt nur weiter.
- **CRM – vorhandene Referenzen:** Der direkte A→B-Referenzvergleich wird auf einen Fall reduziert. Du kannst „Geprüft“, „Korrektur nötig“, „Unklar“, „Überspringen“ oder „Zurück“ vergeben. Diese **neuen fachlichen Prüfentscheide** werden revisionsfähig in der zusätzlichen SQLite-Tabelle `focus_reference_decisions` gespeichert. Sie ändern **keine** A- oder B-Adresse. Ein eigener CSV-Export steht bereit.
- **Schweizer Adressprüfung:** Vorher/Nachher mit getrennten Feldern für Strasse und Hausnummer, Bestätigen / Unverändert / Überspringen / Zurücksetzen. Greift auf die vorhandenen Adressprüfaufträge zu. Die **strenge swisstopo-Massenfreigabe** und ein CSV-Export sind weiterhin erreichbar.

**Filter werden vor Arbeitsbeginn gesetzt.** Während des Abarbeitens ist die Filtermaske eingeklappt. „Zurück“ und „Weiter“ navigieren durch die aktuelle gefilterte Warteschlange. Nach einer Entscheidung wird automatisch weitergeschaltet. Am Ende wird nicht stillschweigend wieder beim ersten Datensatz begonnen.

## Datenschutz und Grenzen

- Keine zusätzlichen externen Adress- oder KI-Anfragen durch den Fokusmodus. Das Modul verwendet lokal gespeicherte Datensätze und bereits berechnete Treffer.
- Die bestehenden Ollama-/Splink- sowie swisstopo-Funktionen und deren Einstellungen werden nicht ersetzt.
- Keine automatische Anlage, Zusammenführung oder Änderung von CRM-Datensätzen.
- Der Fokusmodus ist **keine neue Matching-Engine**: Er präsentiert und bewertet bereits berechnete Kandidaten.
- Er ist mit Streamlit **1.63** und der dokumentierten SQLite-Struktur konzipiert. Ein vollständiger Browser-/Portainer-Test auf deinem Server wurde hier nicht ausgeführt.
- Die **Schweizer Adressprüfung** erscheint nur, wenn die entsprechenden Module und Prüfaufträge aus v1.8.x tatsächlich in deinem Repository und in der Datenbank vorhanden sind. Ein öffentlich noch auf v1.7.x stehendes Repository wird dadurch nicht automatisch zu v1.8.3 aktualisiert.

## Tests

Lokal gegen den beigefügten Fokusmodul-Code:

```bash
python3 -m pytest tests/test_focus.py -q
```

Getestet: Datenbankfilter/Revision, SQL-Injection-Schutz, CSV-Formelschutz, Navigation nach Statusänderung, idempotenter Integrationspatch und SQL-Paginierung. Die echten, älteren/nachgelagerten Projekttests müssen nach dem Update mit der vollständigen Projekt-Codebasis erneut ausgeführt werden.
