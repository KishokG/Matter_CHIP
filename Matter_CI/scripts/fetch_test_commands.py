#!/usr/bin/env python3
"""
fetch_test_commands.py
======================
Fetches test commands from Google Sheet, parses DUT and python commands,
filters by tc_list.txt, and saves to logs/test_commands.json.

Usage:
    python3 scripts/fetch_test_commands.py [--config config/build_config.yaml]
"""

import re
import os
import sys
import json
import yaml
import argparse
from pathlib import Path

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gapi_build
except ImportError:
    print("[ERROR] Missing Google API libs. Run:")
    print("  pip3 install google-auth google-auth-httplib2 google-api-python-client --break-system-packages")
    sys.exit(1)

SCRIPT_DIR  = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


# =============================================================================
# Config helpers
# =============================================================================
def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

def cfg_str(cfg, *keys, default=""):
    val = cfg
    for k in keys:
        val = val.get(k, {}) if isinstance(val, dict) else {}
    return val if isinstance(val, str) else default

def cfg_int(cfg, *keys, default=0):
    val = cfg
    for k in keys:
        val = val.get(k, {}) if isinstance(val, dict) else {}
    return val if isinstance(val, int) else default


# =============================================================================
# DUT command parser
# Extracts: rm -rf /tmp/chip_* && ./chip-xxx-app [args]
# Strips:   notes, path prefix (./apps/), header text
# =============================================================================
def parse_dut_command(raw: str) -> str:
    if not raw:
        return ""

    # Remove everything before and including "DUT terminal:" if present
    raw = re.sub(r'.*?DUT terminal\s*[:\-]\s*', '', raw, flags=re.IGNORECASE | re.DOTALL)

    # Remove note lines (lines starting with Note: or -)
    lines = raw.splitlines()
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if re.match(r'^(note|-).*', stripped, re.IGNORECASE):
            break   # stop at first note
        if stripped:
            clean_lines.append(stripped)

    cmd = ' '.join(clean_lines).strip()

    # Extract from "rm -rf" onwards
    match = re.search(r'(rm\s+-rf\s+/tmp/chip_\*.*)', cmd, re.IGNORECASE)
    if not match:
        return ""

    cmd = match.group(1).strip()

    # Strip path prefix from binary — keep only ./binary-name or binary-name
    # e.g. ./apps/chip-all-clusters-app → ./chip-all-clusters-app
    # e.g. ./apps/matter-network-manager-app → ./matter-network-manager-app
    cmd = re.sub(r'\./[\w/\-]+/([\w\-]+(?:-app|matter[\w\-]*))', r'./\1', cmd)

    return cmd


# =============================================================================
# Python command parser
# Extracts: python3 TC_xxx.py --args...
# Strips:   Note: lines and everything after
# =============================================================================
def parse_python_command(raw: str) -> str:
    if not raw:
        return ""

    # Split on newline or literal \n
    lines = re.split(r'\n|\\n', raw)

    result_lines = []
    for line in lines:
        stripped = line.strip()
        # Stop at Note: line
        if re.match(r'^note\s*:', stripped, re.IGNORECASE):
            break
        if stripped:
            result_lines.append(stripped)

    cmd = ' '.join(result_lines).strip()

    # Extract from "python3" onwards
    match = re.search(r'(python3\s+\S+\.py\s+.*)', cmd, re.IGNORECASE)
    if not match:
        return ""

    cmd = match.group(1).strip()

    # Remove Note: suffix if on same line
    cmd = re.sub(r'\s*Note:.*$', '', cmd, flags=re.IGNORECASE).strip()

    return cmd


# =============================================================================
# Load TC list
# =============================================================================
def load_tc_list(path: Path) -> list[str]:
    if not path.exists():
        print(f"[WARN] tc_list.txt not found at {path} — will fetch ALL rows from sheet.")
        return []
    tcs = []
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                tcs.append(stripped)
    return tcs


