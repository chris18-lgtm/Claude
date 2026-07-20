#!/usr/bin/env python3
"""home_charging_tracker – Auswertung monatlicher BMW-Charging-Exporte (.xlsx).

Filtert Heimlade-Sessions, berechnet kWh/Kosten nach konfigurierbarem Tarif,
pflegt ein fortlaufendes CSV-Ledger (idempotent) und erzeugt Monatsreport,
Eigenbeleg (--beleg) und Jahresauswertung (--jahr).

Aufruf:
    python tracker.py <export.xlsx> [<weitere.xlsx> ...] [--beleg] [--dry-run]
    python tracker.py --jahr 2026
"""

import argparse
import calendar
import csv
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import openpyxl

# ============================== CONFIG =======================================

HOME_FILTER = "HOME"              # Charging location muss damit beginnen
TARIFE = [("2026-01-01", 0.30)]   # Liste (gueltig_ab, Arbeitspreis EUR/kWh),
                                  # aufsteigend; Session-Preis nach Plug-in-Datum
KORREKTURFAKTOR = 1.0             # Kalibrierung ggü. geeichtem Zwischenzähler
KWH_QUELLE = "cost"               # "cost": kWh = Cost / Tarif (2 Dezimalen),
                                  # Fallback "energy"-Spalte, falls Cost = "--"
MONATSZUORDNUNG = "plugin"        # Session zählt zum Monat des Plug-in-Datums
LEDGER = "charging_ledger.csv"    # fortlaufendes Register, append

# =============================================================================

TS_FMT = "%Y-%m-%d %H:%M"
CENT = Decimal("0.01")
LEDGER_FELDER = ["plugin_timestamp", "unplug_timestamp", "kwh", "tarif",
                 "kosten_eur", "lademinuten", "quelle_datei"]
MONATSNAMEN = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
               "August", "September", "Oktober", "November", "Dezember"]

MIN_KW = Decimal("1.5")   # Plausibilität: unterhalb warnt der Report
MAX_KW = Decimal("11")    # Plausibilität: oberhalb warnt der Report


@dataclass
class Session:
    plugin_dt: datetime
    unplug_dt: datetime | None
    km: Decimal
    soc_in: Decimal
    soc_out: Decimal
    location: str
    address: str
    cost_raw: object
    energy_raw: object
    charged_min: int
    quelle_datei: str
    kwh: Decimal = field(default=Decimal(0))
    tarif: Decimal = field(default=Decimal(0))
    kosten: Decimal = field(default=Decimal(0))
    kwh_quelle: str = ""


# ---------------------------- Bereinigung ------------------------------------

def clean_number(value) -> Decimal:
    """"~", "EUR", "€", "kWh" usw. strippen; "--" → 0; Tausendertrennzeichen raus."""
    if value is None:
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    s = str(value).strip()
    if s in ("", "--"):
        return Decimal(0)
    s = re.sub(r"(?i)(EUR|€|kWh|km|%|~)", "", s).strip()
    s = s.replace(",", "")  # US-Tausendertrennzeichen (z. B. "12,345 km")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal(0)


