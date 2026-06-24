#!/usr/bin/env bash
# =============================================================================
# build.sh — Runs ON the Raspberry Pi
# Mirrors the exact manual build process:
#
# Fresh clone:
#   git clone + submodules
#   source scripts/bootstrap.sh
#   source scripts/activate.sh
#   gn_build_example.sh ...
#
# Existing SDK (--skip-sdk):
#   git pull
#   source scripts/bootstrap.sh
#   source scripts/activate.sh
#   gn_build_example.sh ...
# =============================================================================

set -eo pipefail   # -e: exit on error  NOTE: no -u, activate.sh uses unbound vars

# ── CLI args ──────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/config/build_config.yaml"
SKIP_SDK=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)    CONFIG_FILE="$2"; shift 2 ;;
        --skip-sdk)  SKIP_SDK=true; shift ;;
        *)           echo "[WARN] Unknown arg: $1"; shift ;;
    esac
done

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

# =============================================================================
# STEP 1 — Clone or update SDK
# Manual equivalent:
#   git clone https://github.com/project-chip/connectedhomeip.git
#   git submodule update --init --recursive
# =============================================================================
sdk_setup() {
    banner "Step 1 — SDK Clone / Update"
    log "Repo   : ${SDK_REPO}"
    log "Branch : ${SDK_BRANCH}"
    log "SHA    : ${SDK_SHA:-<HEAD of branch>}"
    log "Dir    : ${SDK_DIR}"

    if [[ -d "${SDK_DIR}/.git" ]]; then
        log "Existing SDK found — pulling latest..."
        cd "${SDK_DIR}"
        # Manual equivalent: git pull
        git fetch origin "${SDK_BRANCH}"
        git checkout "${SDK_BRANCH}"
        git pull origin "${SDK_BRANCH}"
    else
        log "Cloning SDK..."
        # Manual equivalent: git clone <url>
        git clone \
            --branch "${SDK_BRANCH}" \
            --depth 1 \
            "${SDK_REPO}" \
            "${SDK_DIR}"
        cd "${SDK_DIR}"
    fi

    # Pin to specific SHA if configured
    if [[ -n "${SDK_SHA}" ]]; then
        log "Pinning to SHA: ${SDK_SHA}"
        git checkout "${SDK_SHA}"
    fi

    # Manual equivalent: git submodule update --init --recursive
    log "Initialising submodules..."
    git submodule update --init --depth 1 --recursive

    ok "SDK ready → commit: $(git rev-parse --short HEAD)"
}

# =============================================================================
# STEP 2 — Bootstrap
# Manual equivalent: source scripts/bootstrap.sh
# =============================================================================
sdk_bootstrap() {
    banner "Step 2 — Bootstrap"
    cd "${SDK_DIR}"

    if ! cfg_bool sdk bootstrap; then
        warn "Bootstrap disabled in config — skipping."
        return
    fi

    log "Running: source scripts/bootstrap.sh"
    # Note: We run with bash (not source) because we are already in a subshell.
    # The environment vars set by bootstrap are captured by the subsequent
    # source of scripts/activate.sh which re-reads them from .environment/
    bash scripts/bootstrap.sh
    ok "Bootstrap complete."
}

# =============================================================================
# Activate environment
# Manual equivalent: source scripts/activate.sh
#
# KEY POINT: scripts/activate.sh requires PW_* vars that bootstrap sets.
# In a fresh SSH shell these don't exist, so we:
#   1. Disable set -u temporarily (activate.sh has unbound var refs)
#   2. Source scripts/activate.sh directly — same as manual process
# =============================================================================
activate_env() {
    cd "${SDK_DIR}"
    log "Activating Matter environment (source scripts/activate.sh)..."

    # Disable unbound-variable errors for the duration of sourcing.
    # activate.sh references optional PW_* vars without ${:-} guards,
    # which crashes under set -u in a non-interactive SSH shell.
    # This is safe — we re-enable strict mode right after.
    set +u
    # shellcheck disable=SC1091
    source scripts/activate.sh
    set -u

    ok "Environment activated. gn=$(command -v gn 2>/dev/null || echo NOT_FOUND)  ninja=$(command -v ninja 2>/dev/null || echo NOT_FOUND)"
}

# =============================================================================
# STEP 3 — Build reference apps
# Manual equivalent:
#   scripts/examples/gn_build_example.sh \
#       examples/all-clusters-app/linux \
#       examples/all-clusters-app/linux/out/all-clusters-app \
#       chip_inet_config_enable_ipv4=false
# =============================================================================
build_apps() {
    banner "Step 3 — Reference Apps"

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
# STEP 4 — Build chip-tool
# Manual equivalent:
#   scripts/examples/gn_build_example.sh \
#       examples/chip-tool \
#       out/chip-tool \
#       'chip_mdns="platform" chip_inet_config_enable_ipv4=false'
# =============================================================================
build_chip_tool() {
    banner "Step 4 — chip-tool"

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
# STEP 5 — Build Python controller
# Manual equivalent:
#   ./scripts/build_python.sh -m platform -d true -i python_env
# =============================================================================
build_python_controller() {
    banner "Step 5 — Python Controller"

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
# STEP 6 — Build summary
# =============================================================================
print_summary() {
    banner "Build Summary"
    cd "${SDK_DIR}"

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
            local size
            size=$(du -sh "${ct_path}" | cut -f1)
            echo -e "  ${GREEN}✔${NC}  chip-tool (${size}) → ${ct_path}"
        else
            echo -e "  ${RED}✘${NC}  chip-tool MISSING: ${ct_path}"
            all_ok=false
        fi
        echo ""
    fi

    if cfg_bool python_controller enabled; then
        echo -e "${BOLD}Python Controller:${NC}"
        local venv_name venv_path
        venv_name=$(cfg_get python_controller install_venv_name)
        venv_path="${SDK_DIR}/${venv_name}"
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
# Main
# =============================================================================
main() {
    banner "Matter CI — Build Pipeline"
    log "Config  : ${CONFIG_FILE}"
    log "SDK Dir : ${SDK_DIR}"
    log "Host    : $(hostname)  |  arch: $(uname -m)"
    log "Date    : $(date)"
    echo ""

    if [[ "${SKIP_SDK}" == "true" ]]; then
        warn "--skip-sdk: skipping clone and bootstrap."
        warn "Using existing SDK at: ${SDK_DIR}"
    else
        sdk_setup
        sdk_bootstrap
    fi

    build_apps
    build_chip_tool
    build_python_controller
    print_summary
}

main "$@"
