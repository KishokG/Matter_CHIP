#!/usr/bin/env bash
# =============================================================================
# build.sh — Runs ON the Raspberry Pi
#
# Three build modes (set via --mode flag):
#   full        → clone SDK + clean + bootstrap + build
#   skip-clone  → git pull + clean + bootstrap + build
#   skip-all    → build only (same commit, no clone/bootstrap/clean)
#
# Resilient build:
#   - Each app builds independently — one failure does NOT stop others
#   - chip-tool and python-controller failures ARE fatal (tests need them)
#   - Build errors saved to <app>_build_error.log with clear conclusions
#   - Exit code reflects overall status
# =============================================================================

set -eo pipefail   # no -u — activate.sh has unbound var refs

# ── CLI args ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/config/build_config.yaml"
BUILD_MODE="full"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)  CONFIG_FILE="$2"; shift 2 ;;
        --mode)    BUILD_MODE="$2";  shift 2 ;;
        --skip-sdk) BUILD_MODE="skip-all"; shift ;;
        *) echo "[WARN] Unknown arg: $1"; shift ;;
    esac
done

if [[ "${BUILD_MODE}" != "full" && "${BUILD_MODE}" != "skip-clone" && "${BUILD_MODE}" != "skip-all" ]]; then
    echo "[ERROR] Invalid --mode '${BUILD_MODE}'. Must be: full | skip-clone | skip-all"
    exit 1
fi

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()    { echo -e "${CYAN}[BUILD]${NC} $*"; }
ok()     { echo -e "${GREEN}[  OK ]${NC} $*"; }
warn()   { echo -e "${YELLOW}[ WARN]${NC} $*"; }
fail()   { echo -e "${RED}[FAIL ]${NC} $*" >&2; exit 1; }
banner() {
    echo -e "\n${BOLD}${CYAN}══════════════════════════════════════${NC}"
    echo -e "${BOLD}${CYAN}  $*${NC}"
    echo -e "${BOLD}${CYAN}══════════════════════════════════════${NC}\n"
}

# ── Build result tracking ─────────────────────────────────────────────────────
# Associative arrays to track per-app build results
declare -A BUILD_STATUS    # app_name → PASS | FAIL
declare -A BUILD_ERROR     # app_name → error summary string
FAILED_APPS=()
PASSED_APPS=()

# ── YAML helpers ──────────────────────────────────────────────────────────────
cfg_get() {
    python3 - "${CONFIG_FILE}" "$@" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
val = cfg
for k in sys.argv[2:]:
    val = val.get(k, "") if isinstance(val, dict) else ""
print("" if val is None else val)
PY
}

# Emits one JSON object per line for each enabled reference app, read from
# the discovered-apps cache produced by discover_apps(). Same output contract
# as before (one compact JSON dict per line) so build_apps/clean/summary are
# unchanged. Returns nothing if discovery hasn't run yet.
cfg_apps() {
    [[ -f "${DISCOVERED_APPS_JSON}" ]] || return 0
    python3 - "${DISCOVERED_APPS_JSON}" <<'PY'
import sys, json
try:
    apps = json.load(open(sys.argv[1]))
except Exception:
    apps = []
for a in apps:
    if a.get("enabled", True):
        print(json.dumps(a))
PY
}

cfg_bool() {
    [[ "$(cfg_get "$@")" =~ ^[Tt]rue$ ]]
}

# ── Read SDK config ───────────────────────────────────────────────────────────
SDK_REPO=$(cfg_get sdk repo)
SDK_PLATFORM=$(cfg_get sdk platform)
SDK_PLATFORM="${SDK_PLATFORM:-linux}"
SUBMODULE_JOBS=$(cfg_get sdk submodule_jobs)
SUBMODULE_JOBS="${SUBMODULE_JOBS:-4}"
SDK_DIR="${MATTER_SDK_DIR:-$(cfg_get rpi sdk_dir)}"
CONFIG_BRANCH=$(cfg_get sdk branch)
CONFIG_SHA=$(cfg_get sdk sha)
SDK_BRANCH="${RUNTIME_SDK_BRANCH:-${CONFIG_BRANCH}}"
SDK_SHA="${RUNTIME_SDK_SHA:-${CONFIG_SHA}}"

# Log dir for build error logs
BUILD_LOG_DIR="${PROJECT_ROOT}/logs/build_logs"
mkdir -p "${BUILD_LOG_DIR}"

# Resolved reference-app list, produced once by discover_apps() and read by
# cfg_apps() everywhere else. This is the single source of truth for the
# build — the hardcoded apps: block in build_config.yaml is gone; apps are
# resolved dynamically from the SDK's own HostApp mapping.
DISCOVERED_APPS_JSON="${BUILD_LOG_DIR}/discovered_apps.json"

