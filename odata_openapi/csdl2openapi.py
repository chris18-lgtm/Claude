#!/usr/bin/env python3
"""Convert OData CSDL XML or CSDL JSON ($metadata) to OpenAPI 3.0.x or 3.1.0.

Thin Python front end for the official OASIS converter
https://github.com/oasis-tcs/odata-openapi (npm package `odata-openapi`),
which is JavaScript-only. Requires Node.js and npm; the pinned npm packages
are installed next to this script on first run.

Exit codes: 0 = all files converted, 1 = at least one file failed, 2 = usage error.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE / "bridge.js"
OPENAPI_VERSIONS = ("3.0.0", "3.0.1", "3.0.2", "3.0.3", "3.1.0")


class SetupError(Exception):
    pass


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="csdl2openapi.py",
        description="Convert OData CSDL XML/JSON (V2, V3, V4, 4.01) to OpenAPI JSON "
        "using the OASIS odata-openapi converter.",
    )
    p.add_argument("sources", nargs="+", type=Path, metavar="SOURCE",
                   help="CSDL XML or CSDL JSON file(s)")
    p.add_argument("-o", "--openapi-version", choices=OPENAPI_VERSIONS, default="3.0.2",
                   help="OpenAPI version of the output (default: %(default)s)")
    where = p.add_mutually_exclusive_group()
    where.add_argument("-t", "--target", type=Path,
                       help="output file (only with a single SOURCE; default: "
                       "SOURCE basename + .openapi3.json next to the source)")
    where.add_argument("--out-dir", type=Path,
                       help="write <basename>.openapi3.json files into this directory")
    p.add_argument("-p", "--pretty", action="store_true", help="pretty-print the JSON result")
    # Options passed through to odata-openapi (same names as the upstream CLI).
    p.add_argument("--scheme", help="scheme of the server URL (library default: https)")
    p.add_argument("--host", help="host of the server URL (default: localhost)")
    p.add_argument("--basePath", help="base path of the server URL (default: /service-root)")
    p.add_argument("--title", help="default title if none is annotated")
    p.add_argument("--description", help="default description if none is annotated")
    p.add_argument("--levels", type=int, help="maximum number of path segments (default: 4)")
    p.add_argument("-k", "--keep", action="append", metavar="NAME",
                   help="root resource to keep (repeat for several)")
    p.add_argument("--skipBatchPath", action="store_true", help="do not generate the /$batch path")
    p.add_argument("-d", "--diagram", action="store_true", help="include yUML diagram")
    args = p.parse_args(argv)

    if args.target and len(args.sources) > 1:
        p.error("--target can only be used with a single SOURCE (use --out-dir instead)")
    args.targets = [target_for(src, args) for src in args.sources]
    if len(set(t.resolve() for t in args.targets)) != len(args.targets):
        p.error("several sources would be written to the same output file")
    return args


def target_for(source, args):
    if args.target:
        return args.target
    name = source.stem + ".openapi3.json"
    return (args.out_dir or source.parent) / name


def library_options(args):
    """Map CLI arguments to csdl2openapi() options; unset ones keep library defaults."""
    opts = {"openapiVersion": args.openapi_version}
    for arg, key in (("scheme", "scheme"), ("host", "host"), ("basePath", "basePath"),
                     ("title", "defaultTitle"), ("description", "defaultDescription"),
                     ("levels", "maxLevels"), ("keep", "rootResourcesToKeep")):
        if getattr(args, arg) is not None:
            opts[key] = getattr(args, arg)
    if args.skipBatchPath:
        opts["skipBatchPath"] = True
    if args.diagram:
        opts["diagram"] = True
    return opts


def ensure_node():
    node, npm = shutil.which("node"), shutil.which("npm")
    if not node:
        raise SetupError("Node.js not found on PATH – install it from https://nodejs.org")
    if not (HERE / "node_modules" / "odata-openapi").is_dir():
        if not npm:
            raise SetupError("npm not found on PATH – it ships with Node.js")
        print("First run: installing odata-openapi via npm ...", file=sys.stderr)
        r = subprocess.run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"],
                           cwd=HERE, stdout=sys.stderr)
        if r.returncode != 0:
            raise SetupError(f"'npm ci' failed in {HERE}")
    return node


def read_csdl(path):
    """Return CSDL text without BOM/leading whitespace (upstream trips over both)."""
    return path.read_bytes().decode("utf-8-sig").lstrip()


def run_bridge(node, sources, options, indent):
    request = json.dumps({"options": options, "indent": indent, "sources": sources})
    r = subprocess.run([node, str(BRIDGE)], input=request.encode("utf-8"),
                       capture_output=True, cwd=HERE)
    if r.returncode != 0:
        raise SetupError("Node bridge failed:\n" + r.stderr.decode("utf-8", "replace"))
    return json.loads(r.stdout.decode("utf-8"))["results"]


def main(argv=None):
    if hasattr(sys.stderr, "reconfigure"):  # Windows consoles (cp1252)
        sys.stderr.reconfigure(errors="backslashreplace")
        sys.stdout.reconfigure(errors="backslashreplace")
    args = parse_args(argv)

    failed = 0
    jobs = []  # (source, target, csdl text)
    for source, target in zip(args.sources, args.targets):
        try:
            jobs.append((source, target, read_csdl(source)))
        except (OSError, UnicodeDecodeError) as e:
            print(f"{source}: error: {e}", file=sys.stderr)
            failed += 1

    if jobs:
        try:
            node = ensure_node()
            results = run_bridge(node, [text for _, _, text in jobs],
                                 library_options(args), 4 if args.pretty else 0)
        except SetupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        for (source, target, _), result in zip(jobs, results):
            if "error" in result:
                print(f"{source}: error: {result['error']}", file=sys.stderr)
                failed += 1
                continue
            for message in result["messages"]:
                print(f"{source}: warning: {message}", file=sys.stderr)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(result["output"].encode("utf-8"))
            print(target)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
