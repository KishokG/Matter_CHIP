#!/usr/bin/env bash
# =============================================================================
# build_inside_container.sh — runs INSIDE the matter-sdk-builder container
# =============================================================================
# One nightly build, executed by the Mac mini runner via:
#   docker run --rm \
#     -v ~/matter-output:/output \
#     -v "$GITHUB_WORKSPACE/Matter_CI":/matter-ci:ro \
#     matter-sdk-builder:master \
#     bash /matter-ci/docker/build_inside_container.sh
#
# Flow (bootstrap is BAKED into the image — never re-run here):
#   1. git fetch + hard-reset the SDK to origin/<branch>  (latest SDK code)
#   2. sync submodules
#   3. source scripts/activate.sh
#   4. resolve enabled apps via discover_targets.py (same as build.sh)
#   5. build each app + chip-tool + python controller (live ninja progress,
#      per-app pass/fail + diagnose_error — ported from build.sh)
#   6. copy binaries + wheels + build-info.json + build_status.json to /output
#
# Reads (read-only): /matter-ci/config/build_config.yaml, /matter-ci/scripts/*
# Writes:            /output  (→ ~/matter-output on the Mac mini host)
#
# Resilient: one app failing does NOT stop others. chip-tool and the python
# controller ARE fatal (tests need them). Exit code reflects overall status.
# =============================================================================

set -o pipefail   # no -e / -u — activate.sh has unbound vars; app failures are non-fatal

# ── Paths (overridable via env for local testing) ───────────────────────────
MATTER_CI="${MATTER_CI:-/matter-ci}"
SDK_DIR="${SDK_DIR:-/connectedhomeip}"
OUTPUT="${OUTPUT:-/output}"
CONFIG_FILE="${CONFIG_FILE:-${MATTER_CI}/config/build_config.yaml}"
DISCOVER="${MATTER_CI}/scripts/discover_targets.py"
SUBMODULE_JOBS="${SUBMODULE_JOBS:-4}"

LOG_DIR="${OUTPUT}/build_logs"
DISCOVERED_APPS_JSON="${LOG_DIR}/discovered_apps.json"
mkdir -p "${LOG_DIR}" "${OUTPUT}/apps" "${OUTPUT}/wheels"

# ── Colours / logging ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${CYAN}[BUILD]${NC} $*"; }
ok()   { echo -e "${GREEN}[  OK ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL ]${NC} $*" >&2; exit 1; }
banner() {
    echo -e "\n${BOLD}${CYAN}══════════════════════════════════════${NC}"
    echo -e "${BOLD}${CYAN}  $*${NC}"
    echo -e "${BOLD}${CYAN}══════════════════════════════════════${NC}\n"
}

# ── Config helpers ───────────────────────────────────────────────────────────
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
cfg_bool() { [[ "$(cfg_get "$@")" =~ ^[Tt]rue$ ]]; }

# ── Build result tracking ────────────────────────────────────────────────────
declare -A BUILD_STATUS
declare -A BUILD_ERROR
PASSED_APPS=(); FAILED_APPS=()

# ── Error conclusion helper (ported verbatim from build.sh) ──────────────────
diagnose_error() {
    local app_name="$1" error_log="$2" conclusion=""
    if [[ ! -f "${error_log}" ]]; then
        echo "Build log not found — build may have crashed before starting"; return
    fi
    local content; content=$(cat "${error_log}" 2>/dev/null || echo "")
    if echo "${content}" | grep -q "No such file or directory.*source_dir\|does not exist\|source directory"; then
        conclusion="❌ WRONG SOURCE PATH — source_dir for '${app_name}' does not exist in the SDK."
    elif echo "${content}" | grep -q "fatal error:.*No such file or directory"; then
        local mh; mh=$(echo "${content}" | grep "fatal error:" | grep "No such file" | head -1 | sed "s/.*fatal error: //")
        conclusion="❌ MISSING HEADER FILE — ${mh}\n   Fix: add the system package to apt-packages.txt and rebuild the image"
    elif echo "${content}" | grep -q "Package .* was not found in the pkg-config\|No package .* found"; then
        local pkg; pkg=$(echo "${content}" | grep -oiE "Package '[^']+'|package [^ ]+ was not found" | head -1)
        conclusion="❌ MISSING PKG-CONFIG LIB — ${pkg}\n   Fix: add the -dev package to apt-packages.txt and rebuild the image"
    elif echo "${content}" | grep -q "error:.*command not found\|gn: not found\|ninja: not found"; then
        conclusion="❌ TOOL NOT FOUND — gn/ninja not in PATH.\n   Fix: the image bootstrap may be stale — rebuild the image"
    elif echo "${content}" | grep -q "out of memory\|OOM\|std::bad_alloc"; then
        conclusion="❌ OUT OF MEMORY — build ran out of RAM."
    elif echo "${content}" | grep -q "error:.*undeclared\|error:.*undefined reference"; then
        conclusion="❌ COMPILATION ERROR — likely an SDK version mismatch.\n   Fix: check sdk.branch or rebuild the image against the new SDK"
    elif echo "${content}" | grep -q "subcommand failed\|ninja: build stopped"; then
        conclusion="❌ NINJA BUILD FAILED — one or more compile units failed. See the error log."
    else
        conclusion="❌ BUILD FAILED — unknown error. Check the full log."
    fi
    echo -e "${conclusion}"
}