# =============================================================================
# Error conclusion helper
# Analyses error output and gives a human-readable conclusion
# =============================================================================
diagnose_error() {
    local app_name="$1"
    local error_log="$2"
    local conclusion=""

    if [[ ! -f "${error_log}" ]]; then
        echo "Build log not found — build may have crashed before starting"
        return
    fi

    local content
    content=$(cat "${error_log}" 2>/dev/null || echo "")

    # Check for common errors in order of specificity
    if echo "${content}" | grep -q "No such file or directory.*source_dir\|does not exist\|source directory"; then
        conclusion="❌ WRONG SOURCE PATH — The source_dir in build_config.yaml does not exist in the SDK."
        conclusion+="\n   Check: source_dir for '${app_name}' in build_config.yaml"
    elif echo "${content}" | grep -q "No such file or directory.*build_dir\|cannot create.*build"; then
        conclusion="❌ WRONG BUILD PATH — The build_dir in build_config.yaml is invalid."
        conclusion+="\n   Check: build_dir for '${app_name}' in build_config.yaml"
    elif echo "${content}" | grep -q "fatal error:.*No such file or directory"; then
        local missing_header
        missing_header=$(echo "${content}" | grep "fatal error:" | grep "No such file" | head -1 | sed "s/.*fatal error: //")
        conclusion="❌ MISSING HEADER FILE — ${missing_header}"
        conclusion+="\n   Fix: Install missing system package (check apt-packages.txt)"
    elif echo "${content}" | grep -q "error:.*command not found\|gn: not found\|ninja: not found"; then
        conclusion="❌ TOOL NOT FOUND — gn or ninja not in PATH."
        conclusion+="\n   Fix: Run with full/skip-clone mode to re-run bootstrap"
    elif echo "${content}" | grep -q "out of memory\|OOM\|std::bad_alloc"; then
        conclusion="❌ OUT OF MEMORY — Build ran out of RAM."
        conclusion+="\n   Fix: Add/increase swap space on RPi (see README)"
    elif echo "${content}" | grep -q "error:.*undeclared\|error:.*undefined reference"; then
        conclusion="❌ COMPILATION ERROR — Code compile error (likely SDK version mismatch)."
        conclusion+="\n   Fix: Check SDK branch/SHA in config or try full mode rebuild"
    elif echo "${content}" | grep -q "subcommand failed\|ninja: build stopped"; then
        conclusion="❌ NINJA BUILD FAILED — One or more compile units failed."
        conclusion+="\n   Check the error log for specific compile errors"
    elif echo "${content}" | grep -q "timeout\|timed out"; then
        conclusion="❌ BUILD TIMEOUT — Build exceeded time limit."
        conclusion+="\n   Fix: Increase timeout or reduce parallel jobs"
    elif echo "${content}" | grep -q "permission denied\|Permission denied"; then
        conclusion="❌ PERMISSION DENIED — File or directory permission issue."
        conclusion+="\n   Fix: Check file ownership on RPi"
    else
        conclusion="❌ BUILD FAILED — Unknown error. Check full log for details."
    fi

    echo -e "${conclusion}"
}

