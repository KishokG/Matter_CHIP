#!/usr/bin/env bash
# =============================================================================
# build.sh — Runs ON the Raspberry Pi
#
# Three build modes (set via --mode flag from GitHub Actions):
#
#   full        → clone SDK + bootstrap + clean old builds + build
#                 Use for: first time, branch change, SHA change
#
#   skip-clone  → git pull + bootstrap + clean old builds + build
#                 Use for: SDK already cloned, want latest TOT or new SHA
#
#   skip-all    → no clone, no bootstrap, no clean + build only
#                 Use for: rebuilding exact same commit (fastest)
#
# Manual equivalent flow (options full / skip-clone):
#   git clone / git pull
#   source scripts/bootstrap.sh
#   source scripts/activate.sh
#   rm -rf <old build dirs> .environment
#   scripts/examples/gn_build_example.sh ...
#   scripts/build_python.sh ...
# =============================================================================

set -eo pipefail  # no -u here — activate.sh has unbound var refs

# ── CLI args ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/config/build_config.yaml"
BUILD_MODE="full"       # full | skip-clone | skip-all

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)      CONFIG_FILE="$2"; shift 2 ;;
        --mode)        BUILD_MODE="$2"; shift 2 ;;
        # Legacy flag support
        --skip-sdk)    BUILD_MODE="skip-all"; shift ;;
        *)             echo "[WARN] Unknown arg: $1"; shift ;;
    esac
done

# Validate mode
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

cfg_apps() {
    python3 - "${CONFIG_FILE}" <<'PY'
import sys, yaml, json
cfg = yaml.safe_load(open(sys.argv[1]))
for a in cfg.get("apps", []):
    if a.get("enabled", False):
        print(json.dumps(a))
PY
}

cfg_bool() {
    [[ "$(cfg_get "$@")" =~ ^[Tt]rue$ ]]
}

# ── Read SDK config ───────────────────────────────────────────────────────────
SDK_REPO=$(cfg_get sdk repo)
SDK_BRANCH=$(cfg_get sdk branch)
SDK_SHA=$(cfg_get sdk sha)
SDK_DIR="${MATTER_SDK_DIR:-$(cfg_get rpi sdk_dir)}"
SDK_PLATFORM=$(cfg_get sdk platform)
SDK_PLATFORM="${SDK_PLATFORM:-linux}"       # default to linux if not set
SUBMODULE_JOBS=$(cfg_get sdk submodule_jobs)
SUBMODULE_JOBS="${SUBMODULE_JOBS:-4}"       # default to 4 parallel jobs

# =============================================================================
# STEP 1 — Clone SDK (full mode only)
# Manual equivalent: git clone <url> + git submodule update --init --recursive
# =============================================================================
sdk_clone() {
    banner "Step 1 — SDK Clone"
    log "Repo   : ${SDK_REPO}"
    log "Branch : ${SDK_BRANCH}"
    log "SHA    : ${SDK_SHA:-<HEAD of branch>}"
    log "Dir    : ${SDK_DIR}"

    if [[ -d "${SDK_DIR}/.git" ]]; then
        log "SDK directory already exists — removing for clean clone..."
        rm -rf "${SDK_DIR}"
    fi

    log "Cloning SDK (depth=1)..."
    git clone \
        --branch "${SDK_BRANCH}" \
        --depth 1 \
        "${SDK_REPO}" \
        "${SDK_DIR}"

    cd "${SDK_DIR}"

    if [[ -n "${SDK_SHA}" ]]; then
        log "Pinning to SHA: ${SDK_SHA}"
        git fetch --depth 1 origin "${SDK_SHA}" 2>/dev/null || true
        git checkout "${SDK_SHA}"
    fi

    log "Initialising submodules (platform: ${SDK_PLATFORM}, shallow, jobs: ${SUBMODULE_JOBS})..."
    python3 scripts/checkout_submodules.py --platform "${SDK_PLATFORM}" --shallow --recursive --jobs "${SUBMODULE_JOBS}"

    ok "SDK cloned → commit: $(git rev-parse --short HEAD)"
}

