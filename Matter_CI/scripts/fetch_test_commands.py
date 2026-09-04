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
def load_tc_list(path: Path) -> dict[str, str] | None:
    """
    Loads tc_list.json — returns {tc_id: cluster_name} for enabled entries.
    If file not found, returns None (= NO selection list → run all rows from sheet).

    None and {} mean DIFFERENT things and callers must not conflate them:
      None → "no TC list at all"     → run every row in the sheet
      {}   → "selection is empty"    → run NOTHING (e.g. a filter matched no TC)
    Treating {} as "run all" is how a single mistyped tc_filter used to launch the
    entire 450-TC suite instead of stopping.
    """
    if not path.exists():
        print(f"[WARN] tc_list.json not found at {path} — will fetch ALL rows from sheet.")
        return None

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
    """Returns set of app names that FAILED to build.

    The build now runs on the Mac mini in Docker, so build_status.json reaches
    this RPi job as the `build-summary-<run>` GitHub artifact, which the workflow
    downloads into Matter_CI/logs/ before calling us. The legacy path
    (logs/build_logs/) is kept for single-machine / local runs.
    """
    log_dir = PROJECT_ROOT / cfg.get("test_execution", {}).get("log_dir", "logs/test_runs")
    logs_root = log_dir.parent
    candidates = [
        logs_root / "build_status.json",                # downloaded build artifact
        logs_root / "build_logs" / "build_status.json", # legacy native-RPi build
    ]
    build_status_file = next((p for p in candidates if p.exists()), None)

    if build_status_file is None:
        print("[INFO] No build_status.json found — assuming all apps built successfully.")
        return set()

    try:
        with open(build_status_file) as f:
            status = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] Could not read {build_status_file}: {e} — "
              f"assuming all apps built successfully.")
        return set()

    failed = {app for app, result in status.items() if result == "FAIL"}
    print(f"[INFO] Build status read from {build_status_file} "
          f"({len(status)} target(s), {len(failed)} failed).")
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
def parse_rows(rows: list, cfg: dict, tc_map: dict[str, str] | None) -> list[dict]:
    """
    tc_map: {tc_id: cluster_name} for enabled TCs (from tc_list.json), or None
    when there is no TC list at all (→ take every row from the sheet).
    An EMPTY dict is a real, empty selection → no rows are taken.
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

        # Filter by tc_map when there IS one (only run selected TCs). `is not
        # None` — NOT truthiness: an empty selection must select nothing, not
        # everything.
        if tc_map is not None and tc_id not in tc_map:
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
        cluster = tc_map.get(tc_id, "") if tc_map else ""   # None/{} → derive below
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
# YAML certification tests (additive to the Sheet-driven Python tests)
# =============================================================================
def _yaml_target_to_tcid(target: str) -> tuple[str, str]:
    """`Test_TC_ACE_1_1` → (`TC-ACE-1.1`, `ACE`). Falls back to the raw target
    for any name that doesn't fit the Test_TC_<CLUSTER>_<x>_<y> shape."""
    name = target[len("Test_"):] if target.startswith("Test_") else target
    parts = name.split("_")
    if len(parts) >= 3 and parts[0] == "TC":
        cluster = parts[1]
        return f"TC-{cluster}-" + ".".join(parts[2:]), cluster
    return target, ""


def _load_sdk_yaml_test_sets(sdk_dir: Path) -> tuple[set, set]:
    """(automated, manual) YAML test-name sets from the SDK's OWN manifests —
    src/app/tests/suites/ciTests.json (runnable) and manualTests.json (excluded).
    Both are {collection: [test names]}; we flatten across collections. Empty
    automated set = manifests missing → caller keeps entries but warns."""
    suites = sdk_dir / "src" / "app" / "tests" / "suites"

    def flat(fname: str) -> set:
        p = suites / fname
        out: set = set()
        if not p.exists():
            print(f"[WARN] {p} not found — cannot validate YAML tests against the SDK.")
            return out
        try:
            data = json.loads(p.read_text())
            for names in (data.values() if isinstance(data, dict) else []):
                if isinstance(names, list):
                    out.update(n for n in names if isinstance(n, str))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[WARN] Could not read {p}: {e}")
        return out

    return flat("ciTests.json"), flat("manualTests.json")


