"""Command line interface: ``unity-docs versions`` / ``unity-docs scrape``."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import source
from .build import build, parse_sections


def _cmd_versions(_args) -> int:
    print("Probing Unity docs server…", file=sys.stderr)
    found = source.available_versions()
    if not found:
        print("No versions found (network problem?)", file=sys.stderr)
        return 1
    for v, size in found.items():
        print(f"{v:8}  offline zip {size / 1e6:6.0f} MB")
    return 0


def _cmd_scrape(args) -> int:
    version = source.resolve_version(args.version)
    sections = parse_sections(args.sections)
    formats = {f.strip().lower() for f in args.format.split(",") if f.strip()}
    if not formats <= {"md", "jsonl"}:
        raise SystemExit("--format must be md, jsonl or md,jsonl")
    zip_path = Path(args.zip) if args.zip else source.ensure_zip(version, refresh=args.refresh)
    if args.zip and not zip_path.exists():
        raise SystemExit(f"No such file: {zip_path}")

    result = build(
        version, zip_path, Path(args.out), sections, formats,
        only=args.only, workers=args.jobs,
    )
    print(
        f"\nDone: {result['docs']} files in {result['out']} "
        f"(~{result['chars'] / 1e6:.1f} MB of text, roughly {result['chars'] // 4 / 1e6:.1f}M tokens in total)",
        file=sys.stderr,
    )
    if result["collisions"]:
        print(f"warning: {result['collisions']} file names differ only by case "
              "(will collide on case-insensitive filesystems)", file=sys.stderr)
    if result["failures"]:
        print(f"warning: {len(result['failures'])} pages failed to convert, e.g.:", file=sys.stderr)
        for name, err in result["failures"][:5]:
            print(f"  {name}: {err}", file=sys.stderr)
    print(result["out"])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="unity-docs",
        description="Convert the Unity documentation for any editor version into LLM-friendly Markdown.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("versions", help="list editor versions that have downloadable docs").set_defaults(fn=_cmd_versions)

    sc = sub.add_parser("scrape", help="build the Markdown docs for one editor version")
    sc.add_argument("-v", "--version", required=True,
                    help="editor version: 2022.3, 6000.0, a full version like 6000.0.23f1, or 'latest'")
    sc.add_argument("-o", "--out", default="unity-docs", help="output directory (default: ./unity-docs)")
    sc.add_argument("-s", "--sections", default="manual,scriptref",
                    help="comma list of: manual, scriptref (default: both)")
    sc.add_argument("-f", "--format", default="md",
                    help="md (Markdown tree, default), jsonl (one record per page), or md,jsonl")
    sc.add_argument("--only", metavar="REGEX",
                    help="only pages whose 'Section/name' matches, e.g. '^ScriptReference/Rigidbody' or 'physics'")
    sc.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                    help="parallel workers (default: CPUs-1)")
    sc.add_argument("--zip", metavar="FILE", help="use an already-downloaded UnityDocumentation.zip")
    sc.add_argument("--refresh", action="store_true", help="re-download the cached zip")
    sc.set_defaults(fn=_cmd_scrape)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (source.SourceError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