# =============================================================================
# STEP 1b — Update existing SDK (skip-clone mode)
# Manual equivalent: cd connectedhomeip && git pull (or git checkout <SHA>)
# =============================================================================
sdk_update() {
    banner "Step 1 — SDK Update (skip clone)"

    if [[ ! -d "${SDK_DIR}/.git" ]]; then
        fail "SDK not found at ${SDK_DIR} — cannot skip clone. Run with --mode full first."
    fi

    cd "${SDK_DIR}"
    log "Current commit: $(git rev-parse --short HEAD)"
    log "Current branch: $(git rev-parse --abbrev-ref HEAD)"

    if [[ -n "${SDK_SHA}" ]]; then
        # Checkout specific SHA
        log "Fetching and checking out SHA: ${SDK_SHA}"
        git fetch --depth 1 origin "${SDK_SHA}" 2>/dev/null || \
            git fetch origin
        git checkout "${SDK_SHA}"
    else
        # Pull latest TOT
        log "Pulling latest from branch: ${SDK_BRANCH}"
        git fetch origin "${SDK_BRANCH}"
        git checkout "${SDK_BRANCH}"
        git pull origin "${SDK_BRANCH}"
    fi

    log "Updating submodules (platform: ${SDK_PLATFORM}, shallow, jobs: ${SUBMODULE_JOBS})..."
    python3 scripts/checkout_submodules.py --platform "${SDK_PLATFORM}" --shallow --recursive --jobs "${SUBMODULE_JOBS}"

    ok "SDK updated → commit: $(git rev-parse --short HEAD)"
}

# =============================================================================
# STEP 2 — Bootstrap
# Manual equivalent: source scripts/bootstrap.sh
# Always run after clone or update — environment is commit-specific.
# =============================================================================
sdk_bootstrap() {
    banner "Step 2 — Bootstrap"
    cd "${SDK_DIR}"

    log "Running: source scripts/bootstrap.sh"
    log "This sets up pigweed, CIPD tools, and Python venv for this commit..."
    bash scripts/bootstrap.sh
    ok "Bootstrap complete."
}

# =============================================================================
# STEP 3 — Clean old build outputs
# Removes stale binaries and .environment from previous builds.
# Required when switching commits/branches so old artifacts don't linger.
# Manual equivalent: rm -rf <build_dirs> .environment
# =============================================================================
clean_old_builds() {
    banner "Step 3 — Clean Old Build Outputs"
    cd "${SDK_DIR}"

    # Clean .environment (regenerated by bootstrap)
    if [[ -d ".environment" ]]; then
        log "Removing .environment/ ..."
        rm -rf .environment
        ok "Removed .environment/"
    else
        log ".environment/ not found — skipping"
    fi

    # Clean all enabled app build dirs
    while IFS= read -r line; do
        local build_dir
        build_dir=$(echo "${line}" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['build_dir'])")
        if [[ -d "${build_dir}" ]]; then
            log "Removing app build dir: ${build_dir}"
            rm -rf "${build_dir}"
            ok "Removed ${build_dir}"
        fi
    done < <(cfg_apps)

    # Clean chip-tool build dir
    if cfg_bool chip_tool enabled; then
        local ct_build
        ct_build=$(cfg_get chip_tool build_dir)
        if [[ -d "${ct_build}" ]]; then
            log "Removing chip-tool build dir: ${ct_build}"
            rm -rf "${ct_build}"
            ok "Removed ${ct_build}"
        fi
    fi

    # Clean python controller venv
    if cfg_bool python_controller enabled; then
        local venv_name
        venv_name=$(cfg_get python_controller install_venv_name)
        if [[ -d "${venv_name}" ]]; then
            log "Removing python venv: ${venv_name}"
            rm -rf "${venv_name}"
            ok "Removed ${venv_name}"
        fi
    fi

    ok "Clean complete — ready for fresh build."
}