def filter_yaml_by_runtime(yaml_cmds: list[dict], cluster_filter: str,
                           tc_filter: str) -> list[dict]:
    """Apply the workflow's tc_filter / cluster_filter to YAML tests too (they
    were previously only applied to the Sheet's Python tests). tc_filter wins over
    cluster_filter, matching the Python behaviour. Both compare against the same
    fields shown in the report: test_case_id (e.g. TC-ACE-1.1) and cluster."""
    if not cluster_filter and not tc_filter:
        return yaml_cmds
    if tc_filter:
        wanted = {t.strip().upper() for t in tc_filter.split(",") if t.strip()}
        kept = [c for c in yaml_cmds if c["test_case_id"].upper() in wanted]
        print(f"[INFO] YAML tc_filter: {len(kept)}/{len(yaml_cmds)} match {sorted(wanted)}")
        return kept
    wanted = {c.strip().lower() for c in cluster_filter.split(",") if c.strip()}
    kept = [c for c in yaml_cmds if c["cluster"].strip().lower() in wanted]
    print(f"[INFO] YAML cluster_filter: {len(kept)}/{len(yaml_cmds)} match {sorted(wanted)}")
    return kept


def load_yaml_tests(cfg: dict) -> list[dict]:
    """Build YAML test records from config/yaml_tests.json, validated against the
    SDK's automated set. Returns [] when YAML testing is disabled or nothing is
    selected. Each record carries type='yaml' so run_tests.py routes it to the
    run_test_suite.py path instead of the Python-controller path."""
    yt = cfg.get("yaml_tests", {}) or {}
    if not yt.get("enabled", False):
        return []

    list_file = PROJECT_ROOT / yt.get("list_file", "config/yaml_tests.json")
    if not list_file.exists():
        print(f"[WARN] YAML list file not found: {list_file} — no YAML tests.")
        return []
    try:
        raw = json.loads(list_file.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[ERROR] Could not read {list_file}: {e}")
        return []

    entries = raw.get("tests", []) if isinstance(raw, dict) else raw
    entries = [e for e in (entries or []) if isinstance(e, dict) and e.get("enabled")]
    if not entries:
        print("[INFO] No ENABLED YAML tests in yaml_tests.json.")
        return []

    sdk_dir = Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))
    automated, manual = _load_sdk_yaml_test_sets(sdk_dir)

    records, warned = [], []
    for e in entries:
        target = str(e.get("test", "")).strip()
        if not target:
            continue
        # The SDK manifests are ADVISORY, not a gate: yaml_tests.json is the source
        # of truth for what runs. We warn (don't skip) when a test isn't in the
        # SDK's automated set — it may be manual/UI/simulated or lack a YAML file,
        # so it can fail, and that's acceptable. Run everything the user enabled.
        if automated and target not in automated:
            warned.append(f"{target} (not in SDK ciTests.json — may be manual/UI/"
                          f"simulated or missing; running anyway)")
        elif target in manual:
            warned.append(f"{target} (SDK-classified manual — running anyway)")
        elif target.endswith("_Simulated"):
            warned.append(f"{target} (simulated test — running anyway)")
        tcid, abbrev = _yaml_target_to_tcid(target)
        # Full cluster name from the config (for the HTML report); fall back to
        # the abbreviation derived from the target name when not provided.
        cluster = str(e.get("cluster", "")).strip() or abbrev
        records.append({
            "type":           "yaml",
            "test_case_id":   tcid,
            "cluster":        cluster,
            "yaml_target":    target,
            "app":            str(e.get("app", "")).strip(),   # authoritative; "" = SDK picks
            "pics":           str(e.get("pics", "")).strip(),
            "dut_command":    "",   # run_test_suite.py launches the app itself
            "python_command": f"run_test_suite.py --target {target}",
        })

    if warned:
        print(f"[WARN] {len(warned)} YAML test(s) not in the SDK's automated set — "
              f"running anyway (may fail):")
        for w in warned:
            print(f"  ⚠️  {w}")
    print(f"[INFO] {len(records)} YAML test(s) selected (all enabled entries run; "
          f"SDK ciTests.json is advisory).")
    return records


