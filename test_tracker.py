"""Unit-Tests für tracker.py – Aufruf: python -m unittest test_tracker.py -v"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal

import openpyxl

import tracker

HEADER = ["Plug-in date", "Total mileage", "State of charge when plugged in",
          "Unplug date", "State of charge when unplugged", "Charging location",
          "Location address", "Tag", "Cost*", "Energy charged*",
          "Energy discharged", "Energy rate 1", "Energy rate 2", "Charged time"]

# Zeile 1: normale Home-Session, kWh aus Cost (3.00 / 0.30 = 10.00 kWh, 120 min → 5 kW)
ZEILE_NORMAL = ["7/1/2026 21:14", "12,345 km", "40 %", "7/2/2026 6:03", "80 %",
                "HOME 1", "Musterstr. 1, 12345 Stadt", "--", "~ EUR 3.00",
                "~ 10.5 kWh", "--", "--", "--", "2h 0min"]
# Zeile 2: Cost = "--" → Energy-Fallback (8.5 kWh), 11h 49min → 0.7 kW (Warnung)
ZEILE_FALLBACK = ["7/3/2026 22:00", "12,410 km", "30 %", "7/4/2026 9:49", "65 %",
                  "HOME 1", "Musterstr. 1, 12345 Stadt", "--", "--",
                  "~ 8.5 kWh", "--", "--", "--", "11h 49min"]
# Zeile 3: öffentliche Session → wird über HOME_FILTER ausgefiltert
ZEILE_PUBLIC = ["7/5/2026 12:00", "12,500 km", "20 %", "7/5/2026 12:40", "60 %",
                "IONITY Rasthof", "A8, 86899 Landsberg", "--", "~ EUR 25.00",
                "~ 30.0 kWh", "--", "--", "--", "0h 40min"]


def baue_export(pfad, zeilen):
    """xlsx mit BMW-typischem Kopfblock + Tabellenzeilen erzeugen."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["BMW Charging – Charging history"])
    ws.append(["July 2026"])
    ws.append(["Filter: All locations"])
    ws.append(["Vehicle: BMW i4 (WBY...)"])
    ws.append(["Total", "", "", "", "", "", "", "", "~ EUR 28.00", "~ 49.0 kWh"])
    ws.append([])
    ws.append(HEADER)
    for z in zeilen:
        ws.append(z)
    wb.save(pfad)


