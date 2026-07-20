# home_charging_tracker

Python-CLI zur Auswertung monatlicher **BMW-Charging-Exporte (.xlsx)**:
filtert Heimlade-Sessions, berechnet kWh und Kosten nach konfigurierbarem
Arbeitspreis, pflegt ein fortlaufendes CSV-Ledger (idempotent) und erzeugt
Monatsreport, Eigenbeleg und Jahresauswertung – z. B. zur Abrechnung von
Ladestrom gegenüber dem Arbeitgeber.

## Installation

```bash
pip install -r requirements.txt   # nur openpyxl
```

## Nutzung

```bash
python tracker.py export_2026-07.xlsx            # Monatsreport + Ledger-Update
python tracker.py exports/*.xlsx                 # Batch-Import mehrerer Exporte
python tracker.py export_2026-07.xlsx --beleg    # zusätzlich Eigenbeleg (Markdown)
python tracker.py export_2026-07.xlsx --dry-run  # Report ohne Ledger-Schreiben
python tracker.py --jahr 2026                    # Jahresauswertung aus dem Ledger
```

## Konfiguration

Alle Parameter stehen als CONFIG-Block am Anfang von `tracker.py`:

| Parameter | Default | Bedeutung |
|---|---|---|
| `HOME_FILTER` | `"HOME"` | Charging location muss damit beginnen |
| `TARIFE` | `[("2026-01-01", 0.30)]` | Liste `(gueltig_ab, EUR/kWh)`; der Tarif wird pro Session nach Plug-in-Datum gewählt, Preiserhöhungen greifen damit sauber |
| `KORREKTURFAKTOR` | `1.0` | Kalibrierung gegenüber geeichtem Zwischenzähler |
| `KWH_QUELLE` | `"cost"` | `kWh = Cost / Tarif` (2 Dezimalen); Fallback auf die Spalte „Energy charged“, falls `Cost = "--"` |
| `MONATSZUORDNUNG` | `"plugin"` | Session zählt zum Monat des Plug-in-Datums |
| `LEDGER` | `"charging_ledger.csv"` | fortlaufendes Register (append, Semikolon-getrennt) |

## Funktionsweise

- **Parsing:** Der BMW-Kopfblock (Titel, Monat, Filter, Fahrzeug, Summenzeile)
  wird übersprungen; die Header-Zeile `Plug-in date | … | Charged time` wird
  dynamisch gefunden, Spalten werden per Name zugeordnet.
- **Bereinigung:** US-Datumsformat `M/D/YYYY H:MM`; `~`, `EUR`, `kWh` werden
  gestrippt; `"--"` → 0; Tausendertrennzeichen in km entfernt;
  `"11h 49min"` → Minuten. Sessions mit 0 kWh werden übersprungen.
- **Dedupe:** Schlüssel ist der Plug-in-Timestamp. Überlappende Exporte und
  Mehrfach-Importe sind gefahrlos – das Ledger bleibt idempotent.
- **Rundung/Format:** Alle Beträge kaufmännisch auf Cent gerundet
  (`decimal`, `ROUND_HALF_UP`); Ausgabe in deutschen Formaten (`1.234,56 kWh`,
  `123,45 €`).
- **Plausibilität:** Ø Ladeleistung pro Session (kWh / Ladezeit); Warnung bei
  < 1,5 kW oder > 11 kW.
- **Eigenbeleg (`--beleg`):** Markdown pro Monat (`eigenbeleg_JJJJ-MM.md`) mit
  Zeitraum, Zählweise, Summen, Hinweis „Werte laut BMW Charging
  (fahrzeugseitige Messung), Korrekturfaktor X“ und Unterschriftszeile. Der
  Beleg wird aus Ledger + neuem Import gebildet und deckt damit auch
  Teil-Importe eines Monats vollständig ab.

## Tests

```bash
python -m unittest test_tracker.py -v
```

Die Tests erzeugen echte xlsx-Fixtures (inkl. Kopfblock, `"--"`-Fall,
Nicht-Home-Session) und decken Bereinigung, Tarifwechsel, Energy-Fallback,
Korrekturfaktor, Dedupe/Idempotenz, `--dry-run`, Beleg und Jahresauswertung ab.

## Ideen für später

- SoC-Plausibilitätscheck (geladene kWh vs. SoC-Differenz × Batteriekapazität)
- Jahres-Eigenbeleg (`--jahr` + `--beleg` kombiniert)
- Verbrauchsstatistik kWh/100 km aus Kilometerstands-Deltas
- Warnung bei Monatslücken zwischen Ledger und neuem Export
- Ledger-Backup vor jedem Schreiben
- CSV-Export des Monatsreports (`--csv`)
