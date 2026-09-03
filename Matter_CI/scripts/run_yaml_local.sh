#!/usr/bin/env bash
# =============================================================================
# run_yaml_local.sh — run YAML certification test(s) on the RPi by hand,
# WITHOUT triggering a GitHub Actions run. For fast iteration on the YAML path.
#
# Assumes a prior pipeline run (or a local build) already prepared the SDK:
#   - SDK checked out at rpi.sdk_dir with app binaries symlinked into out/
#   - the controller venv (python_env) exists
# It installs the YAML-runner deps itself (same as prepare_rpi_tests.py), builds
# a one-off test_commands.json, and runs our run_tests.py on just those tests.
#
# Usage:
#   bash Matter_CI/scripts/run_yaml_local.sh                 # defaults to Test_TC_ACE_1_1
#   bash Matter_CI/scripts/run_yaml_local.sh Test_TC_ACL_2_1 Test_TC_BOOL_2_1
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CI_DIR="$(cd "$HERE/.." && pwd)"
CONFIG="$CI_DIR/config/build_config.yaml"

# Resolve SDK dir + venv python from the config (env override wins).
SDK="${MATTER_SDK_DIR:-$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG'))['rpi']['sdk_dir'])" 2>/dev/null || echo "$HOME/connectedhomeip")}"
VENV_NAME="$(python3 -c "import yaml;print(yaml.safe_load(open('$CONFIG')).get('python_controller',{}).get('install_venv_name','python_env'))" 2>/dev/null || echo python_env)"
PY="$SDK/$VENV_NAME/bin/python3"

TESTS=("$@"); [ ${#TESTS[@]} -eq 0 ] && TESTS=("Test_TC_ACE_1_1")

echo "SDK    : $SDK"
echo "venv   : $PY"
echo "tests  : ${TESTS[*]}"
[ -x "$PY" ] || { echo "ERROR: venv python not found at $PY — run the pipeline once (or build) first."; exit 1; }

# 1) YAML-runner deps (idempotent — mirrors prepare_rpi_tests.py::setup_yaml_runner_deps)
echo "[1/3] Ensuring YAML-runner deps in the venv ..."
"$PY" -m pip install --quiet python-path pyyaml websockets coloredlogs diskcache \
  alive_progress tabulate rich aenum construct dacite deprecation ecdsa click colorama
"$PY" -m pip install --quiet -e "$SDK/scripts/py_matter_yamltests" -e "$SDK/scripts/py_matter_idl" || true

# 2) Build a one-off YAML-only commands file
CMDS="$CI_DIR/logs/test_commands.json"
mkdir -p "$CI_DIR/logs"
echo "[2/3] Writing $CMDS ..."
python3 - "$CMDS" "${TESTS[@]}" <<'PY'
import json, sys
out, targets = sys.argv[1], sys.argv[2:]
def tcid(t):
    n = t[len("Test_"):] if t.startswith("Test_") else t
    p = n.split("_")
    return (f"TC-{p[1]}-" + ".".join(p[2:])) if len(p) >= 3 and p[0] == "TC" else t
recs = [{"type": "yaml", "test_case_id": tcid(t), "cluster": "", "yaml_target": t,
         "app": "all-clusters", "pics": "", "dut_command": "",
         "python_command": f"run_test_suite.py --target {t}"} for t in targets]
json.dump(recs, open(out, "w"), indent=2)
print(f"  {len(recs)} YAML test(s)")
PY

# 3) Run our harness on just those tests
echo "[3/3] Running run_tests.py ..."
MATTER_SDK_DIR="$SDK" python3 "$CI_DIR/scripts/run_tests.py" --config "$CONFIG" --commands "$CMDS"

echo
echo "Done. Report : $CI_DIR/logs/report.html"
echo "Per-test log : $CI_DIR/logs/test_runs/"
