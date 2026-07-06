# Matter Build Pipeline — Setup Instructions

Complete step-by-step guide to get GitHub Actions building Matter
reference apps, chip-tool, and the Python controller on your Raspberry Pi.

---

## Prerequisites

| What | Requirement |
|---|---|
| Raspberry Pi | Ubuntu 22.04 / 24.04, 64-bit, ≥ 8 GB RAM recommended |
| Storage | ≥ 50 GB free on RPi (SDK + build outputs are large) |
| Network | RPi must be reachable from the internet (or via VPN/tunnel) on SSH port |
| GitHub repo | Any repo where you'll push these scripts |

---

## Part 1 — Prepare the Raspberry Pi

### 1.1 Install system dependencies

SSH into your RPi and run:

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y \
  git python3 python3-pip python3-venv python3-dev \
  pkg-config libssl-dev libdbus-1-dev libglib2.0-dev \
  libavahi-client-dev ninja-build cmake \
  libgirepository1.0-dev libcairo2-dev \
  unzip wget rsync curl

# PyYAML needed by build.sh helper functions
pip3 install --user pyyaml
```

### 1.2 Verify Python and pip versions

```bash
python3 --version   # Need 3.10+
pip3 --version
```

### 1.3 Note your RPi's IP address

```bash
hostname -I
# e.g. 192.168.1.105
```

You'll need this as the `RPI_HOST` GitHub Secret.

---

## Part 2 — SSH Key Setup

The GitHub Actions runner SSHes into your RPi using a dedicated key pair.
**Never reuse your personal SSH key — generate a separate CI key.**

### 2.1 Generate the CI key pair (on your dev machine, not the RPi)

```bash
ssh-keygen -t ed25519 -C "matter-ci-github" -f ~/.ssh/matter_ci_rpi
# Press Enter twice for no passphrase (CI needs passwordless access)
```

This creates:
- `~/.ssh/matter_ci_rpi`       ← **private key** → goes into GitHub Secret
- `~/.ssh/matter_ci_rpi.pub`   ← **public key**  → goes onto the RPi

### 2.2 Copy the public key to the RPi

```bash
ssh-copy-id -i ~/.ssh/matter_ci_rpi.pub ubuntu@<RPI_IP>
```

Or manually:
```bash
cat ~/.ssh/matter_ci_rpi.pub | ssh ubuntu@<RPI_IP> "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

### 2.3 Test the connection

```bash
ssh -i ~/.ssh/matter_ci_rpi ubuntu@<RPI_IP> "echo OK"
# Should print: OK
```

### 2.4 Make RPi accessible from GitHub Actions

GitHub Actions runners are cloud machines — your RPi must be reachable on
its SSH port from the internet. Options:

**Option A — Port forward on your router (home lab)**
- Log into your router admin panel
- Forward external port `22` (or a custom port like `2222`) → RPi internal IP
- Use your home's public IP as `RPI_HOST`
- Check your public IP: `curl ifconfig.me`

**Option B — Cloudflare Tunnel (recommended, no open ports)**
```bash
# On the RPi:
wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i cloudflared-linux-arm64.deb
cloudflared tunnel login
cloudflared tunnel create matter-ci
cloudflared tunnel route dns matter-ci ssh.yourdomain.com
cloudflared service install
```
Then use `ssh.yourdomain.com` as `RPI_HOST` and add to the SSH config in the workflow:
```yaml
ProxyCommand: cloudflared access ssh --hostname %h
```

**Option C — Tailscale (simplest VPN approach)**
```bash
# On the RPi:
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip   # note the 100.x.x.x address
```
Add GitHub Actions IP ranges to your Tailscale ACL, or use a Tailscale
GitHub Action (`tailscale/github-action`) before the SSH step.

---

## Part 3 — Configure the Project

### 3.1 Edit `config/build_config.yaml`

Open the file and update:

```yaml
sdk:
  branch: "master"          # or "v1.4-branch", "v1.5-branch", etc.
  sha: ""                   # leave empty, or pin to a commit hash

discovery:                  # reference apps are discovered from the SDK
  apps:                     # ← full menu (~34); flip enabled per app
    - { name: all-clusters,    enabled: true,  modifiers: [ipv6only] }
    - { name: light,           enabled: true,  modifiers: [ipv6only] }
    - { name: network-manager, enabled: true,  modifiers: [ipv6only] }
    - { name: refrigerator,    enabled: false, modifiers: [ipv6only] }
    # ... etc — modifiers mirror the Matter Test Harness build args

rpi:
  user: "ubuntu"            # your RPi SSH user
  sdk_dir: "/home/ubuntu/connectedhomeip"   # where SDK will be cloned on RPi
```

**To build an app**, flip its `enabled` to `true`. `modifiers` mirror how the
Matter Test Harness builds each app (`ipv6only`, `rpc`, `platform-mdns`, …).
Regenerate the full menu after an SDK bump with:
`python3 scripts/discover_targets.py --sdk-dir <SDK> --emit-config-apps`.

**To pin to a specific Matter SDK commit:**
```yaml
sdk:
  branch: "master"
  sha: "a3f1b2c"   # first 7+ chars of the commit hash
```

---

## Part 4 — GitHub Repository Setup

### 4.1 Create (or use) a GitHub repo

