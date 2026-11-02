# csdl2openapi.py

Python command-line tool that converts OData **CSDL XML or CSDL JSON**
(`$metadata`, OData V2, V3, V4 and 4.01) into **OpenAPI 3.0.x or 3.1.0** JSON.

It doesn't do the conversion itself: it drives the official OASIS converter
[oasis-tcs/odata-openapi](https://github.com/oasis-tcs/odata-openapi) (npm
package `odata-openapi`, pinned to **0.29.0**). That converter only exists in
JavaScript, so **Node.js and npm are required**. Python has no third-party
dependencies.

## Setup

- Python 3.8+
- Node.js 18+ with npm (<https://nodejs.org>)

On first run the script runs `npm ci` in this folder and installs the pinned
packages into `odata_openapi/node_modules/`. You can also do it yourself:

```bash
cd odata_openapi && npm ci --omit=dev
```

## Usage

```bash
python csdl2openapi.py Service.xml                    # -> Service.openapi3.json (OpenAPI 3.0.2)
python csdl2openapi.py Service.json -o 3.1.0 -p       # CSDL JSON in, pretty OpenAPI 3.1.0 out
python csdl2openapi.py Service.xml -t api/openapi.json
python csdl2openapi.py metadata/*.xml --out-dir openapi/   # several files at once
python csdl2openapi.py Service.xml --scheme https --host api.example.com --basePath /sap/opu/odata4/x
```

| Option | Meaning |
|---|---|
| `-o, --openapi-version` | `3.0.0`, `3.0.1`, `3.0.2` (default), `3.0.3` or `3.1.0` |
| `-t, --target FILE` | output file (single source only) |
| `--out-dir DIR` | output directory for `<basename>.openapi3.json` |
| `-p, --pretty` | indent the JSON (4 spaces) |
| `--scheme`, `--host`, `--basePath` | server URL parts; defaults `https`, `localhost`, `/service-root` |
| `--title`, `--description` | used if the CSDL has no `Core.Description` / `Core.LongDescription` |
| `--levels N` | maximum number of path segments (default 4) |
| `-k, --keep NAME` | keep only these root resources (repeatable) |
| `--skipBatchPath` | don't generate `/$batch` |
| `-d, --diagram` | add a yUML entity diagram to the description |

The pass-through options keep the upstream CLI's names, so the upstream
[annotation docs](https://github.com/oasis-tcs/odata-openapi/blob/main/doc/Annotations.md)
apply unchanged.

Without `-t`/`--out-dir`, the output goes next to the source:
`Foo.xml` becomes `Foo.openapi3.json`. Each written path is printed to stdout.
Converter warnings (e.g. unresolved cross-service references) go to stderr,
prefixed with the source file name.

**Exit codes:** `0` all files converted, `1` at least one file failed (the
other files are still written), `2` invalid arguments.

## How it works

```
csdl2openapi.py ──JSON on stdin──▶ node bridge.js ──▶ odata-csdl.xml2json (XML only)
                                                 └──▶ odata-openapi.csdl2openapi()
               ◀──JSON on stdout── {output, messages} | {error}  per file
```

All files in one run share a single Node process. The JSON serialization
happens in Node, so the output is **byte-identical to the upstream
`odata-openapi3` CLI**. This was checked for all 20 upstream example files in
both 3.0.2 and 3.1.0.

### Differences from the upstream CLI

- An invalid `--openapi-version` is rejected. Upstream accepts any string and
  writes it unchanged into the `openapi` field.
- Errors give a non-zero exit code. Upstream exits `0` even if the source file
  doesn't exist.
- A UTF-8 BOM or leading whitespace in the source is tolerated. Upstream then
  mistakes XML for JSON and fails.
- Several source files per call, plus `--out-dir`.
- `--levels` must be an integer. Upstream silently ignores invalid values.

### Known upstream limitations (not fixed here, on purpose)

- With `Core.OptionalParameter` without a `DefaultValue` (example
  `csdl-16.1.xml`), the converter writes `"default": "null"` (a string) for
  nullable non-string parameters. A strict OpenAPI validator rejects that.
- Cross-service references produce `$ref`s like
  `./Other.openapi3.json#/...`. Keep the default file names if you need those
  references to resolve.
- Output is JSON only. For YAML, convert it afterwards (e.g. with `yq`).

## Tests

```bash
python -m unittest test_csdl2openapi.py -v
```

The tests need Node/npm (otherwise they're skipped). The parity tests compare
the output byte by byte with the upstream CLI from `node_modules`. The
fixtures in `tests/fixtures/` were written for this tool (V4 XML, V2 XML, and
CSDL JSON derived from the V4 file).

## Updating the converter

Change the versions in `package.json`, run `npm install`, commit
`package-lock.json`, then run the tests.
