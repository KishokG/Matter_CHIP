# Matter CI — Automated Build Pipeline

Automated pipeline to build Matter reference apps, chip-tool, and Python
controller on a Raspberry Pi using GitHub Actions self-hosted runner.

---

## Table of Contents

1. [Project Structure](#project-structure)
2. [How It Works](#how-it-works)
3. [Prerequisites](#prerequisites)
4. [Setup Guide — First Time](#setup-guide--first-time)
   - [Step 1 — System Dependencies on RPi](#step-1--system-dependencies-on-rpi)
   - [Step 2 — Register RPi as Self-Hosted Runner](#step-2--register-rpi-as-self-hosted-runner)
   - [Step 3 — Configure build_config.yaml](#step-3--configure-build_configyaml)
   - [Step 4 — Push to GitHub](#step-4--push-to-github)
   - [Step 5 — Trigger the Workflow](#step-5--trigger-the-workflow)
5. [Adding a New RPi as Self-Hosted Runner](#adding-a-new-rpi-as-self-hosted-runner)
6. [Build Modes](#build-modes)
7. [Configuration Reference](#configuration-reference)
8. [Adding a New Reference App](#adding-a-new-reference-app)
9. [Build Artifacts](#build-artifacts)
10. [Troubleshooting](#troubleshooting)
11. [File Reference](#file-reference)

---

## Project Structure

```
Matter_CI/
├── config/
│   └── build_config.yaml          ← All settings (SDK, apps, RPi paths)
├── scripts/
│   ├── build.sh                   ← Main build script — runs ON the RPi
│   ├── validate_config.py         ← Config validator — runs on GitHub runner
│   └── collect_build_info.py      ← Post-build summary — runs ON the RPi
└── README.md                      ← This file

.github/
└── workflows/
    └── matter_build.yml           ← GitHub Actions workflow
```

---

## How It Works

```
Your Laptop (browser)
      │
      │  Actions → Run workflow (manual trigger only)
      ▼
GitHub Actions
      │
      ├── Job 1: Validate Config   (runs on GitHub cloud runner — fast)
      │         └── checks build_config.yaml for errors
      │
      └── Job 2: Build             (runs DIRECTLY on RPi — self-hosted runner)
                ├── Checkout repo  (GitHub pulls code onto RPi)
                ├── Run build.sh
                │     ├── git pull / clone SDK
                │     ├── clean old builds + .environment
                │     ├── source scripts/bootstrap.sh
                │     ├── source scripts/activate.sh
                │     ├── gn_build_example.sh (reference apps)
                │     ├── gn_build_example.sh (chip-tool)
                │     └── build_python.sh (python controller)
                ├── Collect build summary
                └── Upload artifact (downloadable from GitHub)
```

**No SSH, no Tailscale, no secrets needed** — the RPi runner picks up jobs
directly from GitHub over the internet.

---

## Prerequisites

| What | Requirement |
|---|---|
| Raspberry Pi OS | Ubuntu 22.04 or 24.04, 64-bit |
| RAM | 8 GB recommended (4 GB minimum + swap) |
| Storage | 50 GB free minimum |
| Internet | RPi must have outbound internet access |
| GitHub repo | Public or private repository |

---

## Setup Guide — First Time

### Step 1 — System Dependencies on RPi

SSH into your RPi and install required packages:

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
  git python3 python3-pip python3-venv python3-dev \
  python3-yaml pkg-config libssl-dev libdbus-1-dev \
  libglib2.0-dev libavahi-client-dev ninja-build cmake \
  libgirepository1.0-dev libcairo2-dev unzip rsync curl \
  libdbus-1-dev libgirepository1.0-dev

# Verify versions
python3 --version    # Need 3.10+
git --version        # Need 2.x+
```

**Add swap space** (important for builds on 4GB RPi):

```bash
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Make permanent
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Verify
free -h
```

---

### Step 2 — Register RPi as Self-Hosted Runner

This is the key step — it connects your RPi to GitHub Actions.

#### 2.1 — Get the registration token from GitHub

Go to your repo on GitHub:
```
Settings → Actions → Runners → New self-hosted runner
```

Select:
- **Operating System:** Linux
- **Architecture:** ARM64

GitHub shows you a set of commands with a **unique token** — copy them.

#### 2.2 — Run the setup commands on RPi

```bash
# Create runner directory
mkdir -p ~/actions-runner
cd ~/actions-runner

# Download the runner (use exact URL from GitHub — version changes)
curl -o actions-runner-linux-arm64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.x.x/actions-runner-linux-arm64-2.x.x.tar.gz

# Extract
tar xzf actions-runner-linux-arm64.tar.gz

# Configure — use exact token from GitHub (expires in 1 hour)
./config.sh \
  --url https://github.com/YOUR_USERNAME/Matter_CHIP \
  --token YOUR_TOKEN_FROM_GITHUB

# When prompted:
#   Runner name:  kishok-rpi          ← give it a meaningful name
#   Runner group: Default             ← press Enter
#   Labels:       self-hosted         ← press Enter (or add custom labels)
#   Work folder:  _work               ← press Enter
```

#### 2.3 — Install as a system service (auto-starts on boot)

```bash
cd ~/actions-runner

# Install service
sudo ./svc.sh install

# Start service
sudo ./svc.sh start

# Check status
sudo ./svc.sh status
# Should show: active (running)
```

#### 2.4 — Verify on GitHub

Go to:
```
Settings → Actions → Runners
```

You should see:
```
Self-hosted runners
──────────────────────────────────────
  ● kishok-rpi    Idle    self-hosted, Linux, ARM64
```

**Idle** = ready to pick up jobs ✅

---

### Step 3 — Configure build_config.yaml

Edit `Matter_CI/config/build_config.yaml`:

```yaml
sdk:
  repo: "https://github.com/project-chip/connectedhomeip.git"
  branch: "master"         # or "v1.4-branch", "v1.5-branch" etc.
  sha: ""                  # leave empty for branch HEAD, or pin e.g. "a1b2c3d"
  bootstrap: true
  platform: "linux"        # submodule platform filter
  submodule_jobs: 4        # parallel submodule checkout jobs

apps:
  - name: "all-clusters-app"
    enabled: true           # ← set true/false per your needs
    ...

rpi:
  sdk_dir: "/home/ubuntu/connectedhomeip"   # ← where SDK lives on YOUR RPi
```

**Important:** `rpi.sdk_dir` must match the actual path on your RPi.

---

### Step 4 — Push to GitHub

```bash
cd ~/Matter_CHIP

git add .
git commit -m "Initial Matter CI setup"
git push origin main
```

**Note:** Pushing does NOT trigger the workflow — it is manual-trigger only.

---

### Step 5 — Trigger the Workflow

1. Go to your repo on GitHub
2. Click **Actions** tab
3. Click **Matter — Build on RPi** in the left sidebar
4. Click **Run workflow** button (top right)
5. Select build mode:
   - `full` — first time / branch change
   - `skip-clone` — update existing SDK to new TOT or SHA
   - `skip-all` — rebuild same commit (fastest)
6. Click green **Run workflow** button
7. Watch logs in real time

---

## Adding a New RPi as Self-Hosted Runner

Follow these steps to add any additional RPi to the same pipeline.

### Step 1 — Install system dependencies

Same as [Step 1 above](#step-1--system-dependencies-on-rpi) — run on the new RPi.

### Step 2 — Get a new registration token

```
GitHub → Settings → Actions → Runners → New self-hosted runner
```

**Each RPi needs its own unique token** — tokens expire in 1 hour.

### Step 3 — Setup runner on new RPi

```bash
# On the NEW RPi:
mkdir -p ~/actions-runner
cd ~/actions-runner

# Download runner (same URL as before)
curl -o actions-runner-linux-arm64.tar.gz -L \
  https://github.com/actions/runner/releases/download/v2.x.x/actions-runner-linux-arm64-2.x.x.tar.gz

tar xzf actions-runner-linux-arm64.tar.gz

# Configure with NEW token and DIFFERENT name
./config.sh \
  --url https://github.com/YOUR_USERNAME/Matter_CHIP \
  --token NEW_TOKEN_FROM_GITHUB

# When prompted:
#   Runner name:  kishok-rpi-2        ← different name from first RPi
#   Runner group: Default
#   Labels:       self-hosted,rpi-2   ← add a unique label
#   Work folder:  _work
```

### Step 4 — Install as service on new RPi

```bash
sudo ./svc.sh install
sudo ./svc.sh start
sudo ./svc.sh status
```

### Step 5 — Update build_config.yaml for new RPi

Add the new RPi's SDK path:

```yaml
rpi:
  sdk_dir: "/home/ubuntu/connectedhomeip"   # path on the specific RPi
```

### Step 6 — Target specific RPi in workflow (optional)

If you want to run on a specific RPi, update the workflow:

```yaml
# Run on any available self-hosted runner (default)
build:
  runs-on: self-hosted

# Run on specific RPi using its label
build:
  runs-on: [self-hosted, rpi-2]
```

### Step 7 — Verify both runners on GitHub

```
Settings → Actions → Runners
──────────────────────────────────────
  ● kishok-rpi      Idle    self-hosted, Linux, ARM64
  ● kishok-rpi-2    Idle    self-hosted, Linux, ARM64, rpi-2
```

---

## Build Modes

| Mode | What it does | When to use |
|---|---|---|
| `full` | Clone SDK → clean → bootstrap → build | First time ever, or after deleting SDK |
| `skip-clone` | git pull/checkout → clean → bootstrap → build | New TOT, new SHA, branch change |
| `skip-all` | Build only (no clone/clean/bootstrap) | Rebuilding exact same commit |

### Exact flow per mode

**`full`:**
```
git clone (depth=1)
  → checkout_submodules.py --platform linux
  → rm -rf .environment + old build dirs
  → source scripts/bootstrap.sh
  → source scripts/activate.sh
  → gn_build_example.sh (apps)
  → gn_build_example.sh (chip-tool)
  → build_python.sh
```

**`skip-clone`:**
```
git pull (or git checkout <SHA>)
  → checkout_submodules.py --platform linux
  → rm -rf .environment + old build dirs
  → source scripts/bootstrap.sh
  → source scripts/activate.sh
  → gn_build_example.sh (apps)
  → gn_build_example.sh (chip-tool)
  → build_python.sh
```

**`skip-all`:**
```
source scripts/activate.sh
  → gn_build_example.sh (apps)
  → gn_build_example.sh (chip-tool)
  → build_python.sh
```

---

## Configuration Reference

### `build_config.yaml`

```yaml
sdk:
  repo: "https://github.com/project-chip/connectedhomeip.git"
  branch: "master"          # SDK branch to build
  sha: ""                   # Pin to exact commit (optional)
  bootstrap: true           # Run bootstrap.sh after clone/update
  platform: "linux"         # Platform for checkout_submodules.py
                            # Options: linux, esp32, nrfconnect, android, darwin
                            # Multiple: "linux esp32"
  submodule_jobs: 4         # Parallel submodule checkout jobs

apps:
  - name: "all-clusters-app"
    enabled: true           # true = build, false = skip
    source_dir: "examples/all-clusters-app/linux"
    build_dir: "examples/all-clusters-app/linux/out/all-clusters-app"
    binary_name: "chip-all-clusters-app"
    extra_gn_args: "chip_inet_config_enable_ipv4=false"

chip_tool:
  enabled: true
  source_dir: "examples/chip-tool"
  build_dir: "out/chip-tool"
  binary_name: "chip-tool"
  extra_gn_args: 'chip_mdns="platform" chip_inet_config_enable_ipv4=false'

python_controller:
  enabled: true
  install_venv_name: "python_env"
  extra_args: ""            # e.g. "--enable_thread_meshcop true"

rpi:
  user: "ubuntu"
  sdk_dir: "/home/ubuntu/connectedhomeip"   # SDK location on RPi
```

---

## Adding a New Reference App

In `build_config.yaml`, add an entry under `apps:`:

```yaml
apps:
  - name: "lighting-app"
    enabled: true
    source_dir: "examples/lighting-app/linux"
    build_dir: "out/lighting-app"
    binary_name: "chip-lighting-app"
    extra_gn_args: "chip_inet_config_enable_ipv4=false"
```

Available apps in the SDK:
- `all-clusters-app` — all clusters reference app
- `lighting-app` — on/off light
- `lock-app` — door lock
- `thermostat` — thermostat
- `bridge-app` — bridge
- `contact-sensor-app` — contact sensor
- `window-app` — window covering

Push the config change and trigger with `skip-all` mode if SDK is unchanged.

---

## Build Artifacts

After every run, a `build-summary-<N>` artifact is uploaded automatically.

**Download:**
```
GitHub → Actions → (click run) → Artifacts section → build-summary-N
```

**Contents of `build_summary.txt`:**
```
══════════════════════════════════════════════════════════════
  Matter CI — Build Summary
  2026-06-24 10:32:01
══════════════════════════════════════════════════════════════
  SDK Dir    : /home/ubuntu/connectedhomeip
  Branch     : master
  Commit     : 10dc2506ef49...

── Reference Apps ────────────────────────────────────────────
  ✅  all-clusters-app    48.2 MB   /home/ubuntu/.../chip-all-clusters-app
  ⏭   lighting-app        disabled

── chip-tool ─────────────────────────────────────────────────
  ✅  chip-tool           22.1 MB   /home/ubuntu/.../chip-tool

── Python Controller ─────────────────────────────────────────
  ✅  venv → /home/ubuntu/connectedhomeip/python_env

  ✅  All enabled targets built successfully!
```

---

## Troubleshooting

### Runner shows Offline on GitHub

```bash
# On RPi — restart the runner service
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh start
sudo ./svc.sh status
```

### Runner not picking up jobs

```bash
# Check runner logs
journalctl -u actions.runner.* -f

# Or check runner log files
cat ~/actions-runner/_diag/Runner_*.log | tail -50
```

### Bootstrap fails — missing packages

```bash
# Re-run system dependency install
sudo apt install -y \
  git python3 python3-pip python3-venv python3-dev python3-yaml \
  pkg-config libssl-dev libdbus-1-dev libglib2.0-dev \
  libavahi-client-dev ninja-build cmake \
  libgirepository1.0-dev libcairo2-dev unzip
```

### Build runs out of memory

```bash
# Check available memory
free -h

# Add/increase swap
sudo swapoff /swapfile
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

### activate.sh fails with unbound variable

The `build.sh` handles this with `set +u` before sourcing `activate.sh`.
If you see this error, make sure you have the latest `build.sh` from this repo.

### Re-register runner (token expired)

```bash
# Remove old runner
cd ~/actions-runner
sudo ./svc.sh stop
sudo ./svc.sh uninstall
./config.sh remove --token OLD_TOKEN

# Get new token from GitHub:
# Settings → Actions → Runners → New self-hosted runner
# Then re-run config.sh with new token
./config.sh \
  --url https://github.com/YOUR_USERNAME/Matter_CHIP \
  --token NEW_TOKEN
sudo ./svc.sh install
sudo ./svc.sh start
```

### Check what's running on RPi during build

```bash
# Watch build logs in real time on RPi
journalctl -u actions.runner.* -f

# Check running processes
ps aux | grep -E "gn|ninja|bootstrap|chip"

# Monitor CPU and memory
htop
```

---

## File Reference

| File | Runs on | Purpose |
|---|---|---|
| `config/build_config.yaml` | — | All settings — SDK, apps, RPi path |
| `scripts/build.sh` | RPi | Main build orchestrator |
| `scripts/validate_config.py` | GitHub runner | Validates YAML before touching RPi |
| `scripts/collect_build_info.py` | RPi | Post-build binary size summary |
| `.github/workflows/matter_build.yml` | GitHub Actions | Workflow — manual trigger only |
