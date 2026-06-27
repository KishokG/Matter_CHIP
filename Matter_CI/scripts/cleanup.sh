#!/usr/bin/env bash
# =============================================================================
# cleanup.sh — RPi Storage Monitor & Log Cleanup
# =============================================================================
# Runs as part of CI pipeline (before build) to:
#   1. Report current disk usage
#   2. Clean old test result runs (keep last N)
#   3. Clean leftover bundle tar.gz files
#   4. Clean /tmp/matter_testing/ mobly logs
#   5. Warn if disk space is critically low
#
# Usage:
#   bash Matter_CI/scripts/cleanup.sh --config Matter_CI/config/build_config.yaml
#   bash Matter_CI/scripts/cleanup.sh --dry-run   (show what would be deleted)
# =============================================================================
set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

log()  { echo -e "${CYAN}[CLEAN]${NC} $*"; }
ok()   { echo -e "${GREEN}[  OK ]${NC} $*"; }
warn() { echo -e "${YELLOW}[ WARN]${NC} $*"; }
fail() { echo -e "${RED}[FAIL ]${NC} $*"; }

DRY_RUN=false
KEEP_RUNS=10          # keep last N test result runs
KEEP_BUILD_LOGS=5     # keep last N build log sets
MIN_FREE_GB=10        # warn if free space below this
CRITICAL_FREE_GB=5    # error if free space below this

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        --keep-runs) KEEP_RUNS="$2"; shift 2 ;;
        --min-free-gb) MIN_FREE_GB="$2"; shift 2 ;;
        *) shift ;;
    esac
done

[[ "${DRY_RUN}" == "true" ]] && log "DRY RUN MODE — nothing will be deleted"

# =============================================================================
# Helper: safe delete
# =============================================================================
safe_rm() {
    local path="$1"
    local desc="$2"
    if [[ -e "${path}" ]]; then
        local size
        size=$(du -sh "${path}" 2>/dev/null | cut -f1 || echo "?")
        if [[ "${DRY_RUN}" == "true" ]]; then
            log "  [DRY] Would delete: ${path} (${size})"
        else
            rm -rf "${path}"
            ok "  Deleted: ${desc} (freed ~${size})"
        fi
    fi
}

# =============================================================================
# Step 1 — Disk usage report
# =============================================================================
echo ""
echo "════════════════════════════════════════════════════════"
echo "  Matter CI — Storage Monitor"
echo "  $(date '+%Y-%m-%d %H:%M:%S IST')"
echo "════════════════════════════════════════════════════════"

# Overall disk usage
DISK_TOTAL=$(df -BG / | awk 'NR==2{print $2}' | tr -d 'G')
DISK_USED=$(df -BG / | awk 'NR==2{print $3}' | tr -d 'G')
DISK_FREE=$(df -BG / | awk 'NR==2{print $4}' | tr -d 'G')
DISK_PCT=$(df / | awk 'NR==2{print $5}' | tr -d '%')

echo ""
log "Disk usage: ${DISK_USED}GB used / ${DISK_TOTAL}GB total / ${DISK_FREE}GB free (${DISK_PCT}% used)"

# Per-directory breakdown
HOME_DIR="${HOME}"
SDK_DIR="${HOME_DIR}/connectedhomeip"
RESULTS_DIR="${HOME_DIR}/matter-ci-results"
CI_LOGS_DIR="${HOME_DIR}/Matter_CHIP/Matter_CI/logs"

echo ""
log "Directory breakdown:"
for dir in \
    "${SDK_DIR}/out" \
    "${RESULTS_DIR}" \
    "${CI_LOGS_DIR}" \
    "/tmp/matter_testing"; do
    if [[ -d "${dir}" ]]; then
        size=$(du -sh "${dir}" 2>/dev/null | cut -f1 || echo "?")
        printf "  %-55s %s\n" "${dir}" "${size}"
    fi
done

# =============================================================================
# Step 2 — Check free space thresholds
# =============================================================================
echo ""
if (( DISK_FREE < CRITICAL_FREE_GB )); then
    fail "CRITICAL: Only ${DISK_FREE}GB free! Pipeline may fail."
    fail "Immediate action required — manual cleanup needed."
    # Don't exit — let cleanup run first