def parse_us_datetime(value) -> datetime | None:
    """US-Format M/D/YYYY H:MM; echte Datumszellen kommen als datetime durch."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if s in ("", "--"):
        return None
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_charged_time(value) -> int:
    """"11h 49min" → 709 Minuten; auch "49min", "11h"; "--" → 0."""
    if value is None:
        return 0
    s = str(value).strip()
    minuten = 0
    m = re.search(r"(\d+)\s*h", s)
    if m:
        minuten += int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*min", s)
    if m:
        minuten += int(m.group(1))
    return minuten


def de(value, dezimalen: int = 2) -> str:
    """Deutsches Zahlenformat: 1234.5 → "1.234,50" (kaufmännisch gerundet)."""
    q = Decimal(1) if dezimalen == 0 else Decimal(1).scaleb(-dezimalen)
    d = Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP)
    s = f"{d:,.{dezimalen}f}"
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


# ---------------------------- Geschäftslogik ---------------------------------

def tarif_fuer(plugin_dt: datetime) -> Decimal:
    """Letzter Tarif mit gueltig_ab <= Plug-in-Datum."""
    gewaehlt = None
    for gueltig_ab, preis in sorted(TARIFE):
        if date.fromisoformat(gueltig_ab) <= plugin_dt.date():
            gewaehlt = Decimal(str(preis))
    if gewaehlt is None:
        raise ValueError(
            f"Kein Tarif für {plugin_dt:%d.%m.%Y} konfiguriert "
            f"(frühester gueltig_ab: {sorted(TARIFE)[0][0]})")
    return gewaehlt


def berechne_session(s: Session) -> None:
    """kWh, Tarif und Kosten der Session bestimmen (inkl. Korrekturfaktor)."""
    s.tarif = tarif_fuer(s.plugin_dt)
    cost = clean_number(s.cost_raw)
    energy = clean_number(s.energy_raw)
    if KWH_QUELLE == "cost" and cost > 0:
        kwh = (cost / s.tarif).quantize(CENT, rounding=ROUND_HALF_UP)
        s.kwh_quelle = "cost"
    else:
        kwh = energy.quantize(CENT, rounding=ROUND_HALF_UP)
        s.kwh_quelle = "energy"
    s.kwh = (kwh * Decimal(str(KORREKTURFAKTOR))).quantize(
        CENT, rounding=ROUND_HALF_UP)
    s.kosten = (s.kwh * s.tarif).quantize(CENT, rounding=ROUND_HALF_UP)


def avg_kw(kwh: Decimal, minuten: int) -> Decimal | None:
    if minuten <= 0:
        return None
    return (kwh * 60 / Decimal(minuten)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP)


# ---------------------------- Parsing ----------------------------------------

def parse_export(pfad: str) -> list[Session]:
    """Alle Sessions eines BMW-Exports lesen (Kopfblock wird übersprungen)."""
    wb = openpyxl.load_workbook(pfad, read_only=True, data_only=True)
    ws = wb.active
    sessions: list[Session] = []
    spalten: dict[str, int] = {}
    datei = os.path.basename(pfad)

    for row in ws.iter_rows(values_only=True):
        zellen = ["" if c is None else str(c).strip() for c in row]
        if not spalten:
            if zellen and zellen[0].lower().startswith("plug-in date"):
                # Header gefunden: Spaltenname (ohne "*") → Index
                spalten = {z.rstrip("*").strip().lower(): i
                           for i, z in enumerate(zellen) if z}
            continue
        if not any(zellen):
            break  # Tabellenende

        def wert(name, default=None):
            i = spalten.get(name)
            return row[i] if i is not None and i < len(row) else default

        plugin_dt = parse_us_datetime(wert("plug-in date"))
        if plugin_dt is None:
            continue  # z. B. Summen- oder Hinweiszeile unter der Tabelle
        sessions.append(Session(
            plugin_dt=plugin_dt,
            unplug_dt=parse_us_datetime(wert("unplug date")),
            km=clean_number(wert("total mileage")),
            soc_in=clean_number(wert("state of charge when plugged in")),
            soc_out=clean_number(wert("state of charge when unplugged")),
            location=str(wert("charging location") or "").strip(),
            address=str(wert("location address") or "").strip(),
            cost_raw=wert("cost"),
            energy_raw=wert("energy charged"),
            charged_min=parse_charged_time(wert("charged time")),
            quelle_datei=datei,
        ))
    wb.close()
    if not spalten:
        raise ValueError(f"{datei}: keine Header-Zeile 'Plug-in date' gefunden "
                         "– ist das ein BMW-Charging-Export?")
    return sessions


# ---------------------------- Ledger -----------------------------------------

def lade_ledger(pfad: str) -> list[dict]:
    if not os.path.exists(pfad):
        return []
    with open(pfad, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f, delimiter=";"))


def schreibe_ledger(pfad: str, sessions: list[Session]) -> None:
    neu = not os.path.exists(pfad)
    with open(pfad, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FELDER, delimiter=";")
        if neu:
            w.writeheader()
        for s in sessions:
            w.writerow({
                "plugin_timestamp": s.plugin_dt.strftime(TS_FMT),
                "unplug_timestamp": s.unplug_dt.strftime(TS_FMT) if s.unplug_dt else "",
                "kwh": str(s.kwh),
                "tarif": str(s.tarif),
                "kosten_eur": str(s.kosten),
                "lademinuten": s.charged_min,
                "quelle_datei": s.quelle_datei,
            })


# ---------------------------- Reports ----------------------------------------

def monatslabel(jahr: int, monat: int) -> str:
    return f"{MONATSNAMEN[monat - 1]} {jahr}"


def drucke_monatsreport(jahr: int, monat: int, sessions: list[Session],
                        duplikate: int, dateien: list[str]) -> None:
    linie = "─" * 58
    print(f"\nHome-Charging Report – {monatslabel(jahr, monat)} "
          f"({', '.join(dateien)})")
    print(linie)
    print(f"{'Datum':<14}{'kWh':>9}{'Tarif':>12}{'Kosten':>12}{'Ø kW':>8}")
    warnungen = []
    for s in sorted(sessions, key=lambda x: x.plugin_dt):
        kw = avg_kw(s.kwh, s.charged_min)
        kw_txt = de(kw, 1) if kw is not None else "–"
        print(f"{s.plugin_dt:%d.%m. %H:%M}{de(s.kwh):>9}"
              f"{de(s.tarif) + ' €':>12}{de(s.kosten) + ' €':>12}{kw_txt:>8}")
        if kw is not None and kw < MIN_KW:
            warnungen.append(f"⚠ {s.plugin_dt:%d.%m.}: Ø Ladeleistung "
                             f"{de(kw, 1)} kW < {de(MIN_KW, 1)} kW – prüfen")
        elif kw is not None and kw > MAX_KW:
            warnungen.append(f"⚠ {s.plugin_dt:%d.%m.}: Ø Ladeleistung "
                             f"{de(kw, 1)} kW > {de(MAX_KW, 0)} kW – prüfen")
    print(linie)
    sum_kwh = sum((s.kwh for s in sessions), Decimal(0))
    sum_eur = sum((s.kosten for s in sessions), Decimal(0))
    plural = "Session" if len(sessions) == 1 else "Sessions"
    print(f"{len(sessions):>3} {plural}  |  {de(sum_kwh)} kWh  |  {de(sum_eur)} €")
    if duplikate:
        print(f"{duplikate:>3} Session(s) bereits im Ledger (übersprungen)")
    for w in warnungen:
        print(w)


def schreibe_beleg(jahr: int, monat: int, rows: list[dict]) -> str:
    """Eigenbeleg als Markdown aus (kombinierten) Ledger-Zeilen des Monats."""
    letzter = calendar.monthrange(jahr, monat)[1]
    sum_kwh = sum((Decimal(r["kwh"]) for r in rows), Decimal(0))
    sum_eur = sum((Decimal(r["kosten_eur"]) for r in rows), Decimal(0))
    tarife = sorted({Decimal(r["tarif"]) for r in rows})
    tarif_txt = " / ".join(f"{de(t)} €/kWh" for t in tarife)
    zaehlweise = ("kWh berechnet aus Kosten laut BMW Charging geteilt durch "
                  "Arbeitspreis" if KWH_QUELLE == "cost"
                  else "kWh laut Spalte „Energy charged“ des BMW-Exports")
    pfad = f"eigenbeleg_{jahr}-{monat:02d}.md"
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(f"""# Eigenbeleg – Heimladung Elektrofahrzeug

