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
def _cut_multi_command(cmd: str) -> str:
    """
    A Sheet cell sometimes packs TWO command variants + English prose into one
    field, e.g. "…admin_storage.json When test is executed on sample app … use the
    below command python3 …". Fetched as-is, the extra words become bogus argparse
    tokens → the script exits 2 before running. Keep only the FIRST command by
    truncating at a second interpreter invocation OR at the prose that introduces
    the next variant. (The pipeline auto-injects sample-app extras — app-pipe,
    simulate_*, --enable-key, --PICS — so the clean real-DUT command is enough.)
    """
    cuts = []
    for marker in (r'\bpython3\b', r'\brm\s+-rf\s+/tmp/chip'):
        idxs = [m.start() for m in re.finditer(marker, cmd, re.IGNORECASE)]
        if len(idxs) > 1:
            cuts.append(idxs[1])                     # start of the 2nd command
    for pat in (r'\bwhen\s+(?:the\s+)?(?:test|executed|run|it\b)',
                r'\buse\s+the\s+below\b', r'\bnote\s*:',
                r'\bfor\s+(?:pre-?condition|step)\b',   # SDK green notes
                r'\bafter\s+advert', r'\bin\s+step\b',
                r'\bon\s+(?:real\s+dut|sample\s+app)\b'):
        m = re.search(pat, cmd, re.IGNORECASE)
        if m:
            cuts.append(m.start())
    return cmd[:min(cuts)].strip() if cuts else cmd


def parse_dut_command(raw: str) -> str:
    """Extract the DUT launch command from a Sheet cell that may contain prose,
    Notes, or multiple 'Terminal N:' blocks.

    Returns "" when there is NO server app to launch — either an explicit
    "Not Required to launch the server app", or a cell that only commissions via
    chip-tool / launches multiple apps (e.g. Fabric-Sync fabric-admin+bridge).
    An empty result is NOT an error: the test is self-orchestrating (it launches
    its own apps via --string-arg app paths), or the runner builds the DUT from
    the SDK CI header (Fabric-Sync). The caller no longer treats "" as a failure.
    """
    if not raw:
        return ""

    raw = re.sub(r'.*?DUT terminal\s*[:\-]\s*', '', raw, flags=re.IGNORECASE | re.DOTALL)
    text = raw.replace('\\n', '\n')

    # Explicit "no server app" → self-orchestrating (nothing to launch).
    if re.search(r'not\s+required\s+to\s+launch', text, re.IGNORECASE):
        return ""

    # Multi-terminal Fabric-Sync setups (fabric-admin + fabric-bridge-app across
    # two terminals) can't be expressed as one './app' launch — the runner builds
    # the DUT from the SDK CI header (fabric-sync-app.py) instead. Signal no-DUT.
    if re.search(r'\bfabric-bridge-app\b', text, re.IGNORECASE) and \
       re.search(r'\bfabric-admin\b', text, re.IGNORECASE):
        return ""

    # Find the FIRST launchable command: optional `rm -rf …chip… &&` then `./app`.
    # Scan line by line, stripping a leading 'Terminal N:' / label so the command
    # after it is still seen. Skip pure prose/Note lines.
    launch_re = re.compile(r'((?:rm\s+-rf\s+\S*chip\S*\s*&&\s*)?\./\S+.*)', re.IGNORECASE)
    cmd = ""
    for line in re.split(r'\n', text):
        s = re.sub(r'^\s*terminal\s*\d*\s*[:\-]?\s*', '', line.strip(), flags=re.IGNORECASE)
        if not s or re.match(r'^(note\b|command\b|once\b|-\s|\*\*)', s, re.IGNORECASE):
            continue
        m = launch_re.search(s)
        if m:
            c = m.group(1).strip()
            # chip-tool is a COMMISSIONER action (e.g. `chip-tool pairing …`), not
            # a DUT server to launch. Such tests self-launch their real DUT via a
            # --string-arg app_path (e.g. TC-DA-1.9) → treat as no-DUT.
            if re.search(r'\./(?:[\w\-]+/)*chip-tool\b', c, re.IGNORECASE):
                continue
            cmd = c
            break
    if not cmd:
        return ""   # no launchable ./app → self-orchestrating (not an error)

    cmd = _cut_multi_command(cmd)
    cmd = re.sub(r'\./(?:[\w\-]+/)+', './', cmd)   # ./apps/chip-x → ./chip-x
    # Trim a trailing prose fragment (two+ Capitalised words) after the command.
    cmd = re.sub(r'\s+[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+).*$', '', cmd).strip()
    if cmd.count('"') % 2 != 0 and cmd.endswith('"'):
        cmd = cmd[:-1].strip()
    return cmd

