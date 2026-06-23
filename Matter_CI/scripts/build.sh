#!/usr/bin/env bash
# =============================================================================
# build.sh — Runs ON the Raspberry Pi
# Reads config/build_config.yaml and builds all enabled targets.
#
# Usage:
#   bash scripts/build.sh
#   bash scripts/build.sh --config /path/to/build_config.yaml
#   bash scripts/build.sh --skip-sdk      # skip clone/bootstrap, only build
# =============================================================================

set -euo pipefail

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

# ── YAML helpers (pure Python, no extra deps needed beyond python3-yaml) ──────
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
# STEP 1 — Clone / update SDK
# =============================================================================
sdk_setup() {
    banner "Step 1 — SDK Clone / Update"
    log "Repo   : ${SDK_REPO}"
    log "Branch : ${SDK_BRANCH}"
    log "SHA    : ${SDK_SHA:-<HEAD of branch>}"
    log "Dir    : ${SDK_DIR}"

    if [[ -d "${SDK_DIR}/.git" ]]; then
        log "Existing SDK found — fetching updates..."
        cd "${SDK_DIR}"
        git fetch origin "${SDK_BRANCH}" --depth 1
        git checkout "${SDK_BRANCH}"
        git reset --hard "origin/${SDK_BRANCH}"
    else
        log "Cloning SDK (shallow, depth=1)..."
        git clone \
            --branch "${SDK_BRANCH}" \
            --depth 1 \
            "${SDK_REPO}" \
            "${SDK_DIR}"
        cd "${SDK_DIR}"
    fi

    # Pin to a specific SHA if configured
    if [[ -n "${SDK_SHA}" ]]; then
        log "Pinning to SHA: ${SDK_SHA}"
        git fetch --depth 1 origin "${SDK_SHA}" 2>/dev/null || \
            git fetch origin 2>/dev/null
        git checkout "${SDK_SHA}"
    fi

    log "Initialising submodules..."
    git submodule update --init --depth 1 --recursive

    ok "SDK ready → commit: $(git rev-parse --short HEAD)"
}

# =============================================================================
# STEP 2 — Bootstrap
# Equivalent to: source scripts/bootstrap.sh
# Must be run once after a fresh clone.
# =============================================================================
sdk_bootstrap() {
    banner "Step 2 — Bootstrap"
    cd "${SDK_DIR}"

    if ! cfg_bool sdk bootstrap; then
        warn "Bootstrap disabled in config — skipping."
        return
    fi

    log "Running: source scripts/bootstrap.sh"
    # bootstrap.sh must be sourced so environment vars survive,
    # but since we are in a subprocess here we run it as bash
    # and re-source activate.sh before every build step.
    bash scripts/bootstrap.sh
    ok "Bootstrap complete."
}

# =============================================================================
# Activate Matter build environment
# Equivalent to: source scripts/activate.sh
# Must be called before any gn/ninja/build_python.sh invocation.
# =============================================================================
activate_env() {
    cd "${SDK_DIR}"
    log "Activating Matter environment..."

    local ENV_ACTIVATE="${SDK_DIR}/.environment/activate.sh"

    if [[ ! -f "${ENV_ACTIVATE}" ]]; then
        fail ".environment/activate.sh not found — run without --skip-sdk to bootstrap first."
    fi

    # Temporarily disable "unbound variable" errors just for sourcing activate.sh.
    # activate.sh references optional PW_* variables with bare ${VAR} syntax
    # (not ${VAR:-}) which causes set -u to abort in a non-interactive SSH shell.
    # We restore set -u immediately after sourcing.
    set +u
    source "${ENV_ACTIVATE}"
    set -u

    ok "Environment ready. gn=$(command -v gn 2>/dev/null || echo NOT_FOUND)  ninja=$(command -v ninja 2>/dev/null || echo NOT_FOUND)"
}

# =============================================================================
# STEP 3 — Build reference apps
#
# Real command (from SDK docs):
#   scripts/examples/gn_build_example.sh \
#       <source_dir> \
#       <build_dir> \
#       [extra_gn_args]
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

        log "┌─ Building: ${name}"
        log "│  source : ${source_dir}"
        log "│  output : ${build_dir}"
        log "│  gn_args: ${extra_gn_args:-<none>}"
        log "└─ Command: scripts/examples/gn_build_example.sh ${source_dir} ${build_dir} ${extra_gn_args}"

        # This is the exact SDK helper — mirrors what you run manually
        scripts/examples/gn_build_example.sh \
            "${source_dir}" \
            "${build_dir}" \
            ${extra_gn_args}   # intentionally unquoted so args split correctly

        ok "${name} built → ${SDK_DIR}/${build_dir}"
        echo ""
    done
}

# =============================================================================
# STEP 4 — Build chip-tool
#
# Real command:
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
        ${extra_gn_args}   # intentionally unquoted

    ok "chip-tool built → ${SDK_DIR}/${build_dir}/$(cfg_get chip_tool binary_name)"
}

# =============================================================================
# STEP 5 — Build Python controller
#
# Real command (two steps):
#   source scripts/activate.sh
#   scripts/build_python.sh -m platform -d true -i python_env
#
# -m platform  → use platform mDNS
# -d true      → enable debug/detail logs
# -i python_env → install into a venv named python_env inside the SDK
# =============================================================================
build_python_controller() {
    banner "Step 5 — Python Controller"

    if ! cfg_bool python_controller enabled; then
        warn "Python controller disabled in config — skipping."
        return
    fi

    local install_venv_name
    install_venv_name=$(cfg_get python_controller install_venv_name)

    # activate.sh MUST be sourced before build_python.sh
    activate_env
    cd "${SDK_DIR}"

    log "Command: scripts/build_python.sh -m platform -d true -i ${install_venv_name}"

    scripts/build_python.sh \
        -m platform \
        -d true \
        -i "${install_venv_name}"

    ok "Python controller built and installed into: ${SDK_DIR}/${install_venv_name}"
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

    # Reference apps
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

    # chip-tool
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

    # Python controller
    if cfg_bool python_controller enabled; then
        echo -e "${BOLD}Python Controller:${NC}"
        local venv_name
        venv_name=$(cfg_get python_controller install_venv_name)
        local venv_path="${SDK_DIR}/${venv_name}"
        if [[ -d "${venv_path}" ]]; then
            echo -e "  ${GREEN}✔${NC}  venv → ${venv_path}"
            # Show installed chip packages
            if [[ -f "${venv_path}/bin/python3" ]]; then
                local chip_pkg
                chip_pkg=$("${venv_path}/bin/python3" -c \
                    "import chip; print(chip.__version__)" 2>/dev/null || echo "version unknown")
                echo -e "  ${GREEN}✔${NC}  chip package version: ${chip_pkg}"
            fi
        else
            echo -e "  ${RED}✘${NC}  venv MISSING: ${venv_path}"
            all_ok=false
        fi
        echo ""
    fi

    if [[ "${all_ok}" == "true" ]]; then
        echo -e "${GREEN}${BOLD}✔ All enabled targets built successfully!${NC}"
    else
        echo -e "${RED}${BOLD}✘ One or more targets are missing — check logs above.${NC}"
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
        warn "--skip-sdk: skipping clone and bootstrap (using existing SDK)."
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
