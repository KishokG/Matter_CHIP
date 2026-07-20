#!/usr/bin/env python3
"""
prepare_rpi_tests.py — runs ON the Raspberry Pi before the test jobs.
=====================================================================
Bridges the Docker-on-Mac-mini build to the RPi test runner. It makes the RPi
look exactly like it did when it built locally, so run_tests.py /
fetch_test_commands.py stay UNCHANGED (they still read binaries from
sdk_dir/out/<name> and use the sdk_dir/<python_env> venv).

Steps:
  1. Download the newest bundle (matter-sdk-*.tar.gz) from the single Google
     Drive folder (google_drive.folder_id) using the service account — same
     creds/folder as upload_artifacts.py.
  2. Extract it.
  3. git fetch + checkout the RPi SDK to the EXACT commit the binaries were
     built from (from the bundle's build-info.json) — so the python_testing
     test scripts match the built SDK.
  4. Symlink the bundle's app binaries + chip-tool into sdk_dir/out/<name>/...
     (not copied — saves ~5-6 min of flash writes + avoids duplicating GBs) so
     run_tests.py finds them where it expects.
  5. Install the bundle's python wheels (+ mobly/click/colorama/pyserial) into
     the sdk_dir/<python_env> venv that run_tests.py uses.

Usage (workflow sets GSHEET_SA_KEY_PATH to the service_account.json):
  python3 scripts/prepare_rpi_tests.py --config config/build_config.yaml
"""

import os
import sys
import json
import shutil
import tarfile
import argparse
import subprocess
from pathlib import Path

import yaml

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent

# Resolve app source_dir/build_dir/binary_name from the SDK (same as the build).
sys.path.insert(0, str(SCRIPT_DIR))
from discover_targets import resolve_pipeline_apps

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gapi_build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:
    print("[ERROR] Missing Google API libs. Run:")
    print("  pip3 install google-auth google-api-python-client --break-system-packages")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Extra runtime deps run_tests.py needs (mirrors the bundle's install.sh).
TEST_PIP_DEPS = ["mobly", "click", "colorama", "pyserial"]


def log(msg):  print(f"[PREP] {msg}")
def die(msg):  print(f"[PREP] ❌ {msg}", file=sys.stderr); sys.exit(1)


# ── Google Drive download ────────────────────────────────────────────────────
def drive_service(sa_key: str):
    creds = service_account.Credentials.from_service_account_file(sa_key, scopes=SCOPES)
    return gapi_build("drive", "v3", credentials=creds)


def latest_bundle(service, folder_id: str) -> dict:
    """Newest BUILD bundle (matter-sdk-*.tar.gz) in the Drive folder (by createdTime).

    Must match build bundles ONLY. Test-result archives (matter-ci-results-*.tar.gz,
    uploaded by upload_test_results.py) may share this folder — selecting one by
    mistake gives a binary-less bundle and every test errors "not built". Drive's
    query language has no NOT-contains, so we over-fetch .tar.gz then filter by name.
    """
    files = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and name contains '.tar.gz'",
        fields="files(id,name,createdTime,size)",
        orderBy="createdTime desc",
    ).execute().get("files", [])
    bundles = [f for f in files
               if f["name"].startswith("matter-sdk-")
               and not f["name"].startswith("matter-ci-results-")]
    if not bundles:
        die(f"No build bundle (matter-sdk-*.tar.gz) found in Drive folder {folder_id}")
    return bundles[0]


def download_file(service, file_id: str, dest: Path):
    from io import FileIO
    request = service.files().get_media(fileId=file_id)
    with FileIO(str(dest), "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"[PREP]   downloading... {int(status.progress()*100)}%", end="\r")
    print()


def download_and_extract(cfg: dict, workdir: Path) -> Path:
    gd = cfg.get("google_drive", {})
    folder_id = gd.get("folder_id", "")
    if not folder_id or folder_id == "YOUR_GDRIVE_FOLDER_ID_HERE":
        die("google_drive.folder_id not set in build_config.yaml")

    sa_key = os.environ.get("GSHEET_SA_KEY_PATH",
                            str(PROJECT_ROOT / "config" / "service_account.json"))
    if not Path(sa_key).exists():
        die(f"Service account key not found: {sa_key}")

    log("Connecting to Google Drive...")
    service = drive_service(sa_key)
    meta = latest_bundle(service, folder_id)
    log(f"Latest bundle: {meta['name']} ({int(meta.get('size',0))/1_048_576:.1f} MB)")

    workdir.mkdir(parents=True, exist_ok=True)

    # Purge previous runs' bundles so the workdir can't grow unbounded (each run
    # downloads a uniquely-named tar; without this they pile up and exhaust disk).
    old_tars = sorted(workdir.glob("matter-sdk-*.tar.gz"))
    for old in old_tars:
        try:
            old.unlink()
            log(f"Removed old bundle tar: {old.name}")
        except OSError as e:
            log(f"⚠️  Could not remove {old.name}: {e}")

    tar_path = workdir / meta["name"]
    download_file(service, meta["id"], tar_path)
    log(f"Downloaded → {tar_path}")

    extract_root = workdir / "extracted"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True)
    with tarfile.open(tar_path, "r:gz") as t:
        t.extractall(extract_root)

    # The tar is dead weight once extracted — drop it (binaries are symlinked
    # from the extracted dir, which persists through the test run).
    try:
        tar_path.unlink()
    except OSError:
        pass

    # The tar contains a single top-level dir (matter-sdk-...); return it.
    subdirs = [d for d in extract_root.iterdir() if d.is_dir()]
    bundle_dir = subdirs[0] if len(subdirs) == 1 else extract_root
    log(f"Extracted → {bundle_dir}")

    # Surface the build metadata at a stable path so the test report + the
    # workflow's test summary can show WHICH commit the DUTs were built at.
    src_info = bundle_dir / "build-info.json"
    if src_info.exists():
        dst_info = PROJECT_ROOT / "logs" / "build-info.json"
        dst_info.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_info, dst_info)
        log(f"Build info → {dst_info}")

    return bundle_dir


