# Mac mini Docker Build — Bring-up Guide

One-time setup + pre-testing for the Docker build migration (Approach B).
Do these **in order** on the Mac mini M4. Steps 1–6 are local (no CI); only
step 9 involves GitHub Actions.

> The Docker migration is on **`main`** (the default branch). Scheduled nightly
> runs fire from `main`; manual runs can target any branch.

---

## 0. What you're setting up

- **Mac mini** = the *builder*. Runs a Linux/arm64 Docker container that builds
  the SDK apps and drops Linux ARM64 binaries into `~/matter-output`. Also runs
  the upload + email jobs (host-side Python).
- **RPi** = the *tester*. Downloads the bundle, checks out the SDK to the build
  commit, and runs the 68 TCs (unchanged).

---

## 1. Install Docker Desktop (Apple Silicon)

1. Install **Docker Desktop for Mac (Apple chip)** from docker.com, open it once,
   finish setup.
2. Docker Desktop → **Settings → Resources**: give it generous limits (you have
   48 GB / 12 cores). Suggested: **CPUs ≥ 8, Memory ≥ 24 GB, Swap ≥ 2 GB, Disk ≥ 80 GB**.
3. Docker Desktop → **Settings → General**: enable **"Start Docker Desktop when
   you log in"** (the runner service needs Docker already running).
4. Verify:
   ```bash
   docker version
   docker run --rm --platform linux/arm64 ubuntu:24.04 uname -m   # -> aarch64
   ```

## 2. Install host tools (git + python)

```bash
xcode-select --install                 # git + compilers (skip if already present)
# Homebrew (if not installed): https://brew.sh
brew install python@3.11
python3 --version && pip3 --version
```
(Python here is only for the host-side upload/notify jobs — the SDK build runs
inside the container.)

## 3. Clone the repo (correct branch!)

```bash
git clone https://github.com/KishokG/Matter_CHIP.git ~/Matter_CHIP
cd ~/Matter_CHIP   # main branch has the Docker migration
```
This manual clone is used only to **build the image** (step 4). During CI the
runner checks out its own copy automatically.

## 4. Build the Docker image (one-time, ~20–60 min)

```bash
cd ~/Matter_CHIP
./Matter_CI/docker/build_image.sh master
```
This builds `ubuntu:24.04` (arm64), installs `apt-packages.txt`, clones the SDK
`master`, syncs submodules, and runs `bootstrap.sh` (the slow part, baked in so
nightly builds skip it). Tag produced: **`matter-sdk-builder:master`**.

> Rebuild this image ONLY when `apt-packages.txt` changes or the SDK needs a
> fresh bootstrap. Day-to-day SDK updates happen via `git pull` inside the
> container — no rebuild.

## 5. Pre-test the image (fast sanity — no full build)

```bash
docker images matter-sdk-builder:master                       # exists?
docker run --rm matter-sdk-builder:master \
  bash -c 'source /connectedhomeip/scripts/activate.sh && gn --version && ninja --version && echo ENV_OK'
```
Expect `gn`/`ninja` versions + `ENV_OK`. If this fails, the bootstrap didn't
bake correctly — rebuild the image.

## 6. Pre-test the full container build (the real dry run)

**Tip:** for a fast first smoke test, temporarily enable just ONE app so this
takes ~10–15 min instead of the full ~45–90 min. Edit
`Matter_CI/config/build_config.yaml` → set every `discovery.apps` entry to
`enabled: false` except `all-clusters`. (Revert after.)

```bash
mkdir -p ~/matter-output ~/matter-ccache
docker run --rm \
  -v ~/matter-output:/output \
  -v ~/Matter_CHIP/Matter_CI:/matter-ci:ro \
  -v ~/matter-ccache:/root/.ccache \
  matter-sdk-builder:master \
  bash /matter-ci/docker/build_inside_container.sh
```
(The `~/matter-ccache` mount persists the compiler cache across runs — the
first build is cold, later builds are mostly cache hits.)
Then verify the output handoff:
```bash
ls -R ~/matter-output/apps ~/matter-output/wheels
cat ~/matter-output/build-info.json
cat ~/matter-output/build_status.json
# CRUCIAL — binaries must be Linux ARM64, not macOS:
file ~/matter-output/apps/*        # -> "ELF 64-bit LSB ... ARM aarch64"
file ~/matter-output/chip-tool     # -> ELF aarch64
```
If `file` says `Mach-O` you built for macOS — check `--platform linux/arm64`.

## 7. Register the Mac mini as a self-hosted runner (label `mac-mini`)