# =============================================================================
# Activate environment
# Manual equivalent: source scripts/activate.sh
# Must be called after bootstrap. Uses set +u to handle optional PW_* vars.
# =============================================================================
activate_env() {
    cd "${SDK_DIR}"
    log "Activating Matter environment (source scripts/activate.sh)..."

    # set +u: temporarily allow unbound variables.
    # activate.sh references optional PW_* vars without ${:-} guards,
    # which crashes under set -u in a non-interactive SSH shell.
    set +u
    # shellcheck disable=SC1091
    source scripts/activate.sh
    set -u

    ok "Environment activated. gn=$(command -v gn 2>/dev/null || echo NOT_FOUND)  ninja=$(command -v ninja 2>/dev/null || echo NOT_FOUND)"
}

# =============================================================================
# STEP 4 — Build reference apps
# Manual equivalent:
#   scripts/examples/gn_build_example.sh \
#       examples/all-clusters-app/linux \
#       examples/all-clusters-app/linux/out/all-clusters-app \
#       chip_inet_config_enable_ipv4=false
# =============================================================================
build_apps() {
    banner "Step 4 — Reference Apps"

    local app_lines
    mapfile -t app_lines < <(cfg_apps)

    if [[ ${#app_lines[@]} -eq 0 ]]; then
        warn "No reference apps enabled — skipping."
        return
    fi

    log "Building ${#app_lines[@]} enabled app(s)..."
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
        log "└─ Command  : scripts/examples/gn_build_example.sh ${source_dir} ${build_dir} ${extra_gn_args}"

        scripts/examples/gn_build_example.sh \
            "${source_dir}" \
            "${build_dir}" \
            ${extra_gn_args}

        ok "${name} built → ${SDK_DIR}/${build_dir}"
        echo ""
    done
}

# =============================================================================
# STEP 5 — Build chip-tool
# Manual equivalent:
#   scripts/examples/gn_build_example.sh \
#       examples/chip-tool out/chip-tool \
#       'chip_mdns="platform" chip_inet_config_enable_ipv4=false'
# =============================================================================
build_chip_tool() {
    banner "Step 5 — chip-tool"

    if ! cfg_bool chip_tool enabled; then
        warn "chip-tool disabled in config — skipping."
        return
    fi

    local source_dir build_dir extra_gn_args
    source_dir=$(cfg_get chip_tool source_dir)
    build_dir=$(cfg_get chip_tool build_dir)
    extra_gn_args=$(cfg_get chip_tool extra_gn_args)

    activate_env
    cd "${SDK_DIR}"

    log "Command: scripts/examples/gn_build_example.sh ${source_dir} ${build_dir} ${extra_gn_args}"

    scripts/examples/gn_build_example.sh \
        "${source_dir}" \
        "${build_dir}" \
        ${extra_gn_args}

    ok "chip-tool built → ${SDK_DIR}/${build_dir}/$(cfg_get chip_tool binary_name)"
}

# =============================================================================
# STEP 6 — Build Python controller
# Manual equivalent:
#   ./scripts/build_python.sh -m platform -d true -i python_env
# =============================================================================
build_python_controller() {
    banner "Step 6 — Python Controller"

    if ! cfg_bool python_controller enabled; then
        warn "Python controller disabled in config — skipping."
        return
    fi

    local install_venv_name extra_args
    install_venv_name=$(cfg_get python_controller install_venv_name)
    extra_args=$(cfg_get python_controller extra_args)

    activate_env
    cd "${SDK_DIR}"

    log "Command: scripts/build_python.sh -m platform -d true -i ${install_venv_name} ${extra_args}"

    scripts/build_python.sh \
        -m platform \
        -d true \
        -i "${install_venv_name}" \
        ${extra_args}

    ok "Python controller built → ${SDK_DIR}/${install_venv_name}"
}

# =============================================================================
# STEP 7 — Build summary
# =============================================================================
print_summary() {
    banner "Build Summary"
    cd "${SDK_DIR}"

    echo -e "${BOLD}Build mode :${NC} ${BUILD_MODE}"
    echo -e "${BOLD}SDK commit :${NC} $(git rev-parse HEAD)"
    echo -e "${BOLD}SDK branch :${NC} $(git rev-parse --abbrev-ref HEAD)"
    echo ""

    local all_ok=true

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
            echo -e "  ${GREEN}✔${NC}  ${name} (${size}) → ${bin_path}"
        else
            echo -e "  ${RED}✘${NC}  ${name} → MISSING: ${bin_path}"
            all_ok=false
        fi
    done < <(cfg_apps)
    echo ""

    if cfg_bool chip_tool enabled; then
        echo -e "${BOLD}chip-tool:${NC}"
        local ct_path="${SDK_DIR}/$(cfg_get chip_tool build_dir)/$(cfg_get chip_tool binary_name)"
        if [[ -f "${ct_path}" ]]; then
            local size; size=$(du -sh "${ct_path}" | cut -f1)
            echo -e "  ${GREEN}✔${NC}  chip-tool (${size}) → ${ct_path}"
        else
            echo -e "  ${RED}✘${NC}  chip-tool MISSING: ${ct_path}"
            all_ok=false
        fi
        echo ""
    fi

    if cfg_bool python_controller enabled; then
        echo -e "${BOLD}Python Controller:${NC}"
        local venv_path="${SDK_DIR}/$(cfg_get python_controller install_venv_name)"
        if [[ -d "${venv_path}" ]]; then
            echo -e "  ${GREEN}✔${NC}  venv → ${venv_path}"
        else
            echo -e "  ${RED}✘${NC}  venv MISSING: ${venv_path}"
            all_ok=false
        fi
        echo ""
    fi

    if [[ "${all_ok}" == "true" ]]; then
        echo -e "${GREEN}${BOLD}✔ All enabled targets built successfully!${NC}"
    else
        echo -e "${RED}${BOLD}✘ One or more targets missing — check logs above.${NC}"
        exit 1
    fi
}

# =============================================================================
# Main — orchestrate steps based on build mode
# =============================================================================
main() {
    banner "Matter CI — Build Pipeline"
    log "Mode    : ${BUILD_MODE}"
    log "Config  : ${CONFIG_FILE}"
    log "SDK Dir : ${SDK_DIR}"
    log "Host    : $(hostname)  |  arch: $(uname -m)"
    log "Date    : $(date)"
    echo ""

    case "${BUILD_MODE}" in

        full)
            # ── Full build: clone + bootstrap + clean + build ──────────────
            log "Mode: FULL — clone SDK, bootstrap, clean old builds, build all"
            sdk_clone
            sdk_bootstrap
            clean_old_builds
            ;;

        skip-clone)
            # ── Skip clone: pull/checkout + bootstrap + clean + build ──────
            log "Mode: SKIP-CLONE — update existing SDK, bootstrap, clean old builds, build all"
            sdk_update
            sdk_bootstrap
            clean_old_builds
            ;;

        skip-all)
            # ── Skip all: build only, no clone/bootstrap/clean ─────────────
            log "Mode: SKIP-ALL — build only (SDK and bootstrap unchanged)"
            warn "Skipping clone, bootstrap, and clean."
            warn "Only safe if rebuilding the exact same SDK commit."
            if [[ ! -d "${SDK_DIR}/.git" ]]; then
                fail "SDK not found at ${SDK_DIR}. Run with --mode full first."
            fi
            ;;

    esac

    build_apps
    build_chip_tool
    build_python_controller
    print_summary
}

main "$@"