# =============================================================================
# STEP 0 — Install system dependencies
# =============================================================================
install_system_deps() {
    banner "Step 0 — System Dependencies"

    local apt_file="${PROJECT_ROOT}/apt-packages.txt"

    if [[ ! -f "${apt_file}" ]]; then
        warn "apt-packages.txt not found — skipping apt dep check."
    else
        local packages=()
        while IFS= read -r line; do
            [[ "${line}" =~ ^#.*$ || -z "${line}" ]] && continue
            packages+=("${line}")
        done < "${apt_file}"

        if [[ ${#packages[@]} -gt 0 ]]; then
            log "Checking ${#packages[@]} required apt packages..."
            local missing=()
            for pkg in "${packages[@]}"; do
                if ! dpkg -s "${pkg}" &>/dev/null 2>&1; then
                    missing+=("${pkg}")
                fi
            done
            if [[ ${#missing[@]} -eq 0 ]]; then
                ok "All apt packages already installed."
            else
                log "Installing ${#missing[@]} missing package(s): ${missing[*]}"
                sudo apt-get update -qq
                sudo apt-get install -y "${missing[@]}"
                ok "Apt packages installed."
            fi
        fi
    fi

    # pip packages
    log "Checking pip packages..."
    python3 -c "import cairo" &>/dev/null 2>&1 || {
        log "Installing pycairo..."
        pip3 install pycairo --break-system-packages --quiet
        ok "pycairo installed."
    }
    ok "All pip packages present."
}

# =============================================================================
# STEP 1 — Clone SDK (full mode only)
# =============================================================================
sdk_clone() {
    banner "Step 1 — SDK Clone"
    log "Repo   : ${SDK_REPO}"
    log "Branch : ${SDK_BRANCH}"
    log "SHA    : ${SDK_SHA:-<HEAD of branch>}"
    log "Dir    : ${SDK_DIR}"

    # Validate repo URL
    if [[ -z "${SDK_REPO}" ]]; then
        fail "❌ MISSING CONFIG — sdk.repo is empty in build_config.yaml"
    fi
    if [[ -z "${SDK_BRANCH}" ]]; then
        fail "❌ MISSING CONFIG — sdk.branch is empty in build_config.yaml"
    fi

    if [[ -d "${SDK_DIR}/.git" ]]; then
        log "Existing SDK found — removing for clean clone..."
        rm -rf "${SDK_DIR}"
    fi

    log "Cloning SDK (depth=1)..."
    git clone \
        --branch "${SDK_BRANCH}" \
        --depth 1 \
        "${SDK_REPO}" \
        "${SDK_DIR}" || fail "❌ CLONE FAILED — Check network and SDK repo URL: ${SDK_REPO}"

    cd "${SDK_DIR}"

    if [[ -n "${SDK_SHA}" ]]; then
        log "Pinning to SHA: ${SDK_SHA}"
        git fetch --depth 1 origin "${SDK_SHA}" 2>/dev/null || \
            git fetch origin || \
            fail "❌ SHA NOT FOUND — SHA '${SDK_SHA}' does not exist in repo"
        git checkout "${SDK_SHA}" || \
            fail "❌ CHECKOUT FAILED — Could not checkout SHA: ${SDK_SHA}"
    fi

    log "Initialising submodules (platform: ${SDK_PLATFORM}, jobs: ${SUBMODULE_JOBS})..."
    for attempt in 1 2 3; do
        log "Submodule checkout attempt ${attempt}/3..."
        python3 scripts/checkout_submodules.py \
            --platform "${SDK_PLATFORM}" \
            --shallow \
            --recursive \
            --jobs "${SUBMODULE_JOBS}" \
            --allow-changing-global-git-config \
            && break \
            || {
                warn "Submodule checkout failed on attempt ${attempt} — retrying in 5s..."
                sleep 5
            }
    done

    ok "SDK cloned → commit: $(git rev-parse --short HEAD)"
}

# =============================================================================
# STEP 1b — Update existing SDK (skip-clone mode)
# =============================================================================
sdk_update() {
    banner "Step 1 — SDK Update (skip clone)"

    if [[ ! -d "${SDK_DIR}/.git" ]]; then
        fail "❌ SDK NOT FOUND — No SDK at '${SDK_DIR}'
   Fix: Run with --mode full to do a fresh clone first
   Or check rpi.sdk_dir in build_config.yaml"
    fi

    cd "${SDK_DIR}"
    log "Current commit : $(git rev-parse --short HEAD)"
    log "Current branch : $(git rev-parse --abbrev-ref HEAD)"
    log "Config branch  : ${SDK_BRANCH}"
    log "Config SHA     : ${SDK_SHA:-<none — will use branch HEAD>}"

    if [[ -n "${SDK_SHA}" ]]; then
        log "Fetching SHA: ${SDK_SHA}"
        git fetch --depth 1 origin "${SDK_SHA}" 2>/dev/null || git fetch origin
        git checkout "${SDK_SHA}" || \
            fail "❌ SHA NOT FOUND — SHA '${SDK_SHA}' does not exist. Check sdk.sha in config"
    else
        log "Switching to branch: ${SDK_BRANCH}"
        git fetch origin "${SDK_BRANCH}" || \
            fail "❌ BRANCH NOT FOUND — Branch '${SDK_BRANCH}' not found in remote
   Fix: Check sdk.branch in build_config.yaml
   Available branches: git branch -r"
        git checkout -B "${SDK_BRANCH}" "origin/${SDK_BRANCH}"
        log "Now on branch  : $(git rev-parse --abbrev-ref HEAD)"
    fi

    log "Updating submodules (platform: ${SDK_PLATFORM}, jobs: ${SUBMODULE_JOBS})..."
    for attempt in 1 2 3; do
        log "Submodule checkout attempt ${attempt}/3..."
        python3 scripts/checkout_submodules.py \
            --platform "${SDK_PLATFORM}" \
            --shallow \
            --recursive \
            --jobs "${SUBMODULE_JOBS}" \
            --allow-changing-global-git-config \
            && break \
            || {
                warn "Submodule checkout failed on attempt ${attempt} — retrying in 5s..."
                sleep 5
            }
    done

    ok "SDK updated → branch: $(git rev-parse --abbrev-ref HEAD)  commit: $(git rev-parse --short HEAD)"
}

# =============================================================================
# STEP 2 — Bootstrap
# =============================================================================
sdk_bootstrap() {
    banner "Step 2 — Bootstrap"
    cd "${SDK_DIR}"

    if ! cfg_bool sdk bootstrap; then
        warn "Bootstrap disabled in config — skipping."
        return
    fi

    log "Running: source scripts/bootstrap.sh"
    bash scripts/bootstrap.sh || \
        fail "❌ BOOTSTRAP FAILED — Environment setup failed
   Fix: Check system dependencies in apt-packages.txt
   Run: sudo apt install -y \$(grep -v '^#' apt-packages.txt | tr '\n' ' ')"
    ok "Bootstrap complete."
}

# =============================================================================
# STEP 3 — Clean old build outputs
# =============================================================================
clean_old_builds() {
    banner "Step 3 — Clean Old Build Outputs"
    cd "${SDK_DIR}"

    if [[ -d ".environment" ]]; then
        log "Removing .environment/ ..."
        rm -rf .environment
        ok "Removed .environment/"
    fi

    # All reference apps, chip-tool and the python controller build into
    # out/<name> (see discovery + chip_tool config; python_lib lives under
    # out/ too). Removing out/ gives a clean rebuild without needing the
    # resolved app list here — which isn't available yet, since discovery
    # runs after bootstrap (it needs scripts/activate.sh).
    if [[ -d "out" ]]; then
        log "Removing out/ (all reference app + chip-tool build outputs)..."
        rm -rf out
        ok "Removed out/"
    fi

    if cfg_bool python_controller enabled; then
        # The python venv lives at the SDK root (not under out/), so remove it
        # explicitly. Its build output (out/python_lib) was already removed
        # with out/ above — otherwise ninja would do an incremental build
        # (e.g. 140 steps instead of 800+) from cached .o files.
        local venv_name
        venv_name=$(cfg_get python_controller install_venv_name)
        [[ -d "${venv_name}" ]] && rm -rf "${venv_name}" && log "Removed python venv: ${venv_name}"
    fi

    ok "Clean complete."
}

# =============================================================================
# Activate environment
# =============================================================================
activate_env() {
    cd "${SDK_DIR}"
    log "Activating Matter environment..."

    if [[ ! -f "scripts/activate.sh" ]]; then
        fail "❌ ACTIVATE.SH NOT FOUND — SDK may not be properly cloned
   Expected: ${SDK_DIR}/scripts/activate.sh
   Fix: Run with --mode full to re-clone the SDK"
    fi

    set +u
    source scripts/activate.sh
    set -u

    # Verify gn and ninja are available
    if ! command -v gn &>/dev/null; then
        fail "❌ GN NOT FOUND — Bootstrap may not have completed successfully
   Fix: Run with --mode full or skip-clone to re-run bootstrap"
    fi
    if ! command -v ninja &>/dev/null; then
        fail "❌ NINJA NOT FOUND — Bootstrap may not have completed successfully
   Fix: Run with --mode full or skip-clone to re-run bootstrap"
    fi

    ok "Environment activated. gn=$(command -v gn)  ninja=$(command -v ninja)"
}

# =============================================================================
# STEP 3.5 — Discover reference apps from the SDK (replaces hardcoded apps:)
#
# Runs discover_targets.py --emit-apps-json to resolve each app's real
# source_dir + binary_name from the SDK's own HostApp mapping, filtered by
# discovery.include / discovery.exclude in build_config.yaml. Must run AFTER
# bootstrap — it sources scripts/activate.sh to query build_examples.py.
# =============================================================================
discover_apps() {
    banner "Step 3.5 — Discover Reference Apps"

    local discover_log="${BUILD_LOG_DIR}/discover.log"
    log "Resolving reference apps from SDK (discover_targets.py)..."
    log "  SDK    : ${SDK_DIR}"
    log "  Config : ${CONFIG_FILE}"

    if ! python3 "${SCRIPT_DIR}/discover_targets.py"             --sdk-dir "${SDK_DIR}"             --config "${CONFIG_FILE}"             --emit-apps-json             > "${DISCOVERED_APPS_JSON}" 2> "${discover_log}"; then
        cat "${discover_log}" >&2
        fail "❌ APP DISCOVERY FAILED — could not resolve apps from SDK
   Log: ${discover_log}
   Common causes: SDK not bootstrapped, or build_examples.py query failed."
    fi

    local n
    n=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))"             "${DISCOVERED_APPS_JSON}" 2>/dev/null || echo 0)

    if [[ "${n}" -eq 0 ]]; then
        cat "${discover_log}" >&2
        fail "❌ APP DISCOVERY returned 0 apps
   Check discovery.include in build_config.yaml (names must be SDK shorthands).
   Log: ${discover_log}"
    fi

    ok "Discovered ${n} reference app(s) → ${DISCOVERED_APPS_JSON}"
    # Surface any resolution warnings (e.g. an include name that didn't resolve)
    grep -E "^\[WARN\]" "${discover_log}" >&2 || true

    python3 - "${DISCOVERED_APPS_JSON}" <<'PY'
import json, sys
for a in json.load(open(sys.argv[1])):
    print(f"   • {a['name']:24} {a['source_dir']:44} -> {a['binary_name']}")
PY
    echo ""
}

# =============================================================================
# STEP 4 — Build reference apps (resilient — continues on single app failure)
# =============================================================================
build_apps() {
    banner "Step 4 — Reference Apps"

    local app_lines
    mapfile -t app_lines < <(cfg_apps)

    if [[ ${#app_lines[@]} -eq 0 ]]; then
        warn "No reference apps enabled — skipping."
        return
    fi

    log "Building ${#app_lines[@]} enabled app(s) (failures are non-fatal)..."
    activate_env
    cd "${SDK_DIR}"

    for line in "${app_lines[@]}"; do
        local name source_dir build_dir extra_gn_args
        name=$(echo "${line}"          | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['name'])")
        source_dir=$(echo "${line}"    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['source_dir'])")
        build_dir=$(echo "${line}"     | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['build_dir'])")
        extra_gn_args=$(echo "${line}" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('extra_gn_args',''))")

        log "┌─ Building : ${name}"
        log "│  source   : ${source_dir}"
        log "│  output   : ${build_dir}"
        log "│  gn_args  : ${extra_gn_args:-<none>}"
        log "│  command  : cd ${SDK_DIR} && scripts/examples/gn_build_example.sh ${source_dir} ${build_dir} ${extra_gn_args}"

        # Validate source directory exists before attempting build
        if [[ ! -d "${source_dir}" ]]; then
            local err_log="${BUILD_LOG_DIR}/${name}_build_error.log"
            echo "Source directory not found: ${SDK_DIR}/${source_dir}" > "${err_log}"
            echo "Configured source_dir: ${source_dir}" >> "${err_log}"
            echo "Check source_dir for '${name}' in build_config.yaml" >> "${err_log}"
            BUILD_STATUS["${name}"]="FAIL"
            BUILD_ERROR["${name}"]="❌ WRONG SOURCE PATH — '${source_dir}' does not exist in SDK\n   Fix: Check source_dir for '${name}' in build_config.yaml"
            FAILED_APPS+=("${name}")
            echo -e "${RED}[FAIL]${NC} ${name} — source directory not found: ${source_dir}"
            continue
        fi

        # Run build with live ninja progress, continue on failure
        local err_log="${BUILD_LOG_DIR}/${name}_build_error.log"
        local tmp_log="${BUILD_LOG_DIR}/${name}_build_full.log"
        local start_ts
        start_ts=$(date +%s)

        # Run build in background, pipe output to log + live progress
        scripts/examples/gn_build_example.sh                 "${source_dir}"                 "${build_dir}"                 ${extra_gn_args}                 > "${tmp_log}" 2>&1 &
        local build_pid=$!

        # Stream ninja progress — print every 50th step + last step + errors
        local last_printed=0
        local last_step=""
        local step_num=0
        local step_total=0

        while kill -0 "${build_pid}" 2>/dev/null; do
            # Read the latest ninja step from log
            local latest
            latest=$(tail -1 "${tmp_log}" 2>/dev/null || echo "")

            if [[ "${latest}" =~ ^\[([[:space:]0-9]+)/([0-9]+)\] ]]; then
                step_num="${BASH_REMATCH[1]// /}"   # trim spaces
                step_total="${BASH_REMATCH[2]}"

                # Print every 50 steps or when step changes significantly
                if (( step_num - last_printed >= 50 )) ||                    [[ "${latest}" == *"FAILED:"* ]]; then
                    echo -e "  ${CYAN}[${step_num}/${step_total}]${NC} ${latest#*\] }"
                    last_printed="${step_num}"
                fi
                last_step="${latest}"
            elif [[ "${latest}" == *"FAILED:"* ]]; then
                echo -e "  ${RED}[FAIL]${NC} ${latest}"
            fi

            sleep 1
        done

        # Wait for build to finish and get exit code
        wait "${build_pid}"
        local build_rc=$?

        # Always print the last ninja step (final link/compile)
        if [[ -n "${last_step}" ]]; then
            local elapsed=$(( $(date +%s) - start_ts ))
            local elapsed_str
            if (( elapsed >= 60 )); then
                elapsed_str="$((elapsed/60))m $((elapsed%60))s"
            else
                elapsed_str="${elapsed}s"
            fi
            echo -e "  ${CYAN}[${step_num}/${step_total}]${NC} ${last_step#*\] }"
        fi

        if [[ ${build_rc} -eq 0 ]]; then
            local binary_path="${SDK_DIR}/${build_dir}/$(echo "${line}" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['binary_name'])")"
            BUILD_STATUS["${name}"]="PASS"
            PASSED_APPS+=("${name}")
            ok "└─ ${name} built successfully → ${binary_path}"
        else
            # Save last 50 lines as error log
            tail -50 "${tmp_log}" > "${err_log}" 2>/dev/null || true

            # Print error lines directly to console
            echo -e "
${RED}── Build error output ──────────────────────────────${NC}"
            grep -E "error:|FAILED:|fatal error:" "${tmp_log}" 2>/dev/null | tail -20 ||                 tail -20 "${tmp_log}" 2>/dev/null
            echo -e "${RED}────────────────────────────────────────────────────${NC}
"

            # Diagnose and save conclusion
            local conclusion
            conclusion=$(diagnose_error "${name}" "${err_log}")
            BUILD_STATUS["${name}"]="FAIL"
            BUILD_ERROR["${name}"]="${conclusion}"
            FAILED_APPS+=("${name}")

            echo -e "${RED}[FAIL]${NC} └─ ${name} build failed after ${elapsed_str:-?}"
            echo -e "       ${conclusion}"
            echo -e "       Full log : ${tmp_log}"
            echo -e "       Error log: ${err_log}"
        fi
        echo ""
    done
}

# =============================================================================
# STEP 5 — Build chip-tool (FATAL on failure — tests need it)
# =============================================================================
build_chip_tool() {
    banner "Step 5 — chip-tool"

    if ! cfg_bool chip_tool enabled; then
        warn "chip-tool disabled — skipping."
        return
    fi

    local source_dir build_dir extra_gn_args
    source_dir=$(cfg_get chip_tool source_dir)
    build_dir=$(cfg_get chip_tool build_dir)
    extra_gn_args=$(cfg_get chip_tool extra_gn_args)

    # Validate source dir
    if [[ ! -d "${SDK_DIR}/${source_dir}" ]]; then
        fail "❌ WRONG SOURCE PATH — chip-tool source_dir '${source_dir}' does not exist in SDK
   Fix: Check chip_tool.source_dir in build_config.yaml
   Expected path: ${SDK_DIR}/${source_dir}"
    fi

    activate_env
    cd "${SDK_DIR}"

    local err_log="${BUILD_LOG_DIR}/chip-tool_build_error.log"
    local tmp_log="${BUILD_LOG_DIR}/chip-tool_build_full.log"
    local start_ts
    start_ts=$(date +%s)

    log "Building chip-tool..."
    log "  command : cd ${SDK_DIR} && scripts/examples/gn_build_example.sh ${source_dir} ${build_dir} ${extra_gn_args}"
    scripts/examples/gn_build_example.sh             "${source_dir}"             "${build_dir}"             ${extra_gn_args}             > "${tmp_log}" 2>&1 &
    local build_pid=$!

    local last_printed=0
    local last_step=""
    local step_num=0
    local step_total=0

    while kill -0 "${build_pid}" 2>/dev/null; do
        local latest
        latest=$(tail -1 "${tmp_log}" 2>/dev/null || echo "")
        if [[ "${latest}" =~ ^\[([[:space:]0-9]+)/([0-9]+)\] ]]; then
            step_num="${BASH_REMATCH[1]// /}"
            step_total="${BASH_REMATCH[2]}"
            if (( step_num - last_printed >= 50 )); then
                echo -e "  ${CYAN}[${step_num}/${step_total}]${NC} ${latest#*\] }"
                last_printed="${step_num}"
            fi
            last_step="${latest}"
        fi
        sleep 1
    done
    wait "${build_pid}"
    local build_rc=$?

    local elapsed=$(( $(date +%s) - start_ts ))
    local elapsed_str
    if (( elapsed >= 60 )); then elapsed_str="$((elapsed/60))m $((elapsed%60))s"
    else elapsed_str="${elapsed}s"; fi

    if [[ -n "${last_step}" ]]; then
        echo -e "  ${CYAN}[${step_num}/${step_total}]${NC} ${last_step#*\] }"
    fi

    if [[ ${build_rc} -eq 0 ]]; then
        local binary_path="${SDK_DIR}/${build_dir}/$(cfg_get chip_tool binary_name)"
        BUILD_STATUS["chip-tool"]="PASS"
        PASSED_APPS+=("chip-tool")
        ok "└─ chip-tool built successfully → ${binary_path}"
    else
        tail -50 "${tmp_log}" > "${err_log}" 2>/dev/null || true
        echo -e "
${RED}── Build error output ──────────────────────────────${NC}"
        grep -E "error:|FAILED:|fatal error:" "${tmp_log}" 2>/dev/null | tail -20 ||             tail -20 "${tmp_log}" 2>/dev/null
        echo -e "${RED}────────────────────────────────────────────────────${NC}
"
        local conclusion
        conclusion=$(diagnose_error "chip-tool" "${err_log}")
        BUILD_STATUS["chip-tool"]="FAIL"
        echo -e "${RED}[FAIL]${NC} └─ chip-tool build failed after ${elapsed_str}"
        echo -e "       ${conclusion}"
        fail "❌ chip-tool build failed — tests cannot run without chip-tool
   Error log: ${err_log}
   ${conclusion}"
    fi
}

# =============================================================================
# STEP 6 — Build Python controller (FATAL on failure — tests need it)
# =============================================================================
build_python_controller() {
    banner "Step 6 — Python Controller"

    if ! cfg_bool python_controller enabled; then
        warn "Python controller disabled — skipping."
        return
    fi

    local install_venv_name extra_args
    install_venv_name=$(cfg_get python_controller install_venv_name)
    extra_args=$(cfg_get python_controller extra_args)

    activate_env
    cd "${SDK_DIR}"

    local err_log="${BUILD_LOG_DIR}/python-controller_build_error.log"
    local tmp_log="${BUILD_LOG_DIR}/python-controller_build_full.log"
    local start_ts
    start_ts=$(date +%s)

    log "Building Python controller..."
    log "  command : cd ${SDK_DIR} && source scripts/activate.sh && scripts/build_python.sh -m platform -d true -i ${install_venv_name} ${extra_args}"
    scripts/build_python.sh             -m platform             -d true             -i "${install_venv_name}"             ${extra_args}             > "${tmp_log}" 2>&1 &
    local build_pid=$!

    local last_printed=0
    local last_step=""
    local step_num=0
    local step_total=0

    while kill -0 "${build_pid}" 2>/dev/null; do
        local latest
        latest=$(tail -1 "${tmp_log}" 2>/dev/null || echo "")
        if [[ "${latest}" =~ ^\[([[:space:]0-9]+)/([0-9]+)\] ]]; then
            step_num="${BASH_REMATCH[1]// /}"
            step_total="${BASH_REMATCH[2]}"
            if (( step_num - last_printed >= 50 )); then
                echo -e "  ${CYAN}[${step_num}/${step_total}]${NC} ${latest#*\] }"
                last_printed="${step_num}"
            fi
            last_step="${latest}"
        elif [[ "${latest}" == *"Installing"* || "${latest}" == *"Collecting"* ]]; then
            # Show pip install progress too
            echo -e "  ${CYAN}[pip]${NC} ${latest}"
        fi
        sleep 1
    done
    wait "${build_pid}"
    local build_rc=$?

    local elapsed=$(( $(date +%s) - start_ts ))
    local elapsed_str
    if (( elapsed >= 60 )); then elapsed_str="$((elapsed/60))m $((elapsed%60))s"
    else elapsed_str="${elapsed}s"; fi

    if [[ -n "${last_step}" ]]; then
        echo -e "  ${CYAN}[${step_num}/${step_total}]${NC} ${last_step#*\] }"
    fi

    if [[ ${build_rc} -eq 0 ]]; then
        BUILD_STATUS["python-controller"]="PASS"
        PASSED_APPS+=("python-controller")
        ok "└─ python-controller built successfully → ${SDK_DIR}/${install_venv_name}"
    else
        tail -50 "${tmp_log}" > "${err_log}" 2>/dev/null || true
        echo -e "
${RED}── Build error output ──────────────────────────────${NC}"
        grep -E "error:|FAILED:|fatal error:" "${tmp_log}" 2>/dev/null | tail -20 ||             tail -20 "${tmp_log}" 2>/dev/null
        echo -e "${RED}────────────────────────────────────────────────────${NC}
"
        local conclusion
        conclusion=$(diagnose_error "python-controller" "${err_log}")
        BUILD_STATUS["python-controller"]="FAIL"
        echo -e "${RED}[FAIL]${NC} └─ python-controller build failed after ${elapsed_str}"
        echo -e "       ${conclusion}"
        fail "❌ Python controller build failed — tests cannot run without it
   Error log: ${err_log}
   ${conclusion}"
    fi
}

# =============================================================================
# STEP 7 — Build summary with clear conclusions
# =============================================================================
print_summary() {
    banner "Build Summary"
    cd "${SDK_DIR}"

    echo -e "${BOLD}Build mode :${NC} ${BUILD_MODE}"
    echo -e "${BOLD}SDK commit :${NC} $(git rev-parse HEAD)"
    echo -e "${BOLD}SDK branch :${NC} $(git rev-parse --abbrev-ref HEAD)"
    echo ""

    local overall_ok=true

    echo -e "${BOLD}Reference Apps:${NC}"
    while IFS= read -r line; do
        local name build_dir binary_name bin_path
        name=$(echo "${line}"        | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['name'])")
        build_dir=$(echo "${line}"   | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['build_dir'])")
        binary_name=$(echo "${line}" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['binary_name'])")
        bin_path="${SDK_DIR}/${build_dir}/${binary_name}"

        if [[ -f "${bin_path}" ]]; then
            local size
            size=$(du -sh "${bin_path}" | cut -f1)
            echo -e "  ${GREEN}✔${NC}  ${name} (${size})"
        else
            echo -e "  ${RED}✘${NC}  ${name} — BUILD FAILED"
            if [[ -n "${BUILD_ERROR[${name}]:-}" ]]; then
                echo -e "       ${BUILD_ERROR[${name}]}"
            fi
            local err_log="${BUILD_LOG_DIR}/${name}_build_error.log"
            [[ -f "${err_log}" ]] && echo -e "       Error log: ${err_log}"
            overall_ok=false
        fi
    done < <(cfg_apps)
    echo ""

    if cfg_bool chip_tool enabled; then
        echo -e "${BOLD}chip-tool:${NC}"
        local ct_path="${SDK_DIR}/$(cfg_get chip_tool build_dir)/$(cfg_get chip_tool binary_name)"
        if [[ -f "${ct_path}" ]]; then
            local size; size=$(du -sh "${ct_path}" | cut -f1)
            echo -e "  ${GREEN}✔${NC}  chip-tool (${size})"
        else
            echo -e "  ${RED}✘${NC}  chip-tool — BUILD FAILED"
            overall_ok=false
        fi
        echo ""
    fi

    if cfg_bool python_controller enabled; then
        echo -e "${BOLD}Python Controller:${NC}"
        local venv_path="${SDK_DIR}/$(cfg_get python_controller install_venv_name)"
        if [[ -d "${venv_path}" ]]; then
            echo -e "  ${GREEN}✔${NC}  venv → ${venv_path}"
        else
            echo -e "  ${RED}✘${NC}  venv MISSING"
            overall_ok=false
        fi
        echo ""
    fi

    # Save build status JSON for test runner to read
    python3 - "${BUILD_LOG_DIR}" << 'PY'
import sys, json, os
log_dir = sys.argv[1]
status = {}
# Find all *_build_error.log files to determine failed apps
for f in os.listdir(log_dir):
    if f.endswith("_build_error.log"):
        app = f.replace("_build_error.log", "")
        status[app] = "FAIL"
out = os.path.join(log_dir, "build_status.json")
with open(out, "w") as f:
    json.dump(status, f, indent=2)
print(f"Build status saved: {out}")
PY

    if [[ "${overall_ok}" == "true" ]]; then
        echo -e "${GREEN}${BOLD}✔ All enabled targets built successfully!${NC}"
    else
        echo -e "${YELLOW}${BOLD}⚠ Some targets failed — check error logs above.${NC}"
        echo -e "${YELLOW}  Tests will run only for successfully built apps.${NC}"
        # Don't exit 1 here — let tests run for what did build
        # chip-tool and python-controller failures already caused exit above
    fi
}

# =============================================================================
# Main
# =============================================================================
main() {
    banner "Matter CI — Build Pipeline"
    log "Mode    : ${BUILD_MODE}"
    log "Config  : ${CONFIG_FILE}"
    log "SDK Dir : ${SDK_DIR}"
    log "Host    : $(hostname)  |  arch: $(uname -m)"
    log "Date    : $(date)"
    echo ""

    install_system_deps

    case "${BUILD_MODE}" in
        full)
            log "Mode: FULL — clone SDK, clean old builds, bootstrap, build all"
            sdk_clone
            clean_old_builds
            sdk_bootstrap
            ;;
        skip-clone)
            log "Mode: SKIP-CLONE — update existing SDK, clean old builds, bootstrap, build all"
            sdk_update
            clean_old_builds
            sdk_bootstrap
            ;;
        skip-all)
            log "Mode: SKIP-ALL — build only (no clone, no bootstrap, no clean)"
            warn "Only safe if rebuilding the exact same SDK commit."
            if [[ ! -d "${SDK_DIR}/.git" ]]; then
                fail "❌ SDK NOT FOUND at ${SDK_DIR}
   Fix: Run with --mode full first to clone the SDK
   Or check rpi.sdk_dir in build_config.yaml"
            fi
            ;;
    esac

    # Resolve the reference-app list from the SDK (needs activate.sh, so it
    # must come after bootstrap in full/skip-clone; in skip-all the SDK was
    # already bootstrapped by a previous run).
    discover_apps

    build_apps
    build_chip_tool
    build_python_controller
    print_summary
}

main "$@"