GitHub → repo **Settings → Actions → Runners → New self-hosted runner → macOS / ARM64**.
Follow the download commands it shows, then configure **with the label**:
```bash
cd ~/actions-runner
./config.sh --url https://github.com/KishokG/Matter_CHIP \
            --token <TOKEN_FROM_GITHUB> \
            --labels mac-mini
# run as a login service so it survives reboots (Docker Desktop must auto-start):
./svc.sh install
./svc.sh start
```
Confirm it shows **Idle** with label `mac-mini` in Settings → Actions → Runners.

> The runner must see `docker` on its PATH. If `docker` isn't found when the
> service runs, ensure Docker Desktop starts on login and re-`./svc.sh start`.

## 8. Label the existing RPi runner `rpi`

The test jobs target `[self-hosted, rpi]`. The RPi runner needs the `rpi` label.
Easiest: on the RPi, re-configure the runner with the label:
```bash
cd ~/actions-runner
sudo ./svc.sh stop && sudo ./svc.sh uninstall
./config.sh remove --token <TOKEN>          # get token from GitHub Runners page
./config.sh --url https://github.com/KishokG/Matter_CHIP \
            --token <TOKEN> --labels rpi
sudo ./svc.sh install && sudo ./svc.sh start
```
(Or add the label via the runner's page if your GitHub version allows editing labels.)
The RPi still needs a `connectedhomeip` checkout at `rpi.sdk_dir` — the prepare
step checks it out to the build commit but won't clone from scratch.

## 9. First CI run (manual, on the branch)

Scheduled (cron) runs fire from the **default branch** (`main`). You can also
trigger manually via **workflow_dispatch** (any branch):

GitHub → Actions → "Matter — Build on RPi" → **Run workflow** (branch `main`):
- **pipeline = `build-only`** first (just build + upload + email; no tests).
- Watch the **Build on Mac mini (Docker)** job → it runs the same `docker run`
  you tested in step 6, then upload + notify on the Mac mini.
- When that's green, run again with **pipeline = `build-test`** to exercise the
  RPi download → checkout → prepare → tests path.

> **`build_mode` in the container** (passed to `build_inside_container.sh --mode`):
> - `skip-all` — build the image's baked SDK commit as-is (no pull, no bootstrap). Fastest.
> - `skip-clone` *(default, incl. nightly)* — `git pull` latest + clean + **bootstrap** + build.
> - `full` — re-clone the SDK + clean + **bootstrap** + build (ignores the baked checkout).
>
> Note: `skip-clone`/`full` re-run **bootstrap inside the container** so the env
> matches freshly-pulled code — that adds time each run and re-does work baked
> into the image. If you want the fast baked path, use `skip-all` (but it won't
> pick up new SDK commits). See "Build-mode tradeoff" below.

---

## Build-mode tradeoff (important)

The image bakes a clone + bootstrap so nightly builds *can* skip them. The three
modes let you choose per run:

| Mode | Clone | Pull | Clean | Bootstrap | Build | When |
|---|---|---|---|---|---|---|
| `skip-all` | — | — | — | — | ✅ | Fast rebuild of the exact baked commit (no new SDK code) |
| `skip-clone` | — | ✅ | ✅ | ✅ | ✅ | **Default / nightly** — latest SDK, env rebuilt to match |
| `full` | ✅ | — | ✅ | ✅ | ✅ | Escape hatch — everything fresh, ignores baked checkout |

Because `skip-clone`/`full` re-bootstrap in the container, they add bootstrap
time every run (fast on the M4, but real) and re-do what the image already
baked. That's the price of guaranteeing the env matches pulled code. If your SDK
branch rarely bumps pigweed/CIPD pins, you can instead **rebuild the image
periodically** (`build_image.sh`) and run nightlies as `skip-all` for maximum
speed — at the cost of not auto-picking-up new SDK commits between image
rebuilds. Pick per your cadence; `skip-clone` is the safe default.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: command not found` in CI | Docker Desktop not running / not on the runner's PATH. Enable auto-start, restart the runner service. |
| Image `matter-sdk-builder:master` not found | Run step 4 on the Mac mini. |
| `gn`/`ninja` not found in step 5 | Bootstrap didn't bake — rebuild the image (step 4). |
| Binaries are `Mach-O` (macOS) | Missing `--platform linux/arm64` — rebuild image + re-run. |
| chip-tool `gn gen` fails on `libpcsclite` | `apt-packages.txt` must include `libpcsclite-dev` (it does) — rebuild the image so it's installed. |
| 0 apps discovered | Check `discovery.apps` has `enabled: true` entries (see the `[INFO] discovery summary` line in the log). |
| Upload/notify import errors | On the Mac mini host: `pip3 install google-auth google-auth-httplib2 google-api-python-client pyyaml`. |
| RPi test job can't find binaries | Prepare step must run first; ensure the RPi has a `connectedhomeip` clone at `rpi.sdk_dir`. |