elif (( DISK_FREE < MIN_FREE_GB )); then
    warn "Low disk space: ${DISK_FREE}GB free (threshold: ${MIN_FREE_GB}GB)"
    warn "Cleanup will run now to free space."
else
    ok "Disk space OK: ${DISK_FREE}GB free"
fi

# =============================================================================
# Step 3 — Clean old test result runs
# =============================================================================
echo ""
log "Cleaning old test result runs (keeping last ${KEEP_RUNS})..."

if [[ -d "${RESULTS_DIR}" ]]; then
    # List runs sorted by number, keep last N
    mapfile -t all_runs < <(ls -d "${RESULTS_DIR}"/run-* 2>/dev/null | \
        sort -t- -k2 -n)
    total_runs=${#all_runs[@]}

    if (( total_runs > KEEP_RUNS )); then
        to_delete=$(( total_runs - KEEP_RUNS ))
        log "  Found ${total_runs} runs — deleting ${to_delete} oldest..."
        for run in "${all_runs[@]:0:${to_delete}}"; do
            safe_rm "${run}" "$(basename ${run})"
        done
    else
        ok "  ${total_runs} runs present — under limit of ${KEEP_RUNS}, nothing to delete"
    fi
else
    log "  No results dir found: ${RESULTS_DIR}"
fi

# =============================================================================
# Step 4 — Clean leftover bundle tar.gz
# =============================================================================
echo ""
log "Cleaning leftover bundle archives..."

BUNDLE_DIR="${CI_LOGS_DIR}/bundle"
if [[ -d "${BUNDLE_DIR}" ]]; then
    safe_rm "${BUNDLE_DIR}" "bundle staging dir"
fi

# Also clean any .tar.gz directly in logs/
for tgz in "${CI_LOGS_DIR}"/matter-sdk-*.tar.gz; do
    [[ -f "${tgz}" ]] && safe_rm "${tgz}" "$(basename ${tgz})"
done

# =============================================================================
# Step 5 — Clean /tmp/matter_testing (mobly logs)
# =============================================================================
echo ""
log "Cleaning /tmp/matter_testing (mobly temp logs)..."
if [[ -d "/tmp/matter_testing" ]]; then
    # Keep logs from last 24h only
    find /tmp/matter_testing -mindepth 1 -maxdepth 1 -mtime +1 | while read -r old_dir; do
        safe_rm "${old_dir}" "mobly log $(basename ${old_dir})"
    done
    ok "  Cleaned mobly logs older than 24h"
else
    log "  /tmp/matter_testing not found — nothing to clean"
fi

# =============================================================================
# Step 6 — Clean CI workspace logs (keep last N build log sets)
# =============================================================================
echo ""
log "Cleaning CI workspace build logs (keeping last ${KEEP_BUILD_LOGS})..."

BUILD_LOGS_DIR="${CI_LOGS_DIR}/build_logs"
if [[ -d "${BUILD_LOGS_DIR}" ]]; then
    # Remove individual app error logs older than 7 days
    find "${BUILD_LOGS_DIR}" -name "*_build_error.log" -mtime +7 | while read -r old_log; do
        safe_rm "${old_log}" "$(basename ${old_log})"
    done
    ok "  Removed build error logs older than 7 days"
fi

# =============================================================================
# Step 7 — Final disk report
# =============================================================================
echo ""
DISK_FREE_AFTER=$(df -BG / | awk 'NR==2{print $4}' | tr -d 'G')
FREED=$(( DISK_FREE_AFTER - DISK_FREE ))

echo "════════════════════════════════════════════════════════"
if (( FREED > 0 )); then
    ok "Cleanup complete — freed ~${FREED}GB | Free space now: ${DISK_FREE_AFTER}GB"
else
    ok "Cleanup complete — Free space: ${DISK_FREE_AFTER}GB"
fi

# Final warning if still critical
if (( DISK_FREE_AFTER < CRITICAL_FREE_GB )); then
    fail "CRITICAL: Still only ${DISK_FREE_AFTER}GB free after cleanup!"
    fail "Manual cleanup required before build can safely proceed."
    exit 1
fi
echo "════════════════════════════════════════════════════════"
echo ""