# ── Generic build runner (live ninja progress + diagnose) ────────────────────
# Usage: do_build <name> <fatal:0|1> -- <command> [args...]
do_build() {
    local name="$1" fatal="$2"; shift 2
    [[ "$1" == "--" ]] && shift

    local tmp_log="${LOG_DIR}/${name}_build_full.log"
    local err_log="${LOG_DIR}/${name}_build_error.log"
    local start_ts; start_ts=$(date +%s)

    "$@" > "${tmp_log}" 2>&1 &
    local pid=$!

    local last_printed=0 step_num=0 step_total=0 last_step=""
    while kill -0 "${pid}" 2>/dev/null; do
        local latest; latest=$(tail -1 "${tmp_log}" 2>/dev/null || echo "")
        if [[ "${latest}" =~ ^\[([[:space:]0-9]+)/([0-9]+)\] ]]; then
            step_num="${BASH_REMATCH[1]// /}"; step_total="${BASH_REMATCH[2]}"
            if (( step_num - last_printed >= 50 )) || [[ "${latest}" == *"FAILED:"* ]]; then
                echo -e "  ${CYAN}[${step_num}/${step_total}]${NC} ${latest#*\] }"
                last_printed="${step_num}"
            fi
            last_step="${latest}"
        elif [[ "${latest}" == *"Installing"* || "${latest}" == *"Collecting"* ]]; then
            echo -e "  ${CYAN}[pip]${NC} ${latest}"
        elif [[ "${latest}" == *"FAILED:"* ]]; then
            echo -e "  ${RED}[FAIL]${NC} ${latest}"
        fi
        sleep 1
    done

    local rc; if wait "${pid}"; then rc=0; else rc=$?; fi

    [[ -n "${last_step}" ]] && echo -e "  ${CYAN}[${step_num}/${step_total}]${NC} ${last_step#*\] }"
    local elapsed=$(( $(date +%s) - start_ts )) elapsed_str
    if (( elapsed >= 60 )); then elapsed_str="$((elapsed/60))m $((elapsed%60))s"; else elapsed_str="${elapsed}s"; fi

    if (( rc == 0 )); then
        BUILD_STATUS["${name}"]="PASS"; PASSED_APPS+=("${name}")
        ok "└─ ${name} built successfully (${elapsed_str})"
    else
        tail -50 "${tmp_log}" > "${err_log}" 2>/dev/null || true
        echo -e "\n${RED}── Build error output ──────────────────────────────${NC}"
        grep -E "error:|FAILED:|fatal error:" "${tmp_log}" 2>/dev/null | tail -20 || tail -20 "${tmp_log}" 2>/dev/null
        echo -e "${RED}────────────────────────────────────────────────────${NC}\n"
        local conclusion; conclusion=$(diagnose_error "${name}" "${err_log}")
        BUILD_STATUS["${name}"]="FAIL"; BUILD_ERROR["${name}"]="${conclusion}"; FAILED_APPS+=("${name}")
        echo -e "${RED}[FAIL]${NC} └─ ${name} build failed after ${elapsed_str}"
        echo -e "       ${conclusion}"
        echo -e "       Full log : ${tmp_log}"
        if (( fatal == 1 )); then
            fail "❌ ${name} build failed — required for tests, cannot continue.
   Error log: ${err_log}
   ${conclusion}"
        fi
    fi
    return 0   # never abort the caller; fatal handled above
}

