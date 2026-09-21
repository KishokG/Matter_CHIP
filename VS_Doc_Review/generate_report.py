"""
Usage:
    python generate_report.py
    python generate_report.py --spreadsheet-id 1AbC...
    python generate_report.py --tab1 "My Tab" --tab2 "Master Tab"
    python generate_report.py --output somewhere/custom.html   # skip the history folder

Everything has a sensible default pulled from config.py; the flags below
just let you override one thing without editing the file.

Every run (unless --output is given) is written as its own timestamped file
under config.REPORTS_DIR, e.g. reports/validation_report_2026-09-21_13-26-12.html,
so past runs are kept rather than overwritten. reports/latest.html is also
refreshed each time as a stable link to the newest run.
"""

import argparse
import datetime as dt
import json
import webbrowser
from pathlib import Path

import config
import validators


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the test-case tracking sheet.")
    parser.add_argument("--spreadsheet-id", default=config.SPREADSHEET_ID)
    parser.add_argument("--service-account-file", default=config.SERVICE_ACCOUNT_FILE)
    parser.add_argument("--tab1", default=config.TAB1_NAME, help="Tab with per-test-case commands")
    parser.add_argument("--tab2", default=config.TAB2_NAME, help="Master/cert-repo tab")
    parser.add_argument("--reports-dir", default=config.REPORTS_DIR, help="Folder each timestamped run is written into")
    parser.add_argument("--output", default=None, help="Write to this exact path instead of the timestamped history folder")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open the report in a browser")
    return parser.parse_args()


def load_tab1_rows(args) -> list:
    from sheets_client import fetch_tab_values  # imported lazily so the offline
    all_values = fetch_tab_values(args.spreadsheet_id, args.tab1, args.service_account_file)
    data_rows = all_values[config.TAB1_DATA_START_ROW - 1:]
    return [row for row in data_rows if any(c.strip() for c in row)]


def load_tab2_rows(args) -> list:
    from sheets_client import fetch_tab_values  # demo script never has to install gspread
    all_values = fetch_tab_values(args.spreadsheet_id, args.tab2, args.service_account_file)
    data_rows = all_values[config.TAB2_DATA_START_ROW - 1:]
    return [row for row in data_rows if any(c.strip() for c in row)]


def build_report_data(args, tab1_raw_rows: list, tab2_raw_rows: list) -> dict:
    validated_rows = [validators.validate_tab1_row(row) for row in tab1_raw_rows]
    cross = validators.cross_check(validated_rows, tab2_raw_rows)

    total_issues = sum(len(r["issues"]) for r in validated_rows)
    rows_with_error = sum(1 for r in validated_rows if any(i["severity"] == "error" for i in r["issues"]))
    rows_with_warning = sum(1 for r in validated_rows if any(i["severity"] == "warning" for i in r["issues"]))
    rows_with_issues = sum(1 for r in validated_rows if r["issues"])

    cli_missing = sum(1 for r in validated_rows if not r["cells"]["G"])
    python_missing = sum(1 for r in validated_rows if not r["cells"]["F"])
    both_missing = sum(1 for r in validated_rows if not r["cells"]["F"] and not r["cells"]["G"])

    summary = {
        "total_rows": len(validated_rows),
        "rows_with_issues": rows_with_issues,
        "clean_rows": len(validated_rows) - rows_with_issues,
        "total_issues": total_issues,
        # Test-case counts (not raw issue counts) - a row with 3 errors
        # still counts once here.
        "errors": rows_with_error,
        "warnings": rows_with_warning,
        "cli_missing": cli_missing,
        "python_missing": python_missing,
        "both_missing": both_missing,
        "extra_in_tab1": len(cross["extra_in_tab1"]),
        "missing_python_tests": len(cross["missing_python_tests"]),
        "cluster_mismatches": len(cross["cluster_mismatches"]),
    }

    return {
        "meta": {
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tab1_name": args.tab1,
            "tab2_name": args.tab2,
        },
        "summary": summary,
        "rows": validated_rows,
        "cross_check": cross,
    }


def render_html(report_data: dict, output_path: Path) -> None:
    template_path = Path(__file__).parent / config.REPORT_TEMPLATE_FILE
    template_html = template_path.read_text(encoding="utf-8")
    payload = json.dumps(report_data, ensure_ascii=False).replace("</", "<\\/")
    final_html = template_html.replace("__REPORT_DATA_JSON__", payload)
    output_path.write_text(final_html, encoding="utf-8")


def build_output_path(args) -> Path:
    """Explicit --output wins; otherwise a timestamped file under reports_dir."""
    if args.output:
        return Path(args.output).resolve()
    reports_dir = Path(args.reports_dir).resolve()
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime(config.REPORT_TIMESTAMP_FORMAT)
    filename = f"{config.REPORT_FILENAME_PREFIX}_{timestamp}.html"
    return reports_dir / filename


def main() -> None:
    args = parse_args()

    print(f"Reading '{args.tab1}' ...")
    tab1_raw_rows = load_tab1_rows(args)
    print(f"Reading '{args.tab2}' ...")
    tab2_raw_rows = load_tab2_rows(args)

    print(f"Validating {len(tab1_raw_rows)} rows ...")
    report_data = build_report_data(args, tab1_raw_rows, tab2_raw_rows)

    output_path = build_output_path(args)
    render_html(report_data, output_path)
    print(f"Report written to {output_path}")

    if config.KEEP_LATEST_COPY and not args.output:
        latest_path = Path(args.reports_dir).resolve() / "latest.html"
        render_html(report_data, latest_path)
        print(f"Also updated {latest_path}")

    if not args.no_open:
        webbrowser.open(output_path.as_uri())


if __name__ == "__main__":
    main()
