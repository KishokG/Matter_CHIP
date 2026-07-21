#!/usr/bin/env python3
"""
regenerate_report.py

Rebuild report.html from an EXISTING run's results — no re-run needed. Every run
saves its full results to test_results.json (same data the live report is built
from), so this re-applies the current report design to any past run (e.g. a
downloaded matter-ci-results-* / test-results-* folder).

Usage:
    # point at a run folder (expects <folder>/test_results.json + <folder>/test_runs/)
    python3 Matter_CI/scripts/regenerate_report.py /path/to/test-results-185

    # or point directly at a test_results.json
    python3 Matter_CI/scripts/regenerate_report.py /path/to/test_results.json

    # custom output location
    python3 Matter_CI/scripts/regenerate_report.py <folder> --out /tmp/report.html

The report links to per-test logs as test_runs/<name>, so report.html is written
INTO the run folder (alongside its test_runs/ dir) by default, keeping links live.
"""
import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import run_tests  # noqa: E402  (adds generate_report; guarded by __main__)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Run folder with test_results.json, OR a test_results.json path")
    ap.add_argument("--out", default=None,
                    help="Output report path (default: <folder>/report.html)")
    args = ap.parse_args()

    p = Path(args.path).expanduser()
    if p.is_dir():
        folder = p
        results_json = p / "test_results.json"
    else:
        results_json = p
        folder = p.parent
    if not results_json.exists():
        sys.exit(f"[ERROR] test_results.json not found: {results_json}")

    results = json.loads(results_json.read_text())
    if not results:
        sys.exit(f"[ERROR] {results_json} has no results.")

    # Header metadata (commit/branch/date) — from the run folder's build-info.json.
    bi_file = folder / "build-info.json"
    build_info = {}
    if bi_file.exists():
        try:
            build_info = json.loads(bi_file.read_text())
        except Exception:
            pass

    out = Path(args.out).expanduser() if args.out else folder / "report.html"
    run_tests.generate_report(results, report_path=out, build_info=build_info)

    if not (folder / "test_runs").is_dir() and out.parent == folder:
        print(f"[WARN] No 'test_runs/' dir next to the report — the Ctrl/DUT Log "
              f"links will 404. Put report.html beside the run's test_runs/ folder.")
    print(f"[OK] Regenerated: {out}  ({len(results)} test cases)")


if __name__ == "__main__":
    main()