```bash
cd matter-build
git init
git remote add origin https://github.com/YOUR_USERNAME/matter-build.git
git add .
git commit -m "Initial Matter CI build pipeline"
git push -u origin main
```

### 4.2 Add GitHub Secrets

Go to your repo on GitHub:
**Settings → Secrets and variables → Actions → New repository secret**

Add these secrets one by one:

| Secret Name | Value | How to get it |
|---|---|---|
| `SSH_PRIVATE_KEY` | Contents of `~/.ssh/matter_ci_rpi` | `cat ~/.ssh/matter_ci_rpi` |
| `RPI_HOST` | IP or hostname of RPi | `hostname -I` on RPi or router port-forward address |
| `RPI_USER` | SSH username | `ubuntu` or `pi` |
| `RPI_PORT` | SSH port (optional) | `22` (default), or your custom port |

**To copy the private key contents:**
```bash
cat ~/.ssh/matter_ci_rpi
# Copy EVERYTHING including -----BEGIN and END lines
```

> ⚠️ Never commit the private key file to your repo. The `.gitignore` already excludes it.

---

## Part 5 — Run the Pipeline

### 5.1 Trigger automatically

Push any change to `config/build_config.yaml` or `scripts/build.sh` to
`main`/`master` — the workflow fires automatically.

### 5.2 Trigger manually (workflow_dispatch)

1. Go to your repo → **Actions** tab
2. Click **"Matter — Build on RPi"** in the left sidebar
3. Click **"Run workflow"** button (top right)
4. Options:
   - **Skip SDK clone** — check this if the SDK is already cloned on RPi and you only want to rebuild binaries (saves 30–60 min)
   - **Target apps** — type comma-separated SDK shorthand names to build (e.g. `all-clusters,light`); enables exactly those in `discovery.apps` and disables the rest. Leave empty to use the config file.

### 5.3 Watch the run

Click the running workflow → expand each step to see live logs.

The most time-consuming steps:
- `Clone SDK + submodules` — ~10–20 min
- `Bootstrap` — ~5–15 min
- `Build apps (gn + ninja)` — ~30–90 min per app on RPi
- Subsequent runs with `--skip-sdk` are much faster (~10–30 min)

---

## Part 6 — Interpreting Results

### 6.1 Build summary artifact

After each run, a `build-summary-<run_number>` artifact is uploaded.
Download it from the **Actions → run → Artifacts** section.

Example output:
```
============================================================
  Matter CI — Build Summary
  Generated: 2025-06-23 14:32:01
============================================================

SDK Directory : /home/ubuntu/connectedhomeip
SDK Branch    : master
SDK Commit    : a1b2c3d4e5f6...

── Reference Apps ──────────────────────────────────────────
  ✅  all-clusters                  48.2 MB   /home/ubuntu/.../chip-all-clusters-app
  ✅  light                         41.7 MB   /home/ubuntu/.../chip-lighting-app

── chip-tool ───────────────────────────────────────────────
  ✅  chip-tool                     22.1 MB   /home/ubuntu/.../chip-tool

── Python Controller ───────────────────────────────────────
  ✅  python-controller wheel        4.3 MB   /home/ubuntu/.../chip_python_controller-...whl
  ✅  venv: /home/ubuntu/connectedhomeip/out/python_venv
```

### 6.2 GitHub job summary

The run's **Summary** tab shows a formatted table of what was built,
who triggered it, and the ref/SHA.

---

## Part 7 — Common Issues & Fixes

### SSH connection refused

```
ssh: connect to host ... port 22: Connection refused
```
- Check port forwarding / firewall on RPi: `sudo ufw status`
- Verify RPi is reachable: `ping <RPI_HOST>`
- Try connecting manually with the key: `ssh -i ~/.ssh/matter_ci_rpi -p <PORT> ubuntu@<HOST>`

### Bootstrap fails with missing packages

```
Package 'libXYZ' not found
```
Re-run Part 1.1 on the RPi. Some packages vary between Ubuntu 22.04 and 24.04.

### GN not found after bootstrap

```
bash: gn: command not found
```
```bash
# On the RPi, activate the environment manually first:
cd /home/ubuntu/connectedhomeip
source scripts/activate.sh
which gn    # should now resolve
```
The `build.sh` script calls `source scripts/activate.sh` automatically, but
if the bootstrap didn't complete successfully, `gn`/`ninja` won't be in PATH.

### Submodule errors

```
fatal: remote error: Repository not found
```
Some submodules require git credentials. On the RPi:
```bash
git config --global url."https://".insteadOf git://
```

### Build runs out of memory

Matter builds are RAM-heavy. Limit ninja parallelism:
In `build.sh`, change:
```bash
ninja -C "${build_dir}"
```
to:
```bash
ninja -j2 -C "${build_dir}"   # use only 2 parallel jobs
```
Or add swap space on the RPi:
```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## Quick Reference — File Roles

| File | Where it runs | Purpose |
|---|---|---|
| `config/build_config.yaml` | Config only | All settings: SDK branch/SHA, app selection (`discovery.apps`) |
| `scripts/validate_config.py` | GitHub runner | Catches config errors before touching RPi |
| `scripts/build.sh` | RPi | Clones SDK, bootstraps, builds everything |
| `scripts/collect_build_info.py` | RPi | Post-build summary of binary sizes |
| `.github/workflows/matter_build.yml` | GitHub Actions | Orchestrates the whole pipeline |