def parse_python_command(raw: str) -> str:
    if not raw:
        return ""

    text = raw.replace('\\n', '\n')
    lines = text.split('\n')

    # Find the line where the `python3 …TC_*.py` command starts — IGNORING any
    # leading prose/Note lines (some cells put a "Note: …" line BEFORE the command,
    # e.g. TC-BRBINFO-4.1). Then collect that line + continuation arg-lines, and
    # STOP at the first prose/Note line that follows (trailing notes).
    start = next((i for i, l in enumerate(lines)
                  if re.search(r'python3\s+\S+\.py\b', l, re.IGNORECASE)), None)
    if start is None:
        return ""
    PROSE = re.compile(r'^(note\b|for\b|after\b|in\s+step\b|when\b|'
                       r'use\s+the\b|on\s+(?:real|sample)\b)', re.IGNORECASE)
    collected = []
    for l in lines[start:]:
        s = l.strip()
        if collected and PROSE.match(s):
            break
        if s:
            collected.append(s)
    cmd = ' '.join(collected).strip()

    match = re.search(r'(python3\s+\S+\.py\b.*)', cmd, re.IGNORECASE)
    if not match:
        return ""

    cmd = match.group(1).strip()
    # Drop a trailing standalone "Note …" that slipped onto the command line.
    cmd = re.sub(r'\s+Note\b.*$', '', cmd, flags=re.IGNORECASE).strip()

    # Keep only the FIRST command variant (drop a second command + prose crammed
    # into the same cell — the common "real DUT / sample app" Sheet pattern).
    cmd = _cut_multi_command(cmd)

    # Remove Note: suffix if on same line
    cmd = re.sub(r'\s*Note:.*$', '', cmd, flags=re.IGNORECASE).strip()

    # Normalize the --PICS value to our runtime placeholder (the real PICS path
    # comes from build_config at run time). The Sheet writes it several ways:
    #   --PICS /real/path            (real path)
    #   --PICS <PICS path>           (angle-bracket placeholder, may contain spaces)
    #   --PICS<PICS File>            (NO space after --PICS)
    # Consume the WHOLE value in every form so no fragment (e.g. "path>") leaks
    # through as a stray argument (argparse: "unrecognized arguments").
    cmd = re.sub(r'--PICS\s*<[^>]*>', '--PICS __PICS_PLACEHOLDER__', cmd)   # <placeholder>
    cmd = re.sub(r'--PICS\s+\S+', '--PICS __PICS_PLACEHOLDER__', cmd)       # real path
    cmd = cmd.strip()

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

        # An empty DUT command is NOT an error — the test is self-orchestrating
        # (it launches its own apps via --string-arg app paths, e.g. SC-3.5,
        # DA-1.9), or the runner builds the DUT from the SDK CI header
        # (Fabric-Sync). The python command IS required.
        if not py_cmd:
            errors.append(f"Row {i}: {tc_id} — could not parse Python command "
                          f"(cell had content but no 'python3 …TC_*.py' found)")
            continue
        if not dut_cmd:
            print(f"[INFO] {tc_id}: no DUT app in cell — self-orchestrating "
                  f"(test launches its own apps / built from CI header).")

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