# ── SDK checkout ─────────────────────────────────────────────────────────────
def read_commit(bundle_dir: Path) -> str:
    info = bundle_dir / "build-info.json"
    if info.exists():
        try:
            return json.loads(info.read_text()).get("commit", "")
        except Exception:
            pass
    # Fallback: parse build-info.txt "Commit : <sha>"
    txt = bundle_dir / "build-info.txt"
    if txt.exists():
        for line in txt.read_text().splitlines():
            if line.lower().startswith("commit"):
                return line.split(":", 1)[1].strip()
    return ""


def _current_head(sdk_dir: Path) -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=sdk_dir,
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def checkout_sdk(sdk_dir: Path, commit: str):
    if not (sdk_dir / ".git").exists():
        die(f"SDK not found at {sdk_dir} — the RPi needs a connectedhomeip checkout "
            f"for the test scripts (rpi.sdk_dir in build_config.yaml).")
    if not commit:
        log("⚠️  No commit in bundle — leaving SDK at its current checkout.")
        return

    # Fast-path: already at the build commit (e.g. a workflow re-run) → nothing
    # to fetch/checkout.
    head = _current_head(sdk_dir)
    if head and (head == commit or head.startswith(commit) or commit.startswith(head)):
        log(f"SDK already at build commit {commit[:9]} — skipping fetch/checkout.")
        return

    log(f"Checking out SDK to build commit {commit[:9]} ...")
    subprocess.run(["git", "fetch", "--tags", "origin"], cwd=sdk_dir, check=False)
    r = subprocess.run(["git", "checkout", "-f", commit], cwd=sdk_dir,
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"git checkout {commit} failed:\n{r.stderr}\n"
            f"Ensure the RPi SDK remote has this commit (git fetch).")
    # NOTE: submodules are intentionally NOT checked out here. The RPi only RUNS
    # the python tests — it never compiles the SDK. The test scripts
    # (src/python_testing) + PICS files come from the main repo (synced by the
    # checkout above), the chip/matter python modules come from the installed
    # wheels, and the DUT binaries are prebuilt in the bundle. The third_party/*
    # submodules (pigweed, openthread, boringssl, …) are C++ BUILD deps only, so
    # fetching them cost ~20+ min/run for nothing.
    log(f"SDK now at {_current_head(sdk_dir)[:10]}")