class TrackerTestBasis(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.ledger = os.path.join(self.tmp, "ledger.csv")
        self._config = {k: getattr(tracker, k) for k in
                        ("HOME_FILTER", "TARIFE", "KORREKTURFAKTOR",
                         "KWH_QUELLE", "LEDGER")}
        self.addCleanup(lambda: [setattr(tracker, k, v)
                                 for k, v in self._config.items()])
        tracker.LEDGER = self.ledger
        tracker.TARIFE = [("2026-01-01", 0.30)]
        tracker.KORREKTURFAKTOR = 1.0
        tracker.KWH_QUELLE = "cost"

    def export(self, zeilen, name="export.xlsx"):
        pfad = os.path.join(self.tmp, name)
        baue_export(pfad, zeilen)
        return pfad

    def run_main(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tracker.main(argv)
        return out.getvalue()

    def ledger_zeilen(self):
        return tracker.lade_ledger(self.ledger)


class TestBereinigung(unittest.TestCase):
    def test_clean_number(self):
        self.assertEqual(tracker.clean_number("~12.34 kWh"), Decimal("12.34"))
        self.assertEqual(tracker.clean_number("1,234 km"), Decimal("1234"))
        self.assertEqual(tracker.clean_number("--"), Decimal("0"))
        self.assertEqual(tracker.clean_number("EUR 4.56"), Decimal("4.56"))
        self.assertEqual(tracker.clean_number("~ EUR 3.00"), Decimal("3.00"))
        self.assertEqual(tracker.clean_number(None), Decimal("0"))
        self.assertEqual(tracker.clean_number(7.5), Decimal("7.5"))
        self.assertEqual(tracker.clean_number("40 %"), Decimal("40"))

    def test_parse_charged_time(self):
        self.assertEqual(tracker.parse_charged_time("11h 49min"), 709)
        self.assertEqual(tracker.parse_charged_time("49min"), 49)
        self.assertEqual(tracker.parse_charged_time("11h"), 660)
        self.assertEqual(tracker.parse_charged_time("--"), 0)
        self.assertEqual(tracker.parse_charged_time(None), 0)

    def test_parse_us_datetime(self):
        self.assertEqual(tracker.parse_us_datetime("7/1/2026 21:14"),
                         datetime(2026, 7, 1, 21, 14))
        dt = datetime(2026, 7, 1, 21, 14)
        self.assertIs(tracker.parse_us_datetime(dt), dt)
        self.assertIsNone(tracker.parse_us_datetime("--"))

    def test_deutsches_format(self):
        self.assertEqual(tracker.de(1234.5), "1.234,50")
        self.assertEqual(tracker.de(Decimal("0.3")), "0,30")
        self.assertEqual(tracker.de(Decimal("7.4"), 1), "7,4")

    def test_kaufmaennische_rundung(self):
        self.assertEqual(tracker.de(Decimal("0.125")), "0,13")
        self.assertEqual(tracker.de(Decimal("2.675")), "2,68")


class TestTarif(unittest.TestCase):
    def setUp(self):
        self._tarife = tracker.TARIFE
        self.addCleanup(setattr, tracker, "TARIFE", self._tarife)
        tracker.TARIFE = [("2026-01-01", 0.30), ("2026-07-01", 0.35)]

    def test_tarifwechsel(self):
        self.assertEqual(tracker.tarif_fuer(datetime(2026, 6, 30, 23, 59)),
                         Decimal("0.30"))
        self.assertEqual(tracker.tarif_fuer(datetime(2026, 7, 1, 0, 0)),
                         Decimal("0.35"))

    def test_kein_tarif(self):
        with self.assertRaises(ValueError):
            tracker.tarif_fuer(datetime(2025, 12, 31))


class TestImport(TrackerTestBasis):
    def test_parse_und_berechnung(self):
        pfad = self.export([ZEILE_NORMAL, ZEILE_FALLBACK, ZEILE_PUBLIC])
        sessions = tracker.parse_export(pfad)
        self.assertEqual(len(sessions), 3)  # Filter greift erst später
        s = sessions[0]
        tracker.berechne_session(s)
        self.assertEqual(s.kwh, Decimal("10.00"))       # 3.00 / 0.30 aus Cost
        self.assertEqual(s.kosten, Decimal("3.00"))
        self.assertEqual(s.kwh_quelle, "cost")
        self.assertEqual(s.km, Decimal("12345"))
        self.assertEqual(s.charged_min, 120)

    def test_energy_fallback_bei_cost_strich(self):
        pfad = self.export([ZEILE_FALLBACK])
        s = tracker.parse_export(pfad)[0]
        tracker.berechne_session(s)
        self.assertEqual(s.kwh, Decimal("8.50"))
        self.assertEqual(s.kwh_quelle, "energy")
        self.assertEqual(s.kosten, Decimal("2.55"))     # 8.50 × 0.30

    def test_korrekturfaktor(self):
        tracker.KORREKTURFAKTOR = 0.95
        pfad = self.export([ZEILE_NORMAL])
        s = tracker.parse_export(pfad)[0]
        tracker.berechne_session(s)
        self.assertEqual(s.kwh, Decimal("9.50"))        # 10.00 × 0.95

    def test_home_filter_und_report(self):
        pfad = self.export([ZEILE_NORMAL, ZEILE_FALLBACK, ZEILE_PUBLIC])
        out = self.run_main([pfad])
        self.assertIn("Juli 2026", out)
        self.assertIn("2 Sessions", out)                # public gefiltert
        self.assertIn("18,50 kWh", out)                 # 10.00 + 8.50
        self.assertIn("5,55 €", out)                    # 3.00 + 2.55
        self.assertNotIn("IONITY", out)
        self.assertIn("0,7 kW", out)                    # Warnung < 1,5 kW
        self.assertIn("⚠", out)

    def test_null_kwh_wird_uebersprungen(self):
        zeile = list(ZEILE_NORMAL)
        zeile[8] = "--"   # Cost
        zeile[9] = "--"   # Energy charged
        pfad = self.export([zeile])
        out = self.run_main([pfad])
        self.assertIn("Keine Home-Sessions", out)
        self.assertEqual(self.ledger_zeilen(), [])


class TestLedger(TrackerTestBasis):
    def test_dedupe_idempotent(self):
        pfad = self.export([ZEILE_NORMAL, ZEILE_FALLBACK])
        self.run_main([pfad])
        self.assertEqual(len(self.ledger_zeilen()), 2)
        out = self.run_main([pfad])                     # zweiter Import
        self.assertEqual(len(self.ledger_zeilen()), 2)  # unverändert
        self.assertIn("2 Session(s) bereits im Ledger", out)

    def test_ueberlappende_exporte(self):
        pfad1 = self.export([ZEILE_NORMAL], "juli_teil1.xlsx")
        pfad2 = self.export([ZEILE_NORMAL, ZEILE_FALLBACK], "juli_teil2.xlsx")
        self.run_main([pfad1])
        self.run_main([pfad2])
        rows = self.ledger_zeilen()
        self.assertEqual(len(rows), 2)
        ts = [r["plugin_timestamp"] for r in rows]
        self.assertEqual(len(ts), len(set(ts)))

    def test_batch_import(self):
        pfad1 = self.export([ZEILE_NORMAL], "a.xlsx")
        pfad2 = self.export([ZEILE_FALLBACK], "b.xlsx")
        self.run_main([pfad1, pfad2])
        self.assertEqual(len(self.ledger_zeilen()), 2)

    def test_dry_run(self):
        pfad = self.export([ZEILE_NORMAL])
        out = self.run_main([pfad, "--dry-run"])
        self.assertIn("[DRY-RUN]", out)
        self.assertFalse(os.path.exists(self.ledger))


class TestBelegUndJahr(TrackerTestBasis):
    def test_beleg(self):
        pfad = self.export([ZEILE_NORMAL, ZEILE_FALLBACK])
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        self.run_main([pfad, "--beleg"])
        beleg = os.path.join(self.tmp, "eigenbeleg_2026-07.md")
        self.assertTrue(os.path.exists(beleg))
        with open(beleg, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Juli 2026", text)
        self.assertIn("18,50 kWh", text)
        self.assertIn("**5,55 €**", text)
        self.assertIn("Korrekturfaktor 1,00", text)
        self.assertIn("fahrzeugseitige Messung", text)

    def test_jahresauswertung(self):
        august = list(ZEILE_NORMAL)
        august[0] = "8/2/2026 20:00"
        pfad = self.export([ZEILE_NORMAL, ZEILE_FALLBACK, august])
        self.run_main([pfad])
        out = self.run_main(["--jahr", "2026"])
        self.assertIn("Jahresauswertung 2026", out)
        self.assertIn("Juli", out)
        self.assertIn("August", out)
        self.assertIn("28,50", out)                     # 18,50 + 10,00 kWh
        self.assertIn("8,55 €", out)                    # 5,55 + 3,00

    def test_jahr_ohne_daten(self):
        out = self.run_main(["--jahr", "2031"])
        self.assertIn("Keine Sessions für 2031", out)


if __name__ == "__main__":
    unittest.main()
