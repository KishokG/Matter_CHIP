#!/usr/bin/env bash
# =============================================================================
# run_thcli_local.sh — run one/some test cases through the Matter Test Harness
# CLI (th-cli) on the RPi by hand, WITHOUT triggering a GitHub Actions run.
# For fast iteration on the TH-CLI execution path (run_tests.py EXECUTION_MODE=thcli).
#
# It builds a one-off test_commands.json for the given TC IDs and runs our
# run_tests.py in TH-CLI mode. The sample apps, th-cli and configs are the RPi's
# own (apps_dir / th_cli_dir / config_dir in build_config.yaml) — we build nothing.
#
# Usage:
#   bash Matter_CI/scripts/run_thcli_local.sh TC-ACE-1.2
#   bash Matter_CI/scripts/run_thcli_local.sh TC-ACE-1.2 TC-IDM-10.2
#   # A YAML test (no Sheet command → synthesized th-cli command):
#   TYPE=yaml bash Matter_CI/scripts/run_thcli_local.sh TC-ACE-1.1
#   # Force a fresh TH project instead of the configured default_project_id:
#   THCLI_NEW_PROJECT=1 bash Matter_CI/scripts/run_thcli_local.sh TC-ACE-1.2
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_DIR="$(cd "$HERE/.." && pwd)"
CONFIG="$CI_DIR/config/build_config.yaml"
TYPE="${TYPE:-python}"        # python (Sheet command) | yaml (synthesized)

TCS=("$@"); [ ${#TCS[@]} -eq 0 ] && TCS=("TC-ACE-1.2")

echo "config : $CONFIG"
echo "type   : $TYPE"
echo "tests  : ${TCS[*]}"

# pexpect drives th-cli's interactive prompts over a pty.
python3 -m pip install pexpect --break-system-packages --quiet 2>/dev/null \
  || pip3 install pexpect --quiet 2>/dev/null || true

# Build a one-off commands file. For python tests we leave thcli_command empty so
# run_tests.py synthesizes a default (add the real Sheet column-G command here to
# exercise -c/-p handling); for yaml we set type=yaml so it's fully synthesized.
CMDS="$CI_DIR/logs/test_commands.json"
mkdir -p "$CI_DIR/logs"
python3 - "$CMDS" "$TYPE" "${TCS[@]}" <<'PY'
import json, sys
out, ttype, tcs = sys.argv[1], sys.argv[2], sys.argv[3:]
def underscore(t): return t.replace("-", "_").replace(".", "_")
recs = []
for t in tcs:
    rec = {
        "test_case_id":   t,
        "cluster":        t.split("-")[1] if "-" in t else "",
        "type":           ttype,
        "dut_command":    "./chip-all-clusters-app",   # edit per test as needed
        "python_command": f"run via th-cli ({t})",
        "thcli_command":  "",                          # empty → run_tests.py builds a default
    }
    recs.append(rec)
json.dump(recs, open(out, "w"), indent=2)
print(f"  wrote {len(recs)} record(s) → {out}")
PY

echo "[run] EXECUTION_MODE=thcli run_tests.py ..."
EXECUTION_MODE=thcli \
THCLI_NEW_PROJECT="${THCLI_NEW_PROJECT:-0}" \
MATTER_SDK_DIR="${MATTER_SDK_DIR:-$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['rpi']['sdk_dir'])")}" \
python3 "$CI_DIR/scripts/run_tests.py" --config "$CONFIG" --commands "$CMDS"

echo
echo "Done. Report      : $CI_DIR/logs/report.html"
echo "Per-test logs     : $CI_DIR/logs/test_runs/  (<TC>.log, <TC>_dut.log, <TC>_th.log)"