# =============================================================================
# STEP 1 — Update SDK to latest (no clone, no bootstrap — those are baked)
# =============================================================================
update_sdk() {
    banner "Step 1 — Update SDK (git pull)"
    [[ -d "${SDK_DIR}/.git" ]] || fail "❌ SDK not found at ${SDK_DIR} — is the image built correctly?"

    local branch; branch=$(cfg_get sdk branch); branch="${branch:-master}"
    local sha;    sha=$(cfg_get sdk sha)
    local image_branch=""; [[ -f /image-sdk-branch ]] && image_branch=$(cat /image-sdk-branch)

    log "SDK dir      : ${SDK_DIR}"
    log "Config branch: ${branch}"
    [[ -n "${image_branch}" ]] && log "Image branch : ${image_branch}"
    if [[ -n "${image_branch}" && "${image_branch}" != "${branch}" ]]; then
        warn "Config branch (${branch}) differs from image branch (${image_branch})."
        warn "The image was bootstrapped for '${image_branch}'; switching branches may need an image rebuild."
    fi

    cd "${SDK_DIR}"
    log "Current commit: $(git rev-parse --short HEAD 2>/dev/null || echo '?')"
    git fetch --tags origin "${branch}" || fail "❌ git fetch failed for branch '${branch}'"

    if [[ -n "${sha}" ]]; then
        log "Pinning to SHA: ${sha}"
        git checkout -f "${sha}" || fail "❌ Could not checkout SHA '${sha}'"
    else
        # Hard-reset to the remote branch tip (out/ and .environment are
        # git-ignored, so they survive; guarantees a clean, latest tree).
        git checkout -f "${branch}" 2>/dev/null || git checkout -B "${branch}" "origin/${branch}"
        git reset --hard "origin/${branch}" || fail "❌ Could not reset to origin/${branch}"
    fi
    ok "SDK now at: $(git rev-parse --short HEAD)  ($(git rev-parse --abbrev-ref HEAD 2>/dev/null))"

    log "Syncing submodules (platform: linux)..."
    for attempt in 1 2 3; do
        python3 scripts/checkout_submodules.py \
            --platform linux --shallow --recursive \
            --jobs "${SUBMODULE_JOBS}" --allow-changing-global-git-config \
            && break || { warn "submodule checkout attempt ${attempt}/3 failed — retrying in 5s"; sleep 5; }
    done
    ok "Submodules synced."
}

# =============================================================================
# STEP 2 — Activate the (baked) build environment
# =============================================================================
activate_env() {
    banner "Step 2 — Activate Environment"
    cd "${SDK_DIR}"
    [[ -f scripts/activate.sh ]] || fail "❌ scripts/activate.sh missing — image bootstrap incomplete."
    set +u; source scripts/activate.sh   # activate.sh references unbound vars
    command -v gn    >/dev/null || fail "❌ gn not found — image bootstrap is stale; rebuild the image."
    command -v ninja >/dev/null || fail "❌ ninja not found — image bootstrap is stale; rebuild the image."
    ok "Environment active. gn=$(command -v gn)  ninja=$(command -v ninja)"
}

# =============================================================================
# STEP 3 — Resolve enabled apps (dynamic discovery, same as build.sh)
# =============================================================================
discover_apps() {
    banner "Step 3 — Discover Reference Apps"
    [[ -f "${DISCOVER}" ]] || fail "❌ discover_targets.py not found at ${DISCOVER} (is /matter-ci mounted?)"

    log "Resolving apps from SDK via discover_targets.py..."
    if ! python3 "${DISCOVER}" --sdk-dir "${SDK_DIR}" --config "${CONFIG_FILE}" \
            --emit-apps-json > "${DISCOVERED_APPS_JSON}" 2> "${LOG_DIR}/discover.log"; then
        cat "${LOG_DIR}/discover.log" >&2
        fail "❌ App discovery failed — see log above. Log: ${LOG_DIR}/discover.log"
    fi
    local n; n=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))))" "${DISCOVERED_APPS_JSON}" 2>/dev/null || echo 0)
    if [[ "${n}" -eq 0 ]]; then
        cat "${LOG_DIR}/discover.log" >&2
        fail "❌ App discovery returned 0 apps — check discovery.apps enabled flags in build_config.yaml."
    fi
    grep -E "^\[(INFO|WARN)\]" "${LOG_DIR}/discover.log" >&2 || true
    ok "Discovered ${n} reference app(s)."
    python3 - "${DISCOVERED_APPS_JSON}" <<'PY'
import json, sys
for a in json.load(open(sys.argv[1])):
    print(f"   • {a['name']:24} {a['source_dir']:44} -> {a['binary_name']}")