# ── Place binaries where run_tests.py expects ────────────────────────────────
def _symlink_binary(src: Path, dst: Path):
    """
    Point sdk_dir/out/<name>/<binary> at the extracted bundle file via a symlink
    instead of copying ~hundreds of MB per app onto slow RPi flash (28 copies
    took ~5-6 min; symlinks are instant and avoid duplicating the bytes). The
    extracted bundle dir persists through the test run, so the link stays valid.
    run_tests.py opens the same path and the OS follows the link transparently.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Remove any pre-existing file/symlink at the destination (e.g. left over
    # from an older copy-based run) so os.symlink can't fail with FileExists.
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    src.chmod(0o755)
    os.symlink(src.resolve(), dst)


def place_binaries(cfg: dict, sdk_dir: Path, bundle_dir: Path):
    apps_src = bundle_dir / "apps"
    placed = 0
    for app in resolve_pipeline_apps(sdk_dir, cfg):
        if not app.get("enabled"):
            continue
        src = apps_src / app["binary_name"]
        if not src.exists():
            log(f"⚠️  {app['binary_name']} not in bundle — skipping")
            continue
        dst = sdk_dir / app["build_dir"] / app["binary_name"]
        _symlink_binary(src, dst)
        placed += 1
        log(f"binary → {dst}")

    # chip-tool
    ct = cfg.get("chip_tool", {})
    if ct.get("enabled"):
        src = bundle_dir / "chip-tool"
        if src.exists():
            dst = sdk_dir / ct["build_dir"] / ct["binary_name"]
            _symlink_binary(src, dst)
            log(f"binary → {dst}")
        else:
            log("⚠️  chip-tool not in bundle")
    log(f"Linked {placed} app binary(ies) into the SDK out/ tree.")


# ── Install wheels into the venv run_tests.py uses ───────────────────────────
def install_wheels(cfg: dict, sdk_dir: Path, bundle_dir: Path):
    venv_name = cfg.get("python_controller", {}).get("install_venv_name", "python_env")
    venv = sdk_dir / venv_name
    py = venv / "bin" / "python3"
    if not py.exists():
        log(f"Creating venv {venv} ...")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"], check=False)

    wheels = sorted((bundle_dir / "wheels").glob("*.whl"))
    if wheels:
        log(f"Installing {len(wheels)} wheel(s) into {venv} ...")
        subprocess.run([str(py), "-m", "pip", "install", *[str(w) for w in wheels], "--quiet"], check=True)
    else:
        log("⚠️  No wheels found in bundle — python controller modules may be missing.")

    # Install the SDK's OWN python_testing requirements — the authoritative,
    # evolving list of per-test-case deps (pycountry, validators, zeroconf, …).
    # A hardcoded list can't keep up as upstream TCs add new imports; the SDK is
    # checked out at the exact build commit, so this file is version-matched.
    # requirements.nfc.txt too, since we build chip-tool/controller with NFC.
    req_files = [
        sdk_dir / "src" / "python_testing" / "requirements.txt",
        sdk_dir / "src" / "python_testing" / "requirements.nfc.txt",
    ]
    installed_any = False
    for req in req_files:
        if req.exists():
            log(f"Installing SDK test requirements: {req.relative_to(sdk_dir)} ...")
            subprocess.run([str(py), "-m", "pip", "install", "-r", str(req), "--quiet"],
                           check=False)
            installed_any = True
    if not installed_any:
        log("⚠️  No src/python_testing/requirements*.txt in SDK — falling back to "
            "the built-in dep list only (some TCs may miss imports).")

    # Safety net: a few runner/CLI deps not always in the SDK requirements.
    log(f"Installing extra runner deps: {', '.join(TEST_PIP_DEPS)} ...")
    subprocess.run([str(py), "-m", "pip", "install", *TEST_PIP_DEPS, "--quiet"], check=False)

    setup_push_av_server(py, sdk_dir)
    log("Python env ready.")


def setup_push_av_server(py: Path, sdk_dir: Path):
    """
    Camera Push-AV tests (TC-PAVST-*/TC-AVSM-*) launch src/tools/push_av_server/
    src/server.py IN THIS VENV and wait for 'Running on https://0.0.0.0:1234'.
    That server has its OWN deps (fastapi, Hypercorn, aioquic, …) AND needs a
    patch to hypercorn (TLS-ASGI extension) — without them it never starts and
    setup_class times out. Install + patch here (idempotent).
    """
    pav = sdk_dir / "src" / "tools" / "push_av_server"
    req = pav / "requirements.txt"
    if not req.exists():
        return
    log(f"Installing push-av-server requirements: {req.relative_to(sdk_dir)} ...")
    subprocess.run([str(py), "-m", "pip", "install", "-r", str(req), "--quiet"], check=False)

    patch = pav / "hypercorn.patch"
    if not patch.exists():
        return
    # Apply the patch in hypercorn's install location (README uses `patch -d
    # <Location>` at -p0). --forward makes it a no-op if already applied.
    show = subprocess.run([str(py), "-m", "pip", "show", "hypercorn"],
                          capture_output=True, text=True)
    loc = ""
    for line in show.stdout.splitlines():
        if line.startswith("Location:"):
            loc = line.split(":", 1)[1].strip()
            break
    if not loc:
        log("⚠️  hypercorn not installed — cannot apply TLS patch (PAVST may fail).")
        return
    r = subprocess.run(f"patch -d '{loc}' -p0 --forward < '{patch}'",
                       shell=True, capture_output=True, text=True)
    out = (r.stdout + r.stderr).lower()
    if r.returncode == 0:
        log("Applied hypercorn TLS patch.")
    elif "previously applied" in out or "already applied" in out or "ignoring" in out:
        log("hypercorn TLS patch already applied.")
    else:
        log(f"⚠️  hypercorn patch may have failed: {r.stdout.strip()[:200]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    ap.add_argument("--workdir", default=str(PROJECT_ROOT / "logs" / "test_bundle"),
                    help="Where to download + extract the bundle")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    sdk_dir = Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))

    print("=" * 70)
    print("  Prepare RPi for tests (download bundle → checkout SDK → install)")
    print("=" * 70)
    log(f"SDK dir : {sdk_dir}")

    bundle_dir = download_and_extract(cfg, Path(args.workdir))
    commit = read_commit(bundle_dir)
    checkout_sdk(sdk_dir, commit)
    place_binaries(cfg, sdk_dir, bundle_dir)
    install_wheels(cfg, sdk_dir, bundle_dir)

    print("=" * 70)
    log("✅ RPi prepared. run_tests.py can now run unchanged.")
    print("=" * 70)


if __name__ == "__main__":
    main()
