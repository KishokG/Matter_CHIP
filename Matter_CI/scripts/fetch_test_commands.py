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

# Reference apps are resolved dynamically from the SDK (see discover_targets.py)
# instead of a hardcoded apps: block in build_config.yaml.
sys.path.insert(0, str(SCRIPT_DIR))
from discover_targets import resolve_pipeline_apps

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

    # Split into lines
    lines = re.split(r'\n|\\n', raw)

    SENTENCE_STARTERS = re.compile(
        r'^(Note|While|Please|In the|Commission|The |This |For |If |When |After |Before |'
        r'During |Also |Additionally|Furthermore|However|Make sure|Ensure|Important)',
        re.IGNORECASE
    )

    cmd_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        # Stop at note lines or lines starting with "- Capital"
        if re.match(r'^(note\s*:|-\s+[A-Z])', stripped, re.IGNORECASE):
            break
        # Stop at bold markers
        if stripped.startswith('**') or stripped.startswith('__'):
            break
        # Stop at known sentence starters
        if SENTENCE_STARTERS.match(stripped):
            break
        # Stop if line is a pure sentence (Capital Word Capital Word, not a shell cmd)
        if (re.match(r'^[A-Z][a-z]+\s+[A-Z]', stripped) and
                not re.match(r'^(rm|--)', stripped)):
            break
        cmd_lines.append(stripped)

    cmd = ' '.join(cmd_lines).strip()

    # Extract from "rm -rf" onwards
    match = re.search(r'(rm\s+-rf\s+/tmp/chip[_/*].*)', cmd, re.IGNORECASE)
    if not match:
        return ""
    cmd = match.group(1).strip()

    # Strip path prefix: ./apps/chip-all-clusters-app → ./chip-all-clusters-app
    cmd = re.sub(r'\./(?:[\w\-]+/)+', './', cmd)

    # Remove trailing sentence fragments after last valid shell token
    cmd = re.sub(r'\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+).*$', '', cmd).strip()

    # Fix 3: Remove extra trailing quote if quotes are unbalanced (odd count)
    # This happens when the Google Sheet cell wraps the command in quotes
    # and the closing cell-quote gets captured as part of the command
    if cmd.count('"') % 2 != 0 and cmd.endswith('"'):
        cmd = cmd[:-1].strip()

    return cmd

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

    # Fix 4: Remove --PICS <path> placeholder — will be resolved at runtime
    # The actual PICS path comes from build_config.yaml pics_file setting
    cmd = re.sub(r'--PICS\s+\S+', '--PICS __PICS_PLACEHOLDER__', cmd).strip()

    return cmd


# =============================================================================
# Load TC list from JSON
# Returns: dict of {tc_id: cluster_name} for enabled TCs only
# =============================================================================
def load_tc_list(path: Path) -> dict[str, str]:
    """
    Loads tc_list.json — returns {tc_id: cluster_name} for enabled entries.
    If file not found, returns empty dict (run all rows from sheet).
    """
    if not path.exists():
        print(f"[WARN] tc_list.json not found at {path} — will fetch ALL rows from sheet.")
        return {}

    with open(path) as f:
        entries = json.load(f)

    tc_map = {}
    disabled = []
    for entry in entries:
        tc_id   = entry.get("tc_id", "").strip()
        cluster = entry.get("cluster", "Unknown").strip()
        enabled = entry.get("enabled", True)
        if not tc_id:
            continue
        if enabled:
            tc_map[tc_id] = cluster
        else:
            disabled.append(tc_id)

    print(f"[INFO] TC list loaded: {len(tc_map)} enabled, {len(disabled)} disabled")
    if disabled:
        print(f"[INFO] Disabled TCs: {disabled}")
    return tc_map