PY
}

# Emit TSV (name, source_dir, build_dir, binary_name, extra_gn_args) per enabled app.
apps_tsv() {
    python3 - "${DISCOVERED_APPS_JSON}" <<'PY'
import json, sys
for a in json.load(open(sys.argv[1])):
    if a.get("enabled", True):
        print("\t".join([a["name"], a["source_dir"], a["build_dir"], a["binary_name"], a.get("extra_gn_args", "")]))
PY
}

# =============================================================================
# STEP 4 — Build reference apps
# =============================================================================
build_apps() {
    banner "Step 4 — Reference Apps"
    cd "${SDK_DIR}"
    local count=0
    while IFS=$'\t' read -r name src bdir bin gnargs; do
        [[ -z "${name}" ]] && continue
        count=$((count+1))
        log "┌─ Building : ${name}"
        log "│  source   : ${src}"
        log "│  output   : ${bdir}"
        log "│  gn_args  : ${gnargs:-<none>}"
        log "│  command  : cd ${SDK_DIR} && scripts/examples/gn_build_example.sh ${src} ${bdir} ${gnargs}"
        if [[ ! -d "${src}" ]]; then
            echo "Source directory not found: ${SDK_DIR}/${src}" > "${LOG_DIR}/${name}_build_error.log"
            BUILD_STATUS["${name}"]="FAIL"
            BUILD_ERROR["${name}"]="❌ WRONG SOURCE PATH — '${src}' does not exist in SDK"
            FAILED_APPS+=("${name}")
            echo -e "${RED}[FAIL]${NC} ${name} — source directory not found: ${src}"
            continue
        fi
        # shellcheck disable=SC2086 — gnargs must word-split into separate gn args
        do_build "${name}" 0 -- scripts/examples/gn_build_example.sh "${src}" "${bdir}" ${gnargs}
        echo ""
    done < <(apps_tsv)
    (( count == 0 )) && warn "No reference apps enabled — nothing built."
}

# =============================================================================
# STEP 5 — chip-tool (fatal)
# =============================================================================
build_chip_tool() {
    banner "Step 5 — chip-tool"
    if ! cfg_bool chip_tool enabled; then warn "chip-tool disabled — skipping."; return; fi
    cd "${SDK_DIR}"
    local src bdir bin gnargs
    src=$(cfg_get chip_tool source_dir); bdir=$(cfg_get chip_tool build_dir)
    bin=$(cfg_get chip_tool binary_name); gnargs=$(cfg_get chip_tool extra_gn_args)
    [[ -d "${src}" ]] || fail "❌ chip-tool source_dir '${src}' does not exist in SDK"
    log "command : cd ${SDK_DIR} && scripts/examples/gn_build_example.sh ${src} ${bdir} ${gnargs}"
    # shellcheck disable=SC2086
    do_build "chip-tool" 1 -- scripts/examples/gn_build_example.sh "${src}" "${bdir}" ${gnargs}
}

# =============================================================================
# STEP 6 — Python controller (fatal)
# =============================================================================
build_python_controller() {
    banner "Step 6 — Python Controller"
    if ! cfg_bool python_controller enabled; then warn "python controller disabled — skipping."; return; fi
    cd "${SDK_DIR}"
    local venv extra
    venv=$(cfg_get python_controller install_venv_name); venv="${venv:-python_env}"
    extra=$(cfg_get python_controller extra_args)
    # Remove any stale venv / python_lib so the build is fresh + complete.
    [[ -d "${venv}" ]] && rm -rf "${venv}"
    [[ -d out/python_lib ]] && rm -rf out/python_lib
    log "command : cd ${SDK_DIR} && source scripts/activate.sh && scripts/build_python.sh -m platform -d true -i ${venv} ${extra}"
    # shellcheck disable=SC2086
    do_build "python-controller" 1 -- scripts/build_python.sh -m platform -d true -i "${venv}" ${extra}
}