# =============================================================================
# Main
# =============================================================================
def apply_runtime_filters(tc_map: dict | None, cluster_filter: str,
                          tc_filter: str) -> dict | None:
    """
    Apply runtime filters from workflow inputs (cluster_filter / tc_filter).
    Priority: tc_filter > cluster_filter > tc_map (all enabled).

    Returns a possibly-EMPTY dict when a filter was given and matched nothing.
    The caller MUST treat that as "run nothing" and abort — never as "run all".
    Returns None only when there was no TC list AND no filter (= run everything).
    """
    if not cluster_filter and not tc_filter:
        return tc_map   # no runtime filter — use tc_list.json as-is

    # TC filter — specific TC IDs override everything
    if tc_filter:
        tc_ids = [t.strip() for t in tc_filter.split(",") if t.strip()]
        if tc_map is None:
            # No tc_list.json to validate against — take the IDs at face value.
            print(f"[INFO] TC filter applied (no tc_list.json to validate against): "
                  f"{len(tc_ids)} TC(s)")
            return {tc_id: "Unknown" for tc_id in tc_ids}
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
        if tc_map is None:
            print("[ERROR] cluster_filter needs tc_list.json (cluster names come "
                  "from it), but no TC list was found — cannot filter by cluster.")
            return {}
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

    # Which kind of tests to run (workflow input; default both). yaml/python
    # restrict to that one kind; both runs the Sheet Python tests AND the curated
    # YAML tests.
    test_type = os.environ.get("TEST_TYPE", "both").strip().lower()
    if test_type not in ("both", "python", "yaml"):
        print(f"[WARN] Unknown TEST_TYPE '{test_type}' — defaulting to 'both'.")
        test_type = "both"
    if test_type != "both":
        print(f"[INFO] Test type filter: {test_type} only")

    # YAML tests come from config/yaml_tests.json (the Sheet lists only Python
    # tests), validated against the SDK's ciTests.json. Loaded first so an empty
    # Python selection can still proceed on a YAML-only run.
    yaml_cmds = load_yaml_tests(cfg) if test_type in ("both", "yaml") else []
    # The tc_filter / cluster_filter workflow inputs apply to YAML tests too.
    yaml_cmds = filter_yaml_by_runtime(yaml_cmds, cluster_filter, tc_filter)

    # An empty Python selection is a HARD STOP — UNLESS there are YAML tests to
    # run. test_type=yaml forces the Python side empty (Sheet is skipped entirely).
    python_selected = test_type in ("both", "python")
    python_empty = (not python_selected) or (tc_map is not None and not tc_map)
    if python_empty and not yaml_cmds:
        if test_type == "yaml":
            print("[ERROR] test_type=yaml but no YAML tests selected — enable entries "
                  "in yaml_tests.json (and ensure they're in the SDK's ciTests.json).")
            sys.exit(1)
        tc_list_file = cfg["test_execution"]["tc_list_file"]
        if tc_filter or cluster_filter:
            print("[ERROR] The requested filter selected 0 test cases — nothing to run.")
            if tc_filter:
                print(f"[ERROR]   tc_filter      = '{tc_filter}'")
            if cluster_filter:
                print(f"[ERROR]   cluster_filter = '{cluster_filter}'")
            print(f"[ERROR] Check the TC IDs / cluster names against {tc_list_file} "
                  f"(entries must also be enabled).")
        else:
            print(f"[ERROR] {tc_list_file} has no ENABLED test cases — nothing to run.")
        print("[ERROR] Refusing to fall back to running the full suite.")
        sys.exit(1)

    # Fetch + parse the Sheet ONLY when there is a Python selection. A YAML-only
    # run (Python selection empty) skips the Sheet entirely.
    commands: list[dict] = []
    if not python_empty:
        if tc_map is None:
            print("[INFO] No TC list and no filter — running every row in the sheet.")
        else:
            print(f"[INFO] Running {len(tc_map)} Python test case(s)")
            clusters = sorted(set(tc_map.values()))
            print(f"[INFO] Clusters: {clusters}")
        rows     = fetch_sheet(cfg)
        commands = parse_rows(rows, cfg, tc_map)
    elif yaml_cmds:
        print("[INFO] No Python tests selected — running YAML tests only.")

    # Append the validated YAML tests (additive to the Python suite).
    commands.extend(yaml_cmds)

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
