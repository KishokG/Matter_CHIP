#!/usr/bin/env python3
"""
prepare_rpi_tests.py — runs ON the Raspberry Pi before the test jobs.
=====================================================================
Bridges the Docker-on-Mac-mini build to the RPi test runner. It makes the RPi
look exactly like it did when it built locally, so run_tests.py /
fetch_test_commands.py stay UNCHANGED (they still read binaries from
sdk_dir/out/<name> and use the sdk_dir/<python_env> venv).

Steps:
  1. Download the latest bundle (matter-sdk-*.tar.gz) from Google Drive
     (root_folder_id/latest/) using the service account — same creds/folder
     as upload_artifacts.py.
  2. Extract it.
  3. git fetch + checkout the RPi SDK to the EXACT commit the binaries were
     built from (from the bundle's build-info.json) — so the python_testing
     test scripts match the built SDK.
  4. Copy the bundle's app binaries + chip-tool into sdk_dir/out/<name>/... so
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


def find_subfolder(service, name: str, parent_id: str) -> str:
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed=false")
    files = service.files().list(q=q, fields="files(id,name)").execute().get("files", [])
    return files[0]["id"] if files else ""


def latest_bundle(service, folder_id: str) -> dict:
    """Newest matter-sdk-*.tar.gz in root/latest/ (fallback: the root folder)."""
    latest_id = find_subfolder(service, "latest", folder_id) or folder_id
    files = service.files().list(
        q=f"'{latest_id}' in parents and trashed=false and name contains '.tar.gz'",
        fields="files(id,name,createdTime,size)",
        orderBy="createdTime desc",
    ).execute().get("files", [])
    if not files:
        die(f"No .tar.gz bundle found in Drive folder {latest_id}")
    return files[0]


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
    tar_path = workdir / meta["name"]
    download_file(service, meta["id"], tar_path)
    log(f"Downloaded → {tar_path}")

    extract_root = workdir / "extracted"
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True)
    with tarfile.open(tar_path, "r:gz") as t:
        t.extractall(extract_root)

    # The tar contains a single top-level dir (matter-sdk-...); return it.
    subdirs = [d for d in extract_root.iterdir() if d.is_dir()]
    bundle_dir = subdirs[0] if len(subdirs) == 1 else extract_root
    log(f"Extracted → {bundle_dir}")
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


def checkout_sdk(sdk_dir: Path, commit: str):
    if not (sdk_dir / ".git").exists():
        die(f"SDK not found at {sdk_dir} — the RPi needs a connectedhomeip checkout "
            f"for the test scripts (rpi.sdk_dir in build_config.yaml).")
    if not commit:
        log("⚠️  No commit in bundle — leaving SDK at its current checkout.")
        return
    log(f"Checking out SDK to build commit {commit[:9]} ...")
    subprocess.run(["git", "fetch", "--tags", "origin"], cwd=sdk_dir, check=False)
    r = subprocess.run(["git", "checkout", "-f", commit], cwd=sdk_dir,
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"git checkout {commit} failed:\n{r.stderr}\n"
            f"Ensure the RPi SDK remote has this commit (git fetch).")
    # Sync submodules to match (test scripts may depend on them).
    subprocess.run(["python3", "scripts/checkout_submodules.py", "--platform", "linux",
                    "--shallow", "--recursive", "--allow-changing-global-git-config"],
                   cwd=sdk_dir, check=False)
    log(f"SDK now at {subprocess.run(['git','rev-parse','--short','HEAD'], cwd=sdk_dir, capture_output=True, text=True).stdout.strip()}")


# ── Place binaries where run_tests.py expects ────────────────────────────────
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
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        dst.chmod(0o755)
        placed += 1
        log(f"binary → {dst}")

    # chip-tool
    ct = cfg.get("chip_tool", {})
    if ct.get("enabled"):
        src = bundle_dir / "chip-tool"
        if src.exists():
            dst = sdk_dir / ct["build_dir"] / ct["binary_name"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst); dst.chmod(0o755)
            log(f"binary → {dst}")
        else:
            log("⚠️  chip-tool not in bundle")
    log(f"Placed {placed} app binary(ies) into the SDK out/ tree.")


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

    log(f"Installing test deps: {', '.join(TEST_PIP_DEPS)} ...")
    subprocess.run([str(py), "-m", "pip", "install", *TEST_PIP_DEPS, "--quiet"], check=False)
    log("Python env ready.")


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
