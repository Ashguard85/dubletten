# v1.8.7 – Interne Dublettenprüfung eines einzelnen CRM-Exports

- **Vierter Fokus-Arbeitsbereich:** Interne Dublettenprüfung mit einem CSV- oder XLSX-Export.
- **Import nur einmal:** Die Quell-IDs und alle Originalspalten werden unverändert in SQLite gespeichert, ohne eine zweite künstliche CRM-Kopie.
- **Echte dedupe-Paarbildung:** Jeder interne Kandidat besteht aus zwei unterschiedlichen Adressen; keine Selbstpaare oder doppelten umgekehrten Paare.
- **RapidFuzz** mit begrenztem Blocking oder **Splink 5 `dedupe_only`** mit einem echten Ein-Quellen-Modell. Keine stille Splink→RapidFuzz-Ersatzlogik.
- Farbiger Vergleich in Data Grid, Split-View, Review-Cockpit und Fokusmodus. Die beiden ursprünglichen Adressauszüge bleiben sichtbar.
- Manuelle Entscheidungen bestätigen/ablehnen/unklar; ein einzelner Datensatz kann innerhalb der internen Prüfung an mehreren bestätigten Paaren beteiligt sein. In den A↔B-Migrationsprojekten bleibt die bisherige eindeutige Zuordnung erhalten.
- **Gruppen ausschliesslich aus bestätigten Paarverbindungen.** Indirekte Gruppenmitgliedschaft ist keine fachliche Bestätigung jedes möglichen Paars; widersprüchliche Ablehnungen innerhalb einer Gruppe werden gekennzeichnet; es wird nichts automatisch zusammengeführt oder gelöscht.
- Drei CSV-Exportarten (alle geprüften Paare, nur bestätigte Paare, Gruppenliste) plus kompletter **unveränderter Originalexport mit angehängten Prüfspalten**. Spreadsheet-Formelinjektion wird beim Export neutralisiert.
- Beispiel `examples/CRM_Intern_Dubletten_Testdaten_v187.csv` mit 28 synthetischen Adressen, fünf geplanten Dublettengruppen und bewusst schwierigen Negativfällen.
- Der bestehende swisstopo-, A↔B-, Fokus- und KI-Aus-Schalter bleiben erhalten.

**Tests:** 33 lokale automatische Tests bestanden; Streamlit-/Docker-/Splink-5-Lauf und tatsächlicher 136-MB-swisstopo-Import auf dem Zielhost noch zu prüfen.

---

# v1.8.6 – Farbiger Feldvergleich in der CRM-Dublettenprüfung

- Grün bei exakt gleichen importierten Feldwerten, Orange bei normalisiert gleichen oder ähnlichen Schreibweisen, Rot bei abweichenden beiden bekannten Werten, Grau bei fehlenden Angaben.
- Fünf Kernfelder (Name/Firma, Strasse, Hausnummer, PLZ, Ort) als kompakter Vergleich im CRM-Fokusmodus.
- Auch Data Grid, Split-View und Review-Cockpit zeigen bei der ausgewählten Kandidatenpaarprüfung denselben Farbvergleich.
- A→B-Referenzprüfung erhält die Farben gemäss ihrem gespeicherten deterministischen Feldstatus; fehlende B-ID wird weiterhin ausdrücklich gemeldet.
- Keine automatische Freigabe durch Farben; Score bleibt separat; importierte CRM-Originalwerte bleiben unverändert.
- HTML-Escaping sämtlicher angezeigter CRM-Feldwerte.
- Alle Funktionen aus v1.8.5, beide Fokusmodi, Massenfreigabe, abgeschaltete KI und beide Testdaten-CSV bleiben enthalten.
- 25 automatisierte Tests erfolgreich; Docker- und Browsertest vor Produktiveinsatz empfohlen.

---

# v1.8.5 – Vollständige, eigenständige Neuinstallation

- Gesamtprojekt inklusive Quellcode, Dockerfile, Portainer-/Compose-Stacks und Beispieldaten.
- Zwei CRM-Workflows mit RapidFuzz sowie integrierter Splink-5-Anbindung/Modellablage.
- Swiss offline Gebäudeadressprüfung mit Streaming ZIP_LABEL-Import, eigener CSV/XLSX-Liste und manueller Korrektur.
- Alle drei Fokusqueues: CRM-Kandidaten, A→B-Referenzen, Schweizer Adressprüfung.
- Farbliche Anzeige von nachgewiesenen/unklaren Abweichungen und unveränderter Originaladressauszug.
- Massenfreigabe ausschliesslich eindeutiger aktueller amtlicher Vier-Felder-Treffer.
- Persistenter, standardmässig ausgeschalteter lokaler Ollama-KI-Schalter.
- Neuinstallation bewusst mit eigenem Port (8192) und separatem Volume, um laufende App-Daten zu schützen.
- Synthetische CRM-Testdaten: A 66 / B 59 Datensätze; kleine separate swisstopo-Schema-Testdatei.
- Gesammelte Modul- und Backend-Integrationstests; keine pauschale Behauptung eines getesteten Browser-/Docker-Deployments.
