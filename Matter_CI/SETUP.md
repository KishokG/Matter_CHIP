# Matter CI — Setup Instructions

Setup for the split pipeline: **Mac mini builds (Docker), Raspberry Pi tests.**
This is the entry point — it points you to the detailed guides and lists what
each machine + GitHub needs. For the full architecture see
[README.md](README.md).

| Machine | Role | Runner label |
|---|---|---|
| Mac mini M4 (Apple Silicon) | Builder — Docker container builds the apps | `mac-mini` |
| Raspberry Pi (Ubuntu ARM64) | Tester — runs the certification TCs | `rpi` |

---

## 1. Mac mini (builder) — see docker/README.md

The Mac mini setup is fully covered in **[docker/README.md](docker/README.md)**:
Docker Desktop → clone the repo → `build_image.sh` (build the image once) →
pre-test the container build → register the `mac-mini` self-hosted runner. Do
that guide first.

Summary of what it sets up:
- Docker Desktop (Apple chip) with `linux/arm64`, generous CPU/RAM.
- The `matter-sdk-builder:master` image (SDK + baked bootstrap).
- A self-hosted runner labelled **`mac-mini`** (runs build + upload + notify).
- Host `python3`/`pip` for the upload/notify jobs.

Rebuild the image (`build_image.sh`) only when `apt-packages.txt` changes or the
SDK needs a fresh bootstrap — otherwise the nightly `git pull` inside the
container keeps the SDK current with no rebuild.

---

## 2. Raspberry Pi (tester)

The RPi no longer builds — it downloads the Mac mini's bundle and runs the TCs.
It needs:

**2.1 System basics**
```bash
sudo apt update && sudo apt install -y git curl python3 python3-pip python3-venv python3-yaml
```

**2.2 A connectedhomeip checkout** at the path in `build_config.yaml`
(`rpi.sdk_dir`, e.g. `/home/ubuntu/connectedhomeip`). The test prep step
(`prepare_rpi_tests.py`) checks this out to the exact build commit but will NOT
clone it from scratch:
```bash
git clone https://github.com/project-chip/connectedhomeip.git /home/ubuntu/connectedhomeip
```

**2.3 A self-hosted runner labelled `rpi`**
```
GitHub → Settings → Actions → Runners → New self-hosted runner → Linux / ARM64
```
```bash
cd ~/actions-runner
./config.sh --url https://github.com/KishokG/Matter_CHIP \
            --token <TOKEN> --labels rpi
sudo ./svc.sh install && sudo ./svc.sh start
```
Confirm it shows **Idle** with the `rpi` label.

> If you test the **camera** app, install its RUNTIME libs on the RPi once —
> otherwise `chip-camera-app` dies at launch with *"error while loading shared
> libraries … No such file or directory"* (rc=127):
> ```bash
> sudo apt-get install -y ffmpeg libcurl4 \
>   libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
>   gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
>   gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav
> sudo usermod -aG video "$USER"   # camera device access
> ```
> Verify nothing is missing: `ldd out/camera/chip-camera-app | grep 'not found'`
> (empty output = good). These are separate from the build-time `-dev` packages.

---

## 3. GitHub configuration

**Secrets** (Settings → Secrets and variables → Actions):

| Secret | Used by | What |
|---|---|---|
| `CREDENTIALS_JSON` | upload, notify, RPi prep, fetch-tests | Google service-account JSON (Drive + Sheets) |
| `GMAIL_SENDER` | notify | Gmail address that sends the email |
| `GMAIL_APP_PASSWORD` | notify | Gmail app password |
| `NOTIFY_EMAILS` | notify | Comma-separated recipients |

Share the Google Drive folder (`google_drive.folder_id` in `build_config.yaml`)
with the **service-account email** so uploads/downloads work.

**Configure `config/build_config.yaml`:**
```yaml
sdk:
  branch: "master"          # image is built for this branch
discovery:
  apps:                     # flip enabled per app; modifiers = TH gn args
    - { name: all-clusters, enabled: true, modifiers: [ipv6only] }
    # ...
google_drive:
  folder_id: "<your Drive folder id>"
rpi:
  sdk_dir: "/home/ubuntu/connectedhomeip"
```
Validate it (also runs automatically in CI): 
```bash
python3 scripts/validate_config.py config/build_config.yaml
```

---

## 4. Run the pipeline

- **Scheduled:** nightly cron in `matter_build.yml` (fires from the default
  branch `main`).
- **Manual:** GitHub → Actions → *Matter — Build on RPi* → **Run workflow**:
  - `pipeline = build-only` → build + upload + email
  - `pipeline = build-test` → build, then run TCs on the RPi
  - `pipeline = test-only` → run TCs against the latest uploaded bundle
  - `build_mode` (build only): `skip-clone` (default), `skip-all` (baked SDK),
    `full` (fresh clone). See [README.md → Build Modes](README.md#build-modes).

First manual run recommendation: `build-only` to confirm build + upload + email,
then `build-test` to exercise the RPi download → checkout → install → test path.

---

## 5. Interpreting results

- **Email** (from `notify.py`): status (success / partial / failed), which apps
  built, and the Drive download link.
- **GitHub run artifact** `build-summary-<n>`: `build_logs/`, `build_status.json`,
  `build-info.json`.
- **Test results** (`run-tests` job): `report.html`, `test_results.json`,
  uploaded as `test-results-<n>` and saved on the RPi under
  `~/matter-ci-results/`.

For failures, see [README.md → Troubleshooting](README.md#troubleshooting).

---

## Quick reference — file roles

See [README.md → File Reference](README.md#file-reference) for the full table.
Single source of truth for everything (apps, modifiers, SDK, Drive, tests):
**`config/build_config.yaml`**.
