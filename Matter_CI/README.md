# Matter CI — Automated Build Pipeline

Automated pipeline to build Matter reference apps, chip-tool, and Python
controller on a Raspberry Pi using GitHub Actions self-hosted runner.

Trigger builds from your browser — no SSH, no Tailscale, no secrets needed.

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [How It Works](#how-it-works)
3. [Prerequisites](#prerequisites)
4. [Setup Guide — First Time](#setup-guide--first-time)
5. [Adding a New RPi as Self-Hosted Runner](#adding-a-new-rpi-as-self-hosted-runner)
6. [Triggering a Build](#triggering-a-build)
7. [Build Modes](#build-modes)
8. [Runtime Inputs](#runtime-inputs)
9. [Configuration Reference](#configuration-reference)
10. [System Dependencies](#system-dependencies)
11. [Adding a New Reference App](#adding-a-new-reference-app)
12. [Build Artifacts](#build-artifacts)
13. [Troubleshooting](#troubleshooting)
14. [File Reference](#file-reference)

---

## Project Structure

```
Matter_CHIP/
├── Matter_CI/
│   ├── config/
│   │   └── build_config.yaml      ← All build settings (SDK branch/SHA, app selection, RPi path)
│   ├── scripts/
│   │   ├── build.sh               ← Main build script — runs ON the RPi
│   │   ├── discover_targets.py    ← Resolves reference apps dynamically from the SDK
│   │   ├── validate_config.py     ← Config validator — runs on GitHub cloud runner
│   │   └── collect_build_info.py  ← Post-build summary — runs ON the RPi
│   ├── apt-packages.txt           ← System packages auto-installed before every build
│   └── README.md                  ← This file
│
└── .github/
    └── workflows/
        └── matter_build.yml       ← GitHub Actions workflow (manual trigger only)
```

---

## How It Works

```
Your Laptop (browser)
      │
      │  Actions → Run workflow (manual only — push never triggers)
      ▼
GitHub Actions
      │
      ├── Job 1: Validate Config     (GitHub cloud runner — ~10 seconds)
      │         └── Validates build_config.yaml for errors
      │
      └── Job 2: Build               (self-hosted runner — runs DIRECTLY on RPi)
                ├── Checkout repo    (GitHub pulls Matter_CHIP onto RPi)
                ├── Run build.sh
                │     ├── Step 0: install missing apt packages (apt-packages.txt)
                │     ├── Step 0: install missing pip packages (pycairo)
                │     ├── Step 1: git clone / git pull SDK        [full / skip-clone]
                │     ├── Step 2: clean old builds + .environment [full / skip-clone]
                │     ├── Step 3: source scripts/bootstrap.sh     [full / skip-clone]
                │     ├── Step 3.5: discover_targets.py (resolve reference apps from SDK)
                │     ├── Step 4: source scripts/activate.sh
                │     ├── Step 5: gn_build_example.sh (reference apps)
                │     ├── Step 6: gn_build_example.sh (chip-tool)
                │     └── Step 7: build_python.sh (python controller)
                ├── Collect build summary
                └── Upload artifact  (downloadable from GitHub Actions)
```

---

## Prerequisites

| What | Requirement |
|---|---|
| Raspberry Pi OS | Ubuntu 22.04 or 24.04, 64-bit (ARM64) |
| RAM | 8 GB recommended (4 GB minimum + swap) |
| Storage | 50 GB free minimum |
| Internet | RPi must have outbound internet access |
| GitHub repo | Public or private |

---

## Setup Guide — First Time

### Step 1 — Add swap space on RPi

Matter builds are RAM-heavy. Add swap before anything else:

```bash
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h   # verify 8G swap shown
```

### Step 2 — Install initial system dependencies

The build script auto-installs packages from `apt-packages.txt` on every run,
but a few are needed to bootstrap the runner itself:

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git curl python3 python3-yaml
```

### Step 3 — Register RPi as self-hosted runner

#### 3.1 Get registration token from GitHub

```
Your repo → Settings → Actions → Runners → New self-hosted runner
Select: Linux / ARM64
```

GitHub shows commands with a unique token — **token expires in 1 hour**.

#### 3.2 Run setup commands on RPi

```bash
mkdir -p ~/actions-runner
cd ~/actions-runner

# Use the exact URL GitHub shows you (version changes)
curl -o actions-runner-linux-arm64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.x.x/actions-runner-linux-arm64-2.x.x.tar.gz

tar xzf actions-runner-linux-arm64.tar.gz

# Use the exact token GitHub shows you
./config.sh \
  --url https://github.com/KishokG/Matter_CHIP \
  --token YOUR_TOKEN_FROM_GITHUB

# Prompts:
#   Runner name:  kishok-rpi      ← meaningful name
#   Runner group: Default         ← Enter
#   Labels:       self-hosted     ← Enter
#   Work folder:  _work           ← Enter
```

#### 3.3 Install as system service (auto-starts on boot)

```bash
cd ~/actions-runner
sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status   # should show: active (running)
```

#### 3.4 Verify on GitHub

```
Settings → Actions → Runners
─────────────────────────────────────────
  ● kishok-rpi    Idle    self-hosted, Linux, ARM64
```

**Idle** = ready to pick up jobs ✅

### Step 4 — Configure build_config.yaml

Edit `Matter_CI/config/build_config.yaml` and set the SDK path on your RPi:

```yaml
rpi:
  sdk_dir: "/home/ubuntu/connectedhomeip"   # ← must match your RPi's path
```

Also set the SDK branch:
```yaml
sdk:
  branch: "v1.6-branch"   # or "master", "v1.4-branch" etc.
  sha: ""                  # leave empty for branch HEAD
```

### Step 5 — Push to GitHub

```bash
cd ~/Matter_CHIP
git add .
git commit -m "Initial Matter CI setup"
git push origin main
```

> **Note:** Pushing does NOT trigger the workflow — it only runs manually.

### Step 6 — Trigger your first build

```
GitHub → Actions → Matter — Build on RPi → Run workflow
Build mode: full   ← use full for first-ever run
SDK branch: v1.6-branch
Run workflow
```

First run takes ~90–150 min (clone + bootstrap + build).

---

## Adding a New RPi as Self-Hosted Runner

### Step 1 — Add swap + install deps on new RPi

```bash
# Swap
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Initial deps
sudo apt update && sudo apt install -y git curl python3 python3-yaml
```

### Step 2 — Get a NEW registration token

```
Settings → Actions → Runners → New self-hosted runner
```

Each RPi needs its own unique token — **tokens expire in 1 hour**.

### Step 3 — Setup runner on new RPi

```bash
mkdir -p ~/actions-runner
cd ~/actions-runner

curl -o actions-runner-linux-arm64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.x.x/actions-runner-linux-arm64-2.x.x.tar.gz

tar xzf actions-runner-linux-arm64.tar.gz

./config.sh \
  --url https://github.com/KishokG/Matter_CHIP \
  --token NEW_TOKEN_FROM_GITHUB

# Prompts:
#   Runner name:  kishok-rpi-2        ← DIFFERENT name from first RPi
#   Labels:       self-hosted,rpi-2   ← add a unique label

sudo ./svc.sh install
sudo ./svc.sh start
```

### Step 4 — Target a specific RPi in the workflow (optional)

```yaml
# Run on any available RPi (default)
runs-on: self-hosted

# Run on a specific RPi
runs-on: [self-hosted, rpi-2]
```

### Step 5 — Verify both runners on GitHub

```
Settings → Actions → Runners
─────────────────────────────────────────────────────
  ● kishok-rpi      Idle    self-hosted, Linux, ARM64
  ● kishok-rpi-2    Idle    self-hosted, Linux, ARM64, rpi-2
```

---

## Triggering a Build

1. Go to your repo → **Actions** tab
2. Click **Matter — Build on RPi** in the left sidebar
3. Click **Run workflow** (top right)
4. Fill in the inputs (see [Runtime Inputs](#runtime-inputs) below)
5. Click green **Run workflow**
6. Watch live logs by clicking the running job

---

## Build Modes

| Mode | Steps performed | When to use |
|---|---|---|
| `full` | install deps → clone SDK → clean → bootstrap → build | First time, or clean slate |
| `skip-clone` | install deps → git pull/checkout → clean → bootstrap → build | New TOT, new SHA, branch change |
| `skip-all` | install deps → build only | Rebuilding exact same commit (fastest) |

### Step-by-step flow

**All modes always run first:**
```
install_system_deps()
  → check apt-packages.txt → install missing apt packages
  → check pycairo → install if missing
```

**`full` then runs:**
```
rm -rf connectedhomeip/         ← delete existing SDK if present
git clone --branch <branch>     ← fresh clone
checkout_submodules.py --platform linux --shallow
rm -rf .environment + build dirs
source scripts/bootstrap.sh
source scripts/activate.sh      ← set +u to handle optional PW_* vars
gn_build_example.sh → ninja
build_python.sh
```

**`skip-clone` then runs:**
```
git fetch + git checkout -B <branch> origin/<branch>
checkout_submodules.py --platform linux --shallow
rm -rf .environment + build dirs
source scripts/bootstrap.sh
source scripts/activate.sh
gn_build_example.sh → ninja
build_python.sh
```

**`skip-all` then runs:**
```
source scripts/activate.sh
gn_build_example.sh → ninja
build_python.sh
```

> ⚠️ `skip-all` only works if bootstrap was previously run for the current commit.

---

## Runtime Inputs

When clicking **Run workflow**, you can override config values at runtime:

| Input | Description | Default |
|---|---|---|
| **Build mode** | `full` / `skip-clone` / `skip-all` | `skip-clone` |
| **SDK branch** | Override branch (e.g. `v1.6-branch`, `master`) | Uses `build_config.yaml` value |
| **SDK SHA** | Pin to a specific commit hash | Uses `build_config.yaml` value |
| **Target apps** | Comma-separated SDK shorthands (e.g. `all-clusters,light`) — enables exactly these in `discovery.apps`, disables the rest | Uses `enabled` flags in `discovery.apps` |

**Priority:** Runtime input → `build_config.yaml` value → branch HEAD

---

## Configuration Reference

### `build_config.yaml`

```yaml
sdk:
  repo: "https://github.com/project-chip/connectedhomeip.git"
  branch: "v1.6-branch"     # SDK branch (overridable at runtime)
  sha: ""                    # Pin to exact commit SHA (optional, overridable at runtime)
  bootstrap: true            # Run bootstrap.sh after clone/update
  platform: "linux"          # Submodule platform filter
                             # Options: linux, esp32, nrfconnect, darwin, android
                             # Multiple: "linux esp32"
  submodule_jobs: 4          # Parallel submodule checkout jobs

# Reference apps are DISCOVERED DYNAMICALLY from the SDK — there is no
# hardcoded source_dir/binary_name list. At build time discover_targets.py
# resolves each app's real source_dir + binary from the SDK's HostApp
# mapping. discovery.apps enumerates EVERY buildable app (~34); you flip
# `enabled` per app. `modifiers` mirror the Matter Test Harness targets
# (chip-cert-bins) and become gn args, so binaries build like the TH:
#   ipv6only → chip_inet_config_enable_ipv4=false | platform-mdns → chip_mdns="platform"
#   nfc-commission → chip_enable_nfc_based_commissioning=true | rpc → import("//with_pw_rpc.gni")
#   nlfaultinject → chip_with_nlfaultinjection=true | clang → is_clang=true
discovery:
  apps:
    - { name: all-clusters,    enabled: true,  modifiers: [ipv6only] }        # chip-all-clusters-app
    - { name: light,           enabled: true,  modifiers: [ipv6only] }        # chip-lighting-app
    - { name: network-manager, enabled: true,  modifiers: [ipv6only] }        # matter-network-manager-app
    - { name: fabric-admin,    enabled: false, modifiers: [rpc, ipv6only] }   # fabric-admin
    # ... ~34 apps total; regenerate with --emit-config-apps (see below)

chip_tool:               # built separately; matches TH chip-tool target
  enabled: true
  source_dir: "examples/chip-tool"
  build_dir: "out/chip-tool"
  binary_name: "chip-tool"
  extra_gn_args: 'chip_mdns="platform" chip_inet_config_enable_ipv4=false chip_enable_nfc_based_commissioning=true'

python_controller:
  enabled: true
  install_venv_name: "python_env"
  extra_args: ""             # e.g. "--enable_thread_meshcop true"

rpi:
  sdk_dir: "/home/ubuntu/connectedhomeip"
```

---

## System Dependencies

All system packages are listed in `apt-packages.txt` and installed automatically
before every build. Only missing packages are installed — already-installed
packages are skipped (fast check via `dpkg -s`).

**To add a new dependency:**
```bash
echo "libnew-dev" >> Matter_CI/apt-packages.txt
git add Matter_CI/apt-packages.txt
git commit -m "Add libnew-dev dependency"
git push origin main
```

The next build run will automatically install it.

**pip packages** (currently just `pycairo`) are also checked and installed
automatically via `pip3 install --break-system-packages`.

---

## Enabling / Adding a Reference App

`discovery.apps` in `build_config.yaml` already lists **every** buildable
reference app (~34). To build one, just flip its `enabled` to `true` — no
`source_dir` / `binary_name` to look up (resolved automatically from the SDK):

```yaml
discovery:
  apps:
    - { name: refrigerator, enabled: true, modifiers: [ipv6only] }   # ← was false
```

**`modifiers`** control the gn build args and mirror the Matter Test Harness
(`chip-cert-bins`) targets, so your binaries build the same way the TH does:

| modifier | gn arg | used by (TH) |
|---|---|---|
| `ipv6only` | `chip_inet_config_enable_ipv4=false` | every reference app |
| `platform-mdns` | `chip_mdns="platform"` | chip-tool, shell, chip-cert |
| `nfc-commission` | `chip_enable_nfc_based_commissioning=true` | chip-tool |
| `rpc` | `import("//with_pw_rpc.gni")` | fabric-admin, fabric-bridge |
| `nlfaultinject` | `chip_with_nlfaultinjection=true` | all-clusters fault-injection variant |
| `clang` | `is_clang=true` | camera (arm64) |

**To regenerate the full app menu** (e.g. after an SDK bump adds apps), paste
the output under `discovery:`:

```bash
python3 Matter_CI/scripts/discover_targets.py \
    --sdk-dir /home/ubuntu/connectedhomeip --emit-config-apps
```

> Enabling many apps is heavy on RPi build time + disk — enable only what you
> test. `chip-tool` and the python controller build from their own sections.

Trigger with `skip-all` if the SDK hasn't changed.

---

## Build Artifacts

After every run (pass or fail), a `build-summary-<N>` artifact is uploaded.

**Download:**
```
GitHub → Actions → (click run) → Artifacts → build-summary-N
```

**Example output:**
```
══════════════════════════════════════════════════════════════
  Matter CI — Build Summary
  2026-06-24 14:32:01
══════════════════════════════════════════════════════════════
  SDK Dir    : /home/ubuntu/connectedhomeip
  Branch     : v1.6-branch
  Commit     : d89f71558bf0c429ec60f3f76ea371775731701d

── Reference Apps ────────────────────────────────────────────
  ✅  all-clusters         48.2 MB
  ✅  light                41.7 MB

── chip-tool ─────────────────────────────────────────────────
  ✅  chip-tool           22.1 MB

── Python Controller ─────────────────────────────────────────
  ✅  venv → /home/ubuntu/connectedhomeip/python_env

  ✅  All enabled targets built successfully!
```

---

## Troubleshooting

### Runner shows Offline

```bash
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh start
sudo ./svc.sh status
```

### Runner not picking up jobs

```bash
journalctl -u actions.runner.* -f
cat ~/actions-runner/_diag/Runner_*.log | tail -50
```

### Build failed — missing system package

The build script auto-installs from `apt-packages.txt`. If a package is
missing from the list, add it to `apt-packages.txt` and push.

### Build runs out of memory (OOM)

```bash
free -h   # check current memory + swap

# Increase swap to 8GB
sudo swapoff /swapfile
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

### Runner lost communication (OOM during build)

```
The self-hosted runner lost communication with the server.
```

This means the RPi ran out of memory mid-build and the OS killed the runner process.
Fix: increase swap (see above), then restart runner and re-run.

### activate.sh fails with unbound variable

`build.sh` uses `set +u` around `source scripts/activate.sh` to handle this.
Ensure you have the latest `build.sh` from this repo.

### Branch not switching correctly

The build log shows both current and configured branch:
```
[BUILD] Current branch : master
[BUILD] Config branch  : v1.6-branch
[BUILD] Switching to branch from config: v1.6-branch
```

If the switch fails, use `full` mode to do a clean clone on the correct branch.

### Re-register runner (token expired or runner broken)

```bash
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh uninstall
./config.sh remove --token OLD_TOKEN  # or skip if token expired

# Get new token: Settings → Actions → Runners → New self-hosted runner
./config.sh \
  --url https://github.com/KishokG/Matter_CHIP \
  --token NEW_TOKEN
sudo ./svc.sh install
sudo ./svc.sh start
```

---

## File Reference

| File | Runs on | Purpose |
|---|---|---|
| `config/build_config.yaml` | — | All settings — SDK branch/SHA, app selection (`discovery:`), RPi path |
| `apt-packages.txt` | RPi | System packages auto-installed before every build |
| `scripts/build.sh` | RPi | Main build orchestrator — all 3 modes |
| `scripts/validate_config.py` | GitHub cloud runner | Validates YAML before any build starts |
| `scripts/collect_build_info.py` | RPi | Post-build binary sizes and paths |
| `.github/workflows/matter_build.yml` | GitHub Actions | Workflow — manual trigger only, no push trigger |
