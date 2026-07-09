# Matter CI — Automated Build & Test Pipeline

Nightly pipeline that **builds** the Matter SDK reference apps + chip-tool +
Python controller in a **Docker container on a Mac mini (Apple Silicon)**,
bundles the Linux/ARM64 binaries to Google Drive, and **runs the certification
test cases on a Raspberry Pi**. Triggered on a schedule (or manually) from
GitHub Actions — no SSH, no manual builds.

> **Architecture in one line:** Mac mini = *builder* (Docker), Raspberry Pi =
> *tester*. Binaries are built once on the fast M4 and shipped to the RPi.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Repository Layout](#repository-layout)
3. [How a Run Works](#how-a-run-works)
4. [Runners](#runners)
5. [First-Time Setup](#first-time-setup)
6. [Triggering a Run](#triggering-a-run)
7. [Build Modes](#build-modes)
8. [Runtime Inputs](#runtime-inputs)
9. [Configuration Reference](#configuration-reference)
10. [Choosing Which Apps to Build](#choosing-which-apps-to-build)
11. [System Dependencies & the Docker Image](#system-dependencies--the-docker-image)
12. [Build Artifacts & the Bundle](#build-artifacts--the-bundle)
13. [Troubleshooting](#troubleshooting)
14. [File Reference](#file-reference)

---

## Architecture

```
GitHub Actions (scheduled nightly, or manual "Run workflow")
      │
      ├─ validate        (ubuntu-latest, GitHub-hosted)  — validate build_config.yaml
      │
      ├─ build           ([self-hosted, mac-mini])        — Docker: build all enabled apps
      │      └─ docker run matter-sdk-builder:master → build_inside_container.sh
      │             → Linux/ARM64 binaries + wheels → ~/matter-output
      │
      ├─ upload-artifacts([self-hosted, mac-mini])        — bundle ~/matter-output → Google Drive
      ├─ notify          ([self-hosted, mac-mini])        — HTML email (build status + link)
      │
      ├─ fetch-tests     ([self-hosted, rpi])             — pull TC commands from Google Sheet
      └─ run-tests       ([self-hosted, rpi])             — download bundle, checkout SDK,
                                                            install, run the TCs
```

- **Why Docker on the Mac mini:** the SDK + `bootstrap.sh` are baked into a
  pre-built image once, so nightly builds skip the slow bootstrap. The M4 builds
  the full app set in ~45–90 min vs ~8.5 h on the RPi. Binaries are Linux/ARM64
  (built in an `ubuntu:24.04` arm64 container) so they run natively on the RPi.
- **Why the RPi still tests:** the RPi is the DUT host with the Thread/Matter
  test rig. It downloads the exact binaries the Mac mini built and runs the TCs.

---

## Repository Layout

```
Matter_CHIP/
├── Matter_CI/
│   ├── config/
│   │   ├── build_config.yaml     ← single source of truth (apps, modifiers, SDK, Drive, tests)
│   │   └── tc_list.json          ← test cases by cluster
│   ├── docker/
│   │   ├── Dockerfile            ← ubuntu:24.04 arm64 + SDK + baked bootstrap
│   │   ├── build_image.sh        ← one-time image build (run on the Mac mini)
│   │   ├── build_inside_container.sh ← the nightly build (runs INSIDE the container)
│   │   └── README.md             ← Mac mini bring-up guide (START HERE for setup)
│   ├── scripts/
│   │   ├── discover_targets.py   ← resolves apps (source_dir/binary/gn-args) from the SDK
│   │   ├── validate_config.py    ← config validator (runs on GitHub cloud)
│   │   ├── upload_artifacts.py   ← bundles ~/matter-output → Google Drive (Mac mini)
│   │   ├── notify.py             ← email notification (Mac mini)
│   │   ├── prepare_rpi_tests.py  ← RPi: download bundle → checkout SDK → install
│   │   ├── fetch_test_commands.py← RPi: pull TC commands from Google Sheet
│   │   ├── run_tests.py          ← RPi: execute the TCs
│   │   └── cleanup.sh            ← RPi: prune old test-result runs / disk check
│   ├── apt-packages.txt          ← system deps baked into the Docker image
│   └── README.md                 ← this file
│
└── .github/workflows/
    └── matter_build.yml          ← the pipeline (schedule + manual dispatch)
```

---

## How a Run Works

**Build (Mac mini, in Docker):** `build_inside_container.sh` runs inside
`matter-sdk-builder:master`:
1. **SDK prep** per `--mode` (see [Build Modes](#build-modes)) — clone/pull/nothing.
2. `source scripts/activate.sh` (the baked pigweed env).
3. **Discover** enabled apps via `discover_targets.py` (reads `discovery.apps`).
4. **Build** each app + chip-tool + python controller (`gn_build_example.sh` /
   `build_python.sh`) with live ninja progress and per-app pass/fail.
5. **Collect** binaries + wheels + `build-info.json` + `build_status.json` into
   `/output` (→ `~/matter-output` on the host).

**Upload (Mac mini):** `upload_artifacts.py` reads `~/matter-output`, tars a
bundle, and uploads it to a **single** Google Drive folder as one uniquely-named
file (`matter-sdk-<branch>-<commit>-arm64.tar.gz`). The email links to that
file's **own permanent ID**, so a link keeps working until its bundle is pruned
(newest `keep_history` kept) — a later run never invalidates it. The newest
build is flagged via its Drive description and a refreshed `LATEST.txt` pointer.

**Test (RPi):** `prepare_rpi_tests.py` downloads the latest bundle, `git
checkout`s the RPi's SDK to the **exact build commit**, places the binaries into
`sdk/out/<name>/` and installs the wheels into the `python_env` venv — so
`run_tests.py` runs **unchanged**. Then `fetch_test_commands.py` + `run_tests.py`
execute the TCs.

---

## Runners

Two self-hosted runners, matched by label:

| Runner | Labels | Runs |
|---|---|---|
| Mac mini M4 | `self-hosted, mac-mini` | build, upload-artifacts, notify |
| Raspberry Pi | `self-hosted, rpi` | fetch-tests, run-tests |

`validate` runs on GitHub-hosted `ubuntu-latest` (no runner needed). A job lands
on a runner only if the runner has **all** labels in the job's `runs-on`.

---

## First-Time Setup

**Mac mini (builder):** follow **[docker/README.md](docker/README.md)** — it's
the step-by-step bring-up (Docker Desktop → clone → `build_image.sh` → register
the `mac-mini` runner → first run). Do that first.

**Raspberry Pi (tester):** the RPi needs:
- A self-hosted runner registered with the **`rpi`** label.
- A `connectedhomeip` git checkout at `rpi.sdk_dir` (the prepare step checks it
  out to the build commit but won't clone it from scratch).
- `python3` + `pip` (the prepare step installs the Google API libs it needs).

**GitHub secrets** (Settings → Secrets and variables → Actions):
`CREDENTIALS_JSON` (Google service account — Drive + Sheets), `GMAIL_SENDER`,
`GMAIL_APP_PASSWORD`, `NOTIFY_EMAILS`. Share the Drive folder
(`google_drive.folder_id`) with the service-account email.

---

## Triggering a Run

- **Scheduled:** nightly via the `cron` in `matter_build.yml` (Mon–Fri IST).
  Scheduled runs fire only from the **default branch** (`main`).
- **Manual:** GitHub → Actions → *Matter — Build on RPi* → **Run workflow**,
  choose a `pipeline`:
  - `build-only` — build + upload + email (no tests)
  - `test-only` — fetch + run tests using the latest uploaded bundle (no build)
  - `build-test` — build, then test

---

## Build Modes

`build_mode` (manual input; scheduled runs default to `skip-clone`) is passed to
`build_inside_container.sh --mode` and controls SDK prep **inside the container**
(the image already has a baked clone + bootstrap):

| Mode | Clone | Pull | Clean | Bootstrap | When |
|---|---|---|---|---|---|
| `skip-all` | — | — | — | — | Build the image's baked SDK commit as-is (fastest; no new SDK code) |
| `skip-clone` | — | ✅ | ✅ | ✅ | **Default/nightly** — latest SDK, env rebuilt to match |
| `full` | ✅ | — | ✅ | ✅ | Everything fresh; ignores the baked checkout |

> The container is `docker run --rm` (ephemeral), so `out/` does **not** persist
> between runs — every run compiles from scratch regardless of mode. `skip-all`
> only saves the pull+bootstrap time, not compilation. (A persistent ccache
> mount is the way to speed repeat compiles — not yet enabled.)

---

## Runtime Inputs

| Input | Description | Default |
|---|---|---|
| `pipeline` | `build-only` / `test-only` / `build-test` | `build-only` |
| `build_mode` | `full` / `skip-clone` / `skip-all` (see above) | `skip-clone` |
| `sdk_branch` | Override SDK branch. Empty = config | `""` |
| `sdk_sha` | Pin SDK commit. Empty = branch HEAD | `""` |
| `target_apps` | Comma-separated SDK shorthands to build (enables exactly these in `discovery.apps`, disables the rest). Empty = config | `""` |
| `cluster_filter` / `tc_filter` | Narrow which TCs run | `""` |

---

## Configuration Reference

`config/build_config.yaml` is the single source of truth. Key sections:

```yaml
sdk:
  repo: "https://github.com/project-chip/connectedhomeip.git"
  branch: "master"          # image is built for this branch; container pulls it
  sha: ""                    # pin a commit (optional)

discovery:                   # WHICH apps to build (see next section)
  apps:
    - { name: all-clusters, enabled: true, modifiers: [ipv6only] }   # → chip-all-clusters-app
    # ... ~34 apps; flip `enabled`; `modifiers` become gn args

chip_tool:                   # built separately (tests need it) — matches TH target
  enabled: true
  source_dir: "examples/chip-tool"
  build_dir: "out/chip-tool"
  binary_name: "chip-tool"
  extra_gn_args: 'chip_mdns="platform" chip_inet_config_enable_ipv4=false chip_enable_nfc_based_commissioning=true chip_device_config_enable_thread_meshcop=true'

python_controller:           # matches TH build_python.sh flags
  enabled: true
  install_venv_name: "python_env"
  extra_args: "--enable_nfc true --enable_thread_meshcop true"

google_drive:
  folder_id: "<Drive folder id, shared with the service account>"
  keep_history: 10           # newest N bundles kept = how long an email link lives
  upload_on_partial: true    # upload the apps that built even if some failed

rpi:
  sdk_dir: "/home/ubuntu/connectedhomeip"   # RPi SDK checkout (test scripts + binary drop target)
```

---

## Choosing Which Apps to Build

Apps are **discovered from the SDK** — there's no hardcoded source/binary list.
`discovery.apps` enumerates every buildable reference app; you flip `enabled`,
and each app's `modifiers` become gn args that **mirror the Matter Test Harness**
(`chip-cert-bins`), so binaries build the way the TH builds them:

| modifier | gn arg |
|---|---|
| `ipv6only` | `chip_inet_config_enable_ipv4=false` (every app) |
| `platform-mdns` | `chip_mdns="platform"` (chip-tool) |
| `nfc-commission` | `chip_enable_nfc_based_commissioning=true` (chip-tool) |
| `rpc` | `import("//with_pw_rpc.gni")` (fabric-admin/bridge) |
| `clang` | `is_clang=true` (camera) |
| `no-werror` | `treat_warnings_as_errors=false` — **local escape hatch** for apps that fail only on an upstream `-Werror` warning (e.g. refrigerator). Not a TH modifier; don't use on core cert apps. |

**To enable an app:** flip its `enabled: true`. **To regenerate the full menu**
(after an SDK bump adds apps):
```bash
python3 Matter_CI/scripts/discover_targets.py --sdk-dir <SDK> --emit-config-apps
```

`source_dir` and `binary_name` are resolved from the SDK's `HostApp.ExamplePath()`
(source folder) and the app's `BUILD.gn` executable (actual output name), so
collect/upload/test all agree with the real built file. A "green ninja" that
produced no binary is reported as an honest **FAIL** (not a silent PASS).

Recommendation: keep `discovery.apps` trimmed to your actual cert set. Enabling
all ~34 surfaces niche-app quirks (extra deps, upstream compile bugs) that don't
matter for certification.

---

## System Dependencies & the Docker Image

`apt-packages.txt` is the system-dependency list **baked into the Docker image**
(installed at image build time). It also documents camera's build deps
(ffmpeg + gstreamer + curl `-dev`) and NFC's `libpcsclite-dev` + `pcscd`.

**Changing `apt-packages.txt` requires rebuilding the image** (it's baked, not
mounted). On the Mac mini:
```bash
bash ./Matter_CI/docker/build_image.sh master
```
By contrast, `build_config.yaml` and the `scripts/` are **mounted read-only**
into the container at run time (`-v .../Matter_CI:/matter-ci:ro`), so config /
script changes take effect on the next run with **no rebuild**.

> Rebuild the image only when: `apt-packages.txt` changes, or you want a fresh
> SDK bootstrap. Day-to-day SDK updates happen via `git pull` inside the container.

---

## Build Artifacts & the Bundle

Each successful build produces a bundle on Google Drive:
```
matter-sdk-<branch>-<commit>-arm64.tar.gz
├── apps/           ← reference-app binaries (Linux/ARM64)
├── chip-tool
├── wheels/         ← python controller wheels
├── build-info.json ← branch, full commit, date (RPi uses this to checkout the SDK)
├── build-info.txt
├── README.txt      ← manual-use guide
└── install.sh      ← manual-use setup (creates a chip_env venv + installs wheels)
```
Uploaded to `google_drive.folder_id` as one uniquely-named file per build (last
`keep_history` kept; a `LATEST.txt` pointer + the newest file's Drive
description flag the current build). The GitHub Actions run also uploads
`build_logs/` + `build_status.json` as a run artifact. The RPi test job picks
the newest bundle in the folder automatically (`prepare_rpi_tests.py`).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: command not found` on the build job | Docker Desktop not running / not on the runner's PATH. Enable "start on login", restart the runner service. |
| `Docker image matter-sdk-builder:master not found` | Build it once: `bash ./Matter_CI/docker/build_image.sh master`. |
| App fails: `MISSING PKG-CONFIG LIB` (e.g. libavformat, gstreamer-1.0) | Add the `-dev` package to `apt-packages.txt` **and rebuild the image**. |
| App fails: `-Werror` on an ignored `[[nodiscard]]` (upstream code) | Add the `no-werror` modifier to that app, or disable it. |
| 0 apps discovered | Check `discovery.apps` has `enabled: true` entries — see the `[INFO] discovery summary` line in the build log. |
| Upload skipped / no bundle | If `upload_on_partial: false`, any app failing blocks the upload. Set `true`, or trim the app set. |
| Upload/notify: `No module named yaml` / Google libs | Host-side deps — the jobs `pip install` them; ensure the Mac mini has `python3`/`pip`. |
| Upload: `No app binaries found` | The build job's `~/matter-output` and the upload job must share the runner's `$HOME` (same mac-mini runner — they do). |
| RPi tests can't find binaries | The `Prepare RPi` step must run first; the RPi needs a `connectedhomeip` clone at `rpi.sdk_dir`. |
| Binary is `Mach-O` not `ELF aarch64` | Missing `--platform linux/arm64` — rebuild the image. |
| `activate.sh` unbound variable | The build sources it with `set +u`; if it still fails, the image bootstrap is stale — rebuild. |

### Runner operations (both `mac-mini` and `rpi`)

These apply to either self-hosted runner. On the **RPi** the runner is a systemd
service → use `sudo` + `journalctl`; on the **Mac mini** it's a launchd service
→ no `sudo`, and use the `_diag/` logs (macOS has no `journalctl`).

**Runner shows Offline / not picking up jobs**
```bash
cd ~/actions-runner
sudo ./svc.sh status      # RPi   (drop 'sudo' on the Mac mini)
sudo ./svc.sh stop && sudo ./svc.sh start
# recent runner logs:
tail -100 ~/actions-runner/_diag/Runner_*.log
journalctl -u 'actions.runner.*' -f    # RPi only
```
If it's Idle but a job never starts, the **labels don't match** — the runner
must have *all* labels in the job's `runs-on` (`mac-mini` for build/upload/notify,
`rpi` for the test jobs).

**Re-register a runner (token expired / runner broken)**
```bash
cd ~/actions-runner
sudo ./svc.sh stop && sudo ./svc.sh uninstall     # drop 'sudo' on the Mac mini
./config.sh remove --token OLD_TOKEN              # or skip if the token expired
# New token: GitHub → Settings → Actions → Runners → New self-hosted runner
./config.sh --url https://github.com/KishokG/Matter_CHIP \
            --token NEW_TOKEN --labels mac-mini    # or: --labels rpi
sudo ./svc.sh install && sudo ./svc.sh start       # drop 'sudo' on the Mac mini
```

> **Memory:** on the **Mac mini**, build OOM is a Docker Desktop limit (Settings →
> Resources → raise Memory), *not* host swap. The **RPi** only runs tests now, so
> the old "add 8 GB swap for the build" step no longer applies to it.

---

## File Reference

| File | Runs on | Purpose |
|---|---|---|
| `config/build_config.yaml` | — | Single source of truth (apps, modifiers, SDK, Drive, tests) |
| `docker/Dockerfile` | Mac mini (image build) | ubuntu:24.04 arm64 + SDK + baked bootstrap |
| `docker/build_image.sh` | Mac mini | One-time image build helper |
| `docker/build_inside_container.sh` | container | The nightly build (SDK prep → discover → build → collect) |
| `scripts/discover_targets.py` | container / RPi | Resolve apps (source/binary/gn-args) from the SDK |
| `scripts/upload_artifacts.py` | Mac mini | Bundle `~/matter-output` → Google Drive |
| `scripts/notify.py` | Mac mini | HTML email notification |
| `scripts/prepare_rpi_tests.py` | RPi | Download bundle → checkout SDK → place binaries + install wheels |
| `scripts/fetch_test_commands.py` | RPi | Pull TC commands from Google Sheet |
| `scripts/run_tests.py` | RPi | Execute the TCs |
| `scripts/cleanup.sh` | RPi | Prune old test-result runs + disk-space check |
| `scripts/validate_config.py` | GitHub cloud | Validate `build_config.yaml` |
| `apt-packages.txt` | Mac mini (image) | System deps baked into the image |
| `.github/workflows/matter_build.yml` | — | The pipeline definition |