# =============================================================================
# Fetch from Google Sheets
# =============================================================================
def fetch_sheet(cfg: dict) -> list[list[str]]:
    gs = cfg["google_sheets"]
    sa_key = os.environ.get("GSHEET_SA_KEY_PATH",
                            str(PROJECT_ROOT / gs["service_account_key"]))

    if not Path(sa_key).exists():
        print(f"[ERROR] Service account key not found: {sa_key}")
        print("  Set GSHEET_SA_KEY_PATH env var or update config.")
        sys.exit(1)

    creds   = service_account.Credentials.from_service_account_file(sa_key, scopes=SCOPES)
    service = gapi_build("sheets", "v4", credentials=creds)

    sheet_id = gs["spreadsheet_id"]
    tab      = gs["sheet_name"]
    print(f"[INFO] Fetching: spreadsheet={sheet_id}  tab='{tab}'")

    result = (service.spreadsheets().values()
              .get(spreadsheetId=sheet_id, range=tab)
              .execute())

    rows = result.get("values", [])
    print(f"[INFO] Fetched {len(rows)} rows from sheet.")
    return rows


# =============================================================================
# Parse rows into test command records
# =============================================================================
def parse_rows(rows: list, cfg: dict, tc_list: list[str]) -> list[dict]:
    gs      = cfg["google_sheets"]
    cols    = gs["columns"]
    skip    = gs.get("header_rows", 6)
    col_tc  = cols["test_case_id"]
    col_dut = cols["dut_command"]
    col_py  = cols["python_command"]

    def cell(row, idx):
        return row[idx].strip() if len(row) > idx else ""

    commands = []
    skipped  = []
    errors   = []

    for i, row in enumerate(rows[skip:], start=skip + 1):
        tc_id = cell(row, col_tc)
        if not tc_id:
            continue

        # Filter by tc_list if provided
        if tc_list and tc_id not in tc_list:
            continue

        raw_dut = cell(row, col_dut)
        raw_py  = cell(row, col_py)

        dut_cmd = parse_dut_command(raw_dut)
        py_cmd  = parse_python_command(raw_py)

        if not dut_cmd:
            errors.append(f"Row {i}: {tc_id} — could not parse DUT command")
            continue
        if not py_cmd:
            errors.append(f"Row {i}: {tc_id} — could not parse Python command")
            continue

        commands.append({
            "row":            i,
            "test_case_id":   tc_id,
            "dut_command":    dut_cmd,
            "python_command": py_cmd,
        })

    if errors:
        print(f"\n[WARN] {len(errors)} row(s) skipped due to parse errors:")
        for e in errors:
            print(f"  {e}")

    print(f"\n[INFO] Parsed {len(commands)} test command(s) ready to execute.")
    return commands


# =============================================================================
# Save output
# =============================================================================
def save(commands: list, cfg: dict):
    out_dir = PROJECT_ROOT / cfg["test_execution"]["log_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir.parent / "test_commands.json"
    with open(out_path, "w") as f:
        json.dump(commands, f, indent=2)
    print(f"[INFO] Saved to: {out_path}")
    return out_path


# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    args = parser.parse_args()

    cfg      = load_config(Path(args.config))
    tc_file  = PROJECT_ROOT / cfg["test_execution"]["tc_list_file"]
    tc_list  = load_tc_list(tc_file)

    if tc_list:
        print(f"[INFO] TC filter: {len(tc_list)} test cases from {tc_file}")
    else:
        print("[INFO] No TC filter — running all rows")

    rows     = fetch_sheet(cfg)
    commands = parse_rows(rows, cfg, tc_list)
    save(commands, cfg)

    print("\n[INFO] Preview of parsed commands:")
    for c in commands[:3]:
        print(f"  {c['test_case_id']}")
        print(f"    DUT : {c['dut_command'][:80]}")
        print(f"    PY  : {c['python_command'][:80]}")
    if len(commands) > 3:
        print(f"  ... and {len(commands) - 3} more")


if __name__ == "__main__":
    main()