# =============================================================================
# Fetch from Google Sheets
# =============================================================================
def fetch_sheet(cfg: dict) -> list[list[str]]:
    gs = cfg["google_sheets"]
    # Key path comes from env var set by the workflow at runtime
    # (written from CREDENTIALS_JSON GitHub Secret)
    sa_key = os.environ.get(
        "GSHEET_SA_KEY_PATH",
        str(PROJECT_ROOT / "config" / "service_account.json")
    )

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
# Load build status — to skip TCs for apps that failed to build
# =============================================================================
def load_build_status(cfg: dict) -> set[str]:
    """Returns set of app names that FAILED to build."""
    log_dir = PROJECT_ROOT / cfg.get("test_execution", {}).get("log_dir", "logs/test_runs")
    build_status_file = log_dir.parent / "build_logs" / "build_status.json"

    if not build_status_file.exists():
        print("[INFO] No build_status.json found — assuming all apps built successfully.")
        return set()

    with open(build_status_file) as f:
        status = json.load(f)

    failed = {app for app, result in status.items() if result == "FAIL"}
    if failed:
        print(f"[WARN] Failed builds detected — TCs for these apps will be skipped: {failed}")
    return failed


# =============================================================================
# Extract app binary name from DUT command
# e.g. "rm -rf /tmp/chip_* && ./chip-all-clusters-app" → "chip-all-clusters-app"
# =============================================================================
def extract_binary_from_dut(dut_cmd: str) -> str:
    match = re.search(r'\./([^\s]+)', dut_cmd)
    return match.group(1) if match else ""


# =============================================================================
# Check if a binary name belongs to a failed app
# =============================================================================
def is_app_failed(binary_name: str, failed_apps: set[str], apps: list[dict]) -> str:
    """Returns app name if binary belongs to a failed app, else empty string.

    `apps` is the dynamically resolved reference-app list (from
    resolve_pipeline_apps) — same names the build used for its status logs.
    """
    for app in apps:
        if app.get("binary_name") == binary_name:
            # Match by name in failed_apps set
            if app["name"] in failed_apps:
                return app["name"]
    return ""


