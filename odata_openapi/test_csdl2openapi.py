"""Tests for csdl2openapi.py – run: python -m unittest test_csdl2openapi.py -v

Needs Node.js + npm. The parity tests compare our output byte-for-byte with
the upstream `odata-openapi3` CLI from the pinned npm package.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "csdl2openapi.py"
FIXTURES = HERE / "tests" / "fixtures"
UPSTREAM_CLI = HERE / "node_modules" / "odata-openapi" / "lib" / "cli.js"


@unittest.skipUnless(shutil.which("node") and shutil.which("npm"), "Node.js/npm not installed")
class Csdl2OpenApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            capture_output=True, text=True, encoding="utf-8",
        )

    def run_upstream(self, source, target, *args):
        subprocess.run(
            ["node", str(UPSTREAM_CLI), *args, "-t", str(target), str(source)],
            check=True, capture_output=True,
        )
        return target.read_bytes()

    def assert_parity(self, fixture, *args):
        ours = self.tmp / "ours.json"
        result = self.run_cli(FIXTURES / fixture, "-t", ours, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = self.run_upstream(FIXTURES / fixture, self.tmp / "upstream.json", *args)
        self.assertEqual(ours.read_bytes(), expected)
        return json.loads(expected)

    # --- parity with upstream ---------------------------------------------

    def test_parity_v4_xml_defaults(self):
        doc = self.assert_parity("Library.xml")
        self.assertEqual(doc["openapi"], "3.0.2")
        self.assertIn("/Books", doc["paths"])

    def test_parity_v4_xml_31_with_all_options(self):
        doc = self.assert_parity(
            "Library.xml", "-o", "3.1.0", "-p", "-d",
            "--scheme", "http", "--host", "example.com", "--basePath", "/odata/v4",
            "--title", "My Title", "--description", "My Description",
            "--levels", "2", "--skipBatchPath", "-k", "Books",
        )
        self.assertEqual(doc["openapi"], "3.1.0")
        self.assertEqual(doc["servers"][0]["url"], "http://example.com/odata/v4")
        self.assertNotIn("/$batch", doc["paths"])

    def test_parity_v2_xml(self):
        doc = self.assert_parity("Shop.v2.xml", "-o", "3.0.3")
        self.assertIn("/Products", doc["paths"])

    def test_parity_csdl_json(self):
        self.assert_parity("Library.json", "-o", "3.1.0")

    # --- our own behaviour ------------------------------------------------

    def test_json_and_xml_input_give_same_result(self):
        self.assertEqual(self.run_cli(FIXTURES / "Library.xml", "-t", self.tmp / "x.json").returncode, 0)
        self.assertEqual(self.run_cli(FIXTURES / "Library.json", "-t", self.tmp / "j.json").returncode, 0)
        self.assertEqual((self.tmp / "x.json").read_bytes(), (self.tmp / "j.json").read_bytes())

    def test_default_target_next_to_source(self):
        src = self.tmp / "Svc.metadata.xml"
        shutil.copy(FIXTURES / "Library.xml", src)
        result = self.run_cli(src)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.tmp / "Svc.metadata.openapi3.json").exists())

    def test_bom_and_leading_whitespace_are_tolerated(self):
        src = self.tmp / "bom.xml"
        src.write_bytes(b"\xef\xbb\xbf\n  " + (FIXTURES / "Library.xml").read_bytes())
        result = self.run_cli(src, "-t", self.tmp / "bom.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/Books", json.loads((self.tmp / "bom.json").read_text("utf-8"))["paths"])

    def test_non_ascii_survives(self):
        self.run_cli(FIXTURES / "Library.xml", "-t", self.tmp / "u.json")
        self.assertIn("Titel – mit Umlaut äöü", (self.tmp / "u.json").read_text("utf-8"))

    def test_invalid_version_rejected(self):
        result = self.run_cli(FIXTURES / "Library.xml", "-o", "3.2.0", "-t", self.tmp / "v.json")
        self.assertEqual(result.returncode, 2)
        self.assertIn("3.1.0", result.stderr)
        self.assertFalse((self.tmp / "v.json").exists())

    def test_batch_with_bad_file_converts_the_rest_and_exits_1(self):
        bad = self.tmp / "bad.xml"
        bad.write_text("<edmx:Edmx><oops", "utf-8")
        good = self.tmp / "good.xml"
        shutil.copy(FIXTURES / "Library.xml", good)
        result = self.run_cli(bad, good)
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad.xml", result.stderr)
        self.assertTrue((self.tmp / "good.openapi3.json").exists())
        self.assertFalse((self.tmp / "bad.openapi3.json").exists())

    def test_invalid_json_reported(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", "utf-8")
        result = self.run_cli(bad)
        self.assertEqual(result.returncode, 1)
        self.assertIn("bad.json", result.stderr)

    def test_missing_file_exits_1(self):
        result = self.run_cli(self.tmp / "nope.xml")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nope.xml", result.stderr)

    def test_target_with_multiple_sources_rejected(self):
        result = self.run_cli(FIXTURES / "Library.xml", FIXTURES / "Shop.v2.xml", "-t", self.tmp / "t.json")
        self.assertEqual(result.returncode, 2)

    def test_out_dir(self):
        out = self.tmp / "out"
        result = self.run_cli(FIXTURES / "Library.xml", FIXTURES / "Shop.v2.xml", "--out-dir", out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            sorted(p.name for p in out.iterdir()),
            ["Library.openapi3.json", "Shop.v2.openapi3.json"],
        )


if __name__ == "__main__":
    unittest.main()