**Zeitraum:** {monatslabel(jahr, monat)} (01.{monat:02d}.{jahr} – {letzter}.{monat:02d}.{jahr})
**Ladeort:** Heimische Ladeeinrichtung (Charging location „{HOME_FILTER}…“)
**Zählweise:** {zaehlweise}

| Position | Wert |
|---|---|
| Ladevorgänge | {len(rows)} |
| Energiemenge | {de(sum_kwh)} kWh |
| Arbeitspreis | {tarif_txt} |
| **Erstattungsbetrag** | **{de(sum_eur)} €** |

> Hinweis: Werte laut BMW Charging (fahrzeugseitige Messung),
> Korrekturfaktor {de(KORREKTURFAKTOR, 2)}.

Datum: ______________________  Unterschrift: ______________________
""")
    return pfad


def drucke_jahresauswertung(jahr: int, rows: list[dict]) -> None:
    linie = "─" * 44
    print(f"\nJahresauswertung {jahr} (Quelle: {LEDGER})")
    print(linie)
    print(f"{'Monat':<12}{'Sessions':>9}{'kWh':>11}{'Kosten':>12}")
    monate: dict[int, list[dict]] = {}
    for r in rows:
        dt = datetime.strptime(r["plugin_timestamp"], TS_FMT)
        if dt.year == jahr:
            monate.setdefault(dt.month, []).append(r)
    sum_kwh = sum_eur = Decimal(0)
    anzahl = 0
    for monat in sorted(monate):
        mrows = monate[monat]
        kwh = sum((Decimal(r["kwh"]) for r in mrows), Decimal(0))
        eur = sum((Decimal(r["kosten_eur"]) for r in mrows), Decimal(0))
        print(f"{MONATSNAMEN[monat - 1]:<12}{len(mrows):>9}"
              f"{de(kwh):>11}{de(eur) + ' €':>12}")
        sum_kwh += kwh
        sum_eur += eur
        anzahl += len(mrows)
    print(linie)
    if anzahl == 0:
        print(f"Keine Sessions für {jahr} im Ledger.")
    else:
        print(f"{'Summe':<12}{anzahl:>9}{de(sum_kwh):>11}{de(sum_eur) + ' €':>12}")


# ---------------------------- Hauptablauf ------------------------------------

def verarbeite_dateien(pfade: list[str], beleg: bool, dry_run: bool) -> None:
    vorhandene_rows = lade_ledger(LEDGER)
    vorhandene_ts = {r["plugin_timestamp"] for r in vorhandene_rows}

    neue: list[Session] = []
    # (jahr, monat) → {"sessions": [...], "duplikate": n, "dateien": {...}}
    monate: dict[tuple[int, int], dict] = {}

    for pfad in pfade:
        for s in parse_export(pfad):
            if not s.location.startswith(HOME_FILTER):
                continue
            berechne_session(s)
            if s.kwh == 0:
                continue
            key = (s.plugin_dt.year, s.plugin_dt.month)
            eintrag = monate.setdefault(
                key, {"sessions": [], "duplikate": 0, "dateien": []})
            if s.quelle_datei not in eintrag["dateien"]:
                eintrag["dateien"].append(s.quelle_datei)
            ts = s.plugin_dt.strftime(TS_FMT)
            if ts in vorhandene_ts:
                eintrag["duplikate"] += 1
                continue
            vorhandene_ts.add(ts)
            eintrag["sessions"].append(s)
            neue.append(s)

    if not monate:
        print("Keine Home-Sessions mit Energie > 0 in den Eingabedateien gefunden.")
        return

    for (jahr, monat) in sorted(monate):
        e = monate[(jahr, monat)]
        drucke_monatsreport(jahr, monat, e["sessions"], e["duplikate"],
                            e["dateien"])

    if dry_run:
        print(f"\n[DRY-RUN] Ledger „{LEDGER}“ nicht verändert "
              f"({len(neue)} neue Session(s) wären geschrieben worden).")
    elif neue:
        schreibe_ledger(LEDGER, neue)
        print(f"\n{len(neue)} Session(s) ins Ledger „{LEDGER}“ übernommen.")
    else:
        print(f"\nKeine neuen Sessions – Ledger „{LEDGER}“ unverändert.")

    if beleg:
        # Beleg über den vollen Monat: bestehende Ledger-Zeilen + neue Sessions,
        # damit auch überlappende Teil-Importe einen vollständigen Beleg ergeben.
        alle_rows = vorhandene_rows + [{
            "plugin_timestamp": s.plugin_dt.strftime(TS_FMT),
            "kwh": str(s.kwh), "tarif": str(s.tarif),
            "kosten_eur": str(s.kosten),
        } for s in neue]
        for (jahr, monat) in sorted(monate):
            rows = [r for r in alle_rows
                    if datetime.strptime(r["plugin_timestamp"], TS_FMT).year == jahr
                    and datetime.strptime(r["plugin_timestamp"], TS_FMT).month == monat]
            pfad = schreibe_beleg(jahr, monat, rows)
            print(f"Eigenbeleg geschrieben: {pfad}")


def main(argv=None) -> int:
    # Windows-Konsolen laufen oft mit cp1252 statt UTF-8; ohne das hier
    # bricht jede Ausgabe von ─/⚠/€ mit UnicodeEncodeError ab. In Tests ist
    # sys.stdout ein io.StringIO ohne reconfigure() – daher der hasattr-Guard.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(
        prog="tracker.py",
        description="Home-Charging-Tracker für BMW-Charging-Exporte (.xlsx)")
    parser.add_argument("xlsx", nargs="*",
                        help="ein oder mehrere BMW-Charging-Exporte (.xlsx)")
    parser.add_argument("--beleg", action="store_true",
                        help="Eigenbeleg als Markdown pro Monat erzeugen")
    parser.add_argument("--jahr", type=int, metavar="JAHR",
                        help="Jahressumme aus dem Ledger ausgeben")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Report anzeigen, Ledger nicht verändern")
    args = parser.parse_args(argv)

    if not args.xlsx and args.jahr is None:
        parser.error("mindestens eine .xlsx-Datei oder --jahr angeben")

    if args.xlsx:
        verarbeite_dateien(args.xlsx, beleg=args.beleg, dry_run=args.dry_run)
    if args.jahr is not None:
        drucke_jahresauswertung(args.jahr, lade_ledger(LEDGER))
    return 0


if __name__ == "__main__":
    sys.exit(main())
