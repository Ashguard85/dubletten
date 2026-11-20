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