# =============================================================================
# Parse rows into test command records
# =============================================================================
def parse_rows(rows: list, cfg: dict, tc_map: dict[str, str]) -> list[dict]:
    """
    tc_map: {tc_id: cluster_name} for enabled TCs (from tc_list.json)
    """
    gs      = cfg["google_sheets"]
    cols    = gs["columns"]
    skip    = gs.get("header_rows", 6)
    col_tc  = cols["test_case_id"]
    col_dut = cols["dut_command"]
    col_py  = cols["python_command"]

    # Load failed build status to skip TCs for failed apps
    failed_apps = load_build_status(cfg)
    # Resolve the reference-app list once (only needed to map a failed binary
    # back to its app name) — skipped entirely if nothing failed to build.
    resolved_apps = []
    if failed_apps:
        sdk_dir = Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))
        resolved_apps = resolve_pipeline_apps(sdk_dir, cfg)

    def cell(row, idx):
        return row[idx].strip() if len(row) > idx else ""

    commands = []
    skipped_build = []
    errors   = []

    for i, row in enumerate(rows[skip:], start=skip + 1):
        tc_id = cell(row, col_tc)
        if not tc_id:
            continue

        # Filter by tc_map if provided (only run enabled TCs)
        if tc_map and tc_id not in tc_map:
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

        # Skip if the required app failed to build
        if failed_apps:
            binary = extract_binary_from_dut(dut_cmd)
            failed_app = is_app_failed(binary, failed_apps, resolved_apps)
            if failed_app:
                skipped_build.append(f"{tc_id} (app '{failed_app}' failed to build)")
                continue

        # Get cluster name from tc_map, fallback to extracting from TC ID
        cluster = tc_map.get(tc_id, "") if tc_map else ""
        if not cluster:
            # Auto-extract from TC ID e.g. TC-ACE-1.2 → Access Control Enforcement
            parts = tc_id.split("-")
            cluster = parts[1] if len(parts) > 1 else "Unknown"

        commands.append({
            "row":            i,
            "test_case_id":   tc_id,
            "cluster":        cluster,
            "dut_command":    dut_cmd,
            "python_command": py_cmd,
        })

    if errors:
        print(f"\n[WARN] {len(errors)} row(s) skipped due to parse errors:")
        for e in errors:
            print(f"  {e}")

    if skipped_build:
        print(f"\n[WARN] {len(skipped_build)} TC(s) skipped — app failed to build:")
        for s in skipped_build:
            print(f"  ⏭  {s}")

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
def apply_runtime_filters(tc_map: dict, cluster_filter: str, tc_filter: str) -> dict:
    """
    Apply runtime filters from workflow inputs (Issue 5).
    Priority: tc_filter > cluster_filter > tc_map (all enabled)
    """
    if not cluster_filter and not tc_filter:
        return tc_map   # no runtime filter — use tc_list.json as-is

    # TC filter — specific TC IDs override everything
    if tc_filter:
        tc_ids = [t.strip() for t in tc_filter.split(",") if t.strip()]
        filtered = {tc_id: tc_map.get(tc_id, "Unknown")
                    for tc_id in tc_ids
                    if tc_id in tc_map}
        not_found = [t for t in tc_ids if t not in tc_map]
        if not_found:
            print(f"[WARN] TC IDs not in tc_list.json (will skip): {not_found}")
        print(f"[INFO] TC filter applied: {len(filtered)} TCs from tc_filter input")
        return filtered

    # Cluster filter — filter by cluster name(s)
    if cluster_filter:
        clusters = [c.strip() for c in cluster_filter.split(",") if c.strip()]
        filtered = {tc_id: cluster
                    for tc_id, cluster in tc_map.items()
                    if any(c.lower() in cluster.lower() for c in clusters)}
        print(f"[INFO] Cluster filter '{cluster_filter}': {len(filtered)} TCs matched")
        if not filtered:
            print(f"[WARN] No TCs matched cluster filter. Available clusters:")
            for c in sorted(set(tc_map.values())):
                print(f"  - {c}")
        return filtered

    return tc_map


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    args = parser.parse_args()

    cfg      = load_config(Path(args.config))
    tc_file  = PROJECT_ROOT / cfg["test_execution"]["tc_list_file"]
    tc_map   = load_tc_list(tc_file)

    # Fix 5 — Apply runtime filters from GitHub Actions workflow inputs
    cluster_filter = os.environ.get("CLUSTER_FILTER", "").strip()
    tc_filter      = os.environ.get("TC_FILTER", "").strip()

    if cluster_filter:
        print(f"[INFO] Runtime cluster filter: {cluster_filter}")
    if tc_filter:
        print(f"[INFO] Runtime TC filter: {tc_filter}")

    tc_map = apply_runtime_filters(tc_map, cluster_filter, tc_filter)

    if tc_map:
        print(f"[INFO] Running {len(tc_map)} test cases")
        clusters = sorted(set(tc_map.values()))
        print(f"[INFO] Clusters: {clusters}")
    else:
        print("[WARN] No TCs to run after filtering!")

    rows     = fetch_sheet(cfg)
    commands = parse_rows(rows, cfg, tc_map)

    # Error: no commands found — exit clearly before saving empty file
    if not commands:
        print("[ERROR] No test commands were parsed from the sheet!")
        print("[ERROR] Possible causes:")
        print("  1. Cluster/TC filter matched nothing — check filter values")
        print("  2. All matched TCs have empty DUT or python commands in sheet")
        print("  3. spreadsheet_id or sheet_name is wrong in build_config.yaml")
        print("  4. header_rows value skips too many rows")
        sys.exit(1)

    save(commands, cfg)

    print("\n[INFO] Preview of parsed commands:")
    for c in commands[:3]:
        print(f"  {c['test_case_id']} [{c.get('cluster','')}]")
        print(f"    DUT : {c['dut_command'][:80]}")
        print(f"    PY  : {c['python_command'][:80]}")
    if len(commands) > 3:
        print(f"  ... and {len(commands) - 3} more")


if __name__ == "__main__":
    main()