# =============================================================================
# STEP 7 — Copy outputs to /output + write manifests
# =============================================================================
collect_output() {
    banner "Step 7 — Collect Output → ${OUTPUT}"
    cd "${SDK_DIR}"

    # 7a. Reference app binaries
    local copied=0
    while IFS=$'\t' read -r name src bdir bin gnargs; do
        [[ -z "${name}" ]] && continue
        local path="${SDK_DIR}/${bdir}/${bin}"
        if [[ -f "${path}" ]]; then
            cp -f "${path}" "${OUTPUT}/apps/${bin}"
            local sz; sz=$(du -h "${path}" | cut -f1)
            ok "app  ${bin} (${sz})"; copied=$((copied+1))
        else
            warn "app  ${bin} not found at ${path} — skipping (build likely failed)"
        fi
    done < <(apps_tsv)
    log "Copied ${copied} app binary(ies) → ${OUTPUT}/apps/"

    # 7b. chip-tool
    if cfg_bool chip_tool enabled; then
        local ct="${SDK_DIR}/$(cfg_get chip_tool build_dir)/$(cfg_get chip_tool binary_name)"
        if [[ -f "${ct}" ]]; then cp -f "${ct}" "${OUTPUT}/chip-tool"; ok "chip-tool copied"; \
        else warn "chip-tool not found at ${ct}"; fi
    fi

    # 7c. Python wheels (same locations upload_artifacts.py expects)
    local wheel_dirs=(
        "out/python_lib/obj/src/controller/python/matter-controller-wheels"
        "out/python_lib/obj/src/python_testing/matter_testing_infrastructure/matter-testing._build_wheel"
        "out/python_lib/obj/scripts/matter_yamltests_distribution._build_wheel"
    )
    local wcount=0
    for d in "${wheel_dirs[@]}"; do
        [[ -d "${d}" ]] || continue
        while IFS= read -r whl; do
            cp -f "${whl}" "${OUTPUT}/wheels/"; wcount=$((wcount+1))
        done < <(find "${d}" -maxdepth 1 -name '*.whl' 2>/dev/null)
    done
    log "Copied ${wcount} wheel(s) → ${OUTPUT}/wheels/"

    # 7d. build-info.json (branch/commit/date) — host upload reads this since
    #     the SDK is not available on the Mac mini host.
    local commit branch date
    commit=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
    date=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    python3 - "$commit" "$branch" "$date" "${OUTPUT}/build-info.json" <<'PY'
import json, sys
commit, branch, date, out = sys.argv[1:5]
json.dump({"commit": commit, "commit_short": commit[:9], "branch": branch,
           "date": date, "arch": "arm64", "platform": "linux"},
          open(out, "w"), indent=2)
PY
    ok "Wrote build-info.json (commit ${commit:0:9}, branch ${branch})"

    # 7e. build_status.json — per-target PASS/FAIL (used by summary/notify)
    {
        echo "{"
        local first=1
        for k in "${!BUILD_STATUS[@]}"; do
            [[ ${first} -eq 0 ]] && echo ","
            printf '  "%s": "%s"' "${k}" "${BUILD_STATUS[$k]}"
            first=0
        done
        echo ""
        echo "}"
    } > "${OUTPUT}/build_status.json"
    cp -f "${DISCOVERED_APPS_JSON}" "${OUTPUT}/discovered_apps.json" 2>/dev/null || true
}

# =============================================================================
# Summary
# =============================================================================
print_summary() {
    banner "Build Summary"
    echo -e "${BOLD}SDK commit:${NC} $(cd "${SDK_DIR}" && git rev-parse --short HEAD 2>/dev/null)"
    echo -e "${BOLD}Passed (${#PASSED_APPS[@]}):${NC} ${PASSED_APPS[*]:-none}"
    if (( ${#FAILED_APPS[@]} > 0 )); then
        echo -e "${RED}${BOLD}Failed (${#FAILED_APPS[@]}):${NC} ${FAILED_APPS[*]}"
        for a in "${FAILED_APPS[@]}"; do echo -e "  ${RED}✘${NC} ${a} — ${BUILD_ERROR[$a]:-}"; done
    fi
    echo -e "\nOutput bundle staged at: ${OUTPUT} (→ ~/matter-output on the host)"
}

# =============================================================================
main() {
    banner "Matter CI — Docker Build (inside container)"
    log "Host   : $(hostname)  |  arch: $(uname -m)"
    log "Date   : $(date -u)"
    log "Config : ${CONFIG_FILE}"
    log "SDK    : ${SDK_DIR}"
    log "Output : ${OUTPUT}"

    [[ -f "${CONFIG_FILE}" ]] || fail "❌ build_config.yaml not found at ${CONFIG_FILE} (is /matter-ci mounted?)"

    update_sdk
    activate_env
    discover_apps
    build_apps
    build_chip_tool
    build_python_controller
    collect_output
    print_summary

    ok "Container build finished."
}
main "$@"
