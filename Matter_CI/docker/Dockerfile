# =============================================================================
# Matter SDK Builder — pre-built image (Approach B)
# =============================================================================
# Ubuntu 24.04 ARM64 image with all connectedhomeip build tools + a bootstrapped
# SDK baked in, so nightly builds SKIP the expensive bootstrap and only:
#   git pull → build apps → copy binaries to /output.
#
# Built ONCE on the Mac mini (see build_image.sh). NOT rebuilt every night.
# The nightly `git pull` inside the container (build_inside_container.sh) picks
# up latest SDK code without an image rebuild.
#
# The Matter_CI scripts + build_config.yaml are NOT baked in — they are mounted
# read-only at /matter-ci at run time (from the runner's checkout), so config
# changes take effect with no image rebuild.
#
# Binaries produced are Linux/ARM64 — built here, run natively on the RPi.
# =============================================================================
FROM ubuntu:24.04

# Build args (override at build time, e.g. --build-arg SDK_BRANCH=v1.6-branch)
ARG SDK_REPO="https://github.com/project-chip/connectedhomeip.git"
ARG SDK_BRANCH="master"
ARG SUBMODULE_JOBS="4"

ENV DEBIAN_FRONTEND=noninteractive \
    SDK_DIR=/connectedhomeip

# ── System dependencies ──────────────────────────────────────────────────────
# ca-certificates is required for the https clone/bootstrap and is not in the
# base image; the rest come verbatim from Matter_CI/apt-packages.txt (the same
# list the RPi uses), so the two environments never drift.
COPY apt-packages.txt /tmp/apt-packages.txt
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates sudo locales && \
    apt-get install -y --no-install-recommends \
        $(grep -vE '^\s*#|^\s*$' /tmp/apt-packages.txt | tr '\n' ' ') && \
    locale-gen en_US.UTF-8 && \
    rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8

# ── Clone the SDK ────────────────────────────────────────────────────────────
# Full clone (not --depth 1) so the nightly `git pull` inside the container can
# fast-forward the branch. Pinned to SDK_BRANCH.
RUN git clone --branch "${SDK_BRANCH}" "${SDK_REPO}" "${SDK_DIR}"

WORKDIR ${SDK_DIR}

# ── Submodules (linux platform only) ─────────────────────────────────────────
RUN python3 scripts/checkout_submodules.py \
        --platform linux \
        --shallow \
        --recursive \
        --jobs "${SUBMODULE_JOBS}" \
        --allow-changing-global-git-config

# ── Bootstrap (the expensive, baked-in step) ─────────────────────────────────
# Creates .environment (pigweed venv, CIPD packages, gn/ninja). Nightly builds
# only `source scripts/activate.sh` — they never re-bootstrap.
RUN bash -c "source scripts/bootstrap.sh"

# Record the branch the image was built from (build_inside_container.sh reads
# this to `git pull` the same branch, and to warn on a config/image mismatch).
RUN echo "${SDK_BRANCH}" > /image-sdk-branch

# Default to an interactive shell; the nightly run overrides the command with
#   bash /matter-ci/docker/build_inside_container.sh
CMD ["/bin/bash"]
