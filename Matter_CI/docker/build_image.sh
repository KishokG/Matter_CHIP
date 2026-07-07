#!/usr/bin/env bash
# =============================================================================
# build_image.sh — ONE-TIME helper to build the Matter SDK builder image
# =============================================================================
# Run this MANUALLY on the Mac mini (Apple Silicon) whenever you need to (re)build
# the base image — e.g. first setup, an apt-packages.txt change, or when the SDK
# needs a fresh bootstrap. Nightly CI does NOT run this; it only `docker run`s
# the already-built image.
#
#   ./Matter_CI/docker/build_image.sh                 # branch=master (default)
#   ./Matter_CI/docker/build_image.sh v1.6-branch     # build for a specific branch
#
# Requires Docker Desktop (or colima) with linux/arm64 support on the Mac mini.
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MATTER_CI="$(cd "${SCRIPT_DIR}/.." && pwd)"   # build context: needs apt-packages.txt

SDK_BRANCH="${1:-master}"
IMAGE="matter-sdk-builder:${SDK_BRANCH}"

echo "══════════════════════════════════════════════════════════════"
echo "  Building Matter SDK builder image"
echo "──────────────────────────────────────────────────────────────"
echo "  Image        : ${IMAGE}"
echo "  SDK branch   : ${SDK_BRANCH}"
echo "  Platform     : linux/arm64"
echo "  Build context: ${MATTER_CI}"
echo "  Dockerfile   : ${SCRIPT_DIR}/Dockerfile"
echo "══════════════════════════════════════════════════════════════"

# Sanity checks
command -v docker >/dev/null || { echo "ERROR: docker not found. Install Docker Desktop / colima."; exit 1; }
[[ -f "${MATTER_CI}/apt-packages.txt" ]] || { echo "ERROR: ${MATTER_CI}/apt-packages.txt missing."; exit 1; }

START=$(date +%s)
docker build \
    --platform linux/arm64 \
    --build-arg "SDK_BRANCH=${SDK_BRANCH}" \
    -f "${SCRIPT_DIR}/Dockerfile" \
    -t "${IMAGE}" \
    "${MATTER_CI}"
ELAPSED=$(( $(date +%s) - START ))

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  ✅ Image built: ${IMAGE}   (in $((ELAPSED/60))m $((ELAPSED%60))s)"
echo "──────────────────────────────────────────────────────────────"
echo "  Verify it:"
echo "    docker images ${IMAGE}"
echo "    docker run --rm ${IMAGE} bash -c 'source /connectedhomeip/scripts/activate.sh && gn --version && ninja --version'"
echo ""
echo "  Nightly CI runs it like this (the workflow does this for you):"
echo "    mkdir -p ~/matter-output"
echo "    docker run --rm \\"
echo "      -v ~/matter-output:/output \\"
echo "      -v \"\$GITHUB_WORKSPACE/Matter_CI\":/matter-ci:ro \\"
echo "      ${IMAGE} \\"
echo "      bash /matter-ci/docker/build_inside_container.sh"
echo ""
echo "  Rebuild this image ONLY when apt-packages.txt changes or the SDK needs"
echo "  a fresh bootstrap. Day-to-day SDK updates happen via git pull inside"
echo "  the container — no rebuild needed."
echo "══════════════════════════════════════════════════════════════"
