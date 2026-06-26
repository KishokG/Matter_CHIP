#!/usr/bin/env python3
"""
upload_artifacts.py
===================
Bundles built Matter SDK artifacts and uploads to Google Drive.

Bundle contents:
  matter-sdk-<branch>-<sha>-arm64/
  ├── apps/                  ← all enabled reference app binaries
  ├── chip-tool              ← chip-tool binary
  ├── wheels/                ← python controller wheels
  │   ├── matter_core-*.whl
  │   ├── matter_clusters-*.whl
  │   ├── matter_testing-*.whl
  │   └── matter_yamltests-*.whl
  └── build-info.txt         ← branch, commit, date, platform

Upload structure on Google Drive:
  Matter-CI-Builds/
  ├── latest/                ← always overwritten with newest build
  │   └── matter-sdk-*.tar.gz
  └── history/               ← last N builds kept
      └── matter-sdk-*.tar.gz

Usage:
    python3 scripts/upload_artifacts.py --config config/build_config.yaml
"""

import os
import sys
import json
import yaml
import shutil
import tarfile
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gapi_build
    from googleapiclient.http import MediaFileUpload
except ImportError:
    print("[ERROR] Missing Google API libs. Run:")
    print("  pip3 install google-auth google-auth-httplib2 google-api-python-client --break-system-packages")
    sys.exit(1)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

# =============================================================================
# Config helpers
# =============================================================================
def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

def get_sdk_dir(cfg: dict) -> Path:
    return Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))

def get_git_info(sdk_dir: Path) -> tuple[str, str]:
    def run(cmd):
        try:
            return subprocess.run(cmd, cwd=sdk_dir, capture_output=True,
                                  text=True).stdout.strip()
        except Exception:
            return "unknown"
    commit = run(["git", "rev-parse", "--short", "HEAD"])
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return commit, branch

# =============================================================================
# Bundle builder
# =============================================================================
def build_bundle(cfg: dict) -> tuple[Path, str]:
    """
    Creates a .tar.gz bundle of all built artifacts.
    Returns (tar_path, bundle_name).
    """
    sdk_dir   = get_sdk_dir(cfg)
    commit, branch = get_git_info(sdk_dir)
    date_str  = datetime.now().strftime("%Y-%m-%d")
    safe_branch = branch.replace("/", "-")

    bundle_name = f"matter-sdk-{safe_branch}-{commit}-arm64"
    bundle_dir  = PROJECT_ROOT / "logs" / "bundle" / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    print(f"[BUNDLE] Creating: {bundle_name}")
    print(f"[BUNDLE] Branch : {branch}")
    print(f"[BUNDLE] Commit : {commit}")

    # ── 1. Reference app binaries ─────────────────────────────────────────
    apps_dir = bundle_dir / "apps"
    apps_dir.mkdir(exist_ok=True)
    copied_apps = []

    for app in cfg.get("apps", []):
        if not app.get("enabled"):
            continue
        binary = sdk_dir / app["build_dir"] / app["binary_name"]
        if binary.exists():
            dest = apps_dir / app["binary_name"]
            shutil.copy2(binary, dest)
            size = binary.stat().st_size / 1_048_576
            print(f"[BUNDLE]   ✅ {app['binary_name']} ({size:.1f} MB)")
            copied_apps.append(app["binary_name"])
        else:
            print(f"[BUNDLE]   ⚠️  {app['binary_name']} not found — skipping")

    # ── 2. chip-tool ──────────────────────────────────────────────────────
    ct = cfg.get("chip_tool", {})
    if ct.get("enabled"):
        ct_binary = sdk_dir / ct["build_dir"] / ct["binary_name"]
        if ct_binary.exists():
            dest = bundle_dir / ct["binary_name"]
            shutil.copy2(ct_binary, dest)
            size = ct_binary.stat().st_size / 1_048_576
            print(f"[BUNDLE]   ✅ {ct['binary_name']} ({size:.1f} MB)")
        else:
            print(f"[BUNDLE]   ⚠️  chip-tool not found — skipping")

    # ── 3. Python wheels ──────────────────────────────────────────────────
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(exist_ok=True)

    # Known wheel locations in the SDK build output
    wheel_search_dirs = [
        sdk_dir / "out" / "python_lib" / "obj" / "src" / "controller" / "python" / "matter-controller-wheels",
        sdk_dir / "out" / "python_lib" / "obj" / "src" / "python_testing" / "matter_testing_infrastructure" / "matter-testing._build_wheel",
        sdk_dir / "out" / "python_lib" / "obj" / "scripts" / "matter_yamltests_distribution._build_wheel",
    ]

    # Target wheel names we want to bundle
    target_wheels = [
        "matter_core",
        "matter_clusters",
        "matter_repl",
        "matter_testing",
        "matter_yamltests",
    ]

    copied_wheels = []
    for search_dir in wheel_search_dirs:
        if not search_dir.exists():
            continue
        for whl in search_dir.glob("*.whl"):
            if any(t in whl.name for t in target_wheels):
                if whl.name not in [w.name for w in copied_wheels]:
                    dest = wheels_dir / whl.name
                    shutil.copy2(whl, dest)
                    size = whl.stat().st_size / 1_048_576
                    print(f"[BUNDLE]   ✅ {whl.name} ({size:.1f} MB)")
                    copied_wheels.append(whl)

    if not copied_wheels:
        print("[BUNDLE]   ⚠️  No python wheels found — check SDK build output")

    # ── 4. build-info.txt ─────────────────────────────────────────────────
    build_info = bundle_dir / "build-info.txt"
    build_info.write_text(f"""Matter SDK Build Information
=============================
Branch    : {branch}
Commit    : {commit}
Date      : {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
Platform  : linux/arm64 (Raspberry Pi)
Ubuntu    : {subprocess.run(['lsb_release','-rs'],capture_output=True,text=True).stdout.strip()}
Python    : {sys.version.split()[0]}

Apps bundled:
{chr(10).join(f'  - {a}' for a in copied_apps)}

Wheels bundled:
{chr(10).join(f'  - {w.name}' for w in copied_wheels)}

Install wheels:
  pip install wheels/*.whl --break-system-packages
""")
    print(f"[BUNDLE]   ✅ build-info.txt written")

    # ── 5. Create tar.gz ──────────────────────────────────────────────────
    tar_path = PROJECT_ROOT / "logs" / f"{bundle_name}.tar.gz"
    print(f"\n[BUNDLE] Creating archive: {tar_path.name} ...")

    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(bundle_dir, arcname=bundle_name)

    size_mb = tar_path.stat().st_size / 1_048_576
    print(f"[BUNDLE] ✅ Archive ready: {tar_path.name} ({size_mb:.1f} MB)")

    # Cleanup staging dir
    shutil.rmtree(bundle_dir)

    return tar_path, bundle_name

# =============================================================================
# Google Drive helpers
# =============================================================================
def gdrive_service(sa_key_path: str):
    creds = service_account.Credentials.from_service_account_file(
        sa_key_path, scopes=SCOPES)
    return gapi_build("drive", "v3", credentials=creds)

def get_or_create_folder(service, name: str, parent_id: str) -> str:
    """Get folder ID by name under parent, create if not exists."""
    query = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
             f"and '{parent_id}' in parents and trashed=false")
    results = service.files().list(q=query, fields="files(id,name)").execute()
    files = results.get("files", [])

    if files:
        return files[0]["id"]

    # Create folder
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    print(f"[DRIVE] Created folder: {name}")
    return folder["id"]

def list_files_in_folder(service, folder_id: str) -> list[dict]:
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,createdTime)",
        orderBy="createdTime"
    ).execute()
    return results.get("files", [])

def upload_file(service, file_path: Path, folder_id: str) -> str:
    """Upload file to Google Drive folder. Returns file ID."""
    print(f"[DRIVE] Uploading: {file_path.name} ({file_path.stat().st_size/1_048_576:.1f} MB)...")

    meta  = {"name": file_path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(file_path), resumable=True,
                             mimetype="application/gzip")

    request = service.files().create(body=meta, media_body=media, fields="id")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            print(f"[DRIVE]   Uploading... {pct}%", end="\r")

    print(f"[DRIVE] ✅ Uploaded: {file_path.name} (id={response['id']})")
    return response["id"]

def delete_file(service, file_id: str, name: str):
    service.files().delete(fileId=file_id).execute()
    print(f"[DRIVE] 🗑  Deleted old build: {name}")

def make_public_link(service, file_id: str) -> str:
    """Make file publicly readable and return shareable link."""
    service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"}
    ).execute()
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

# =============================================================================
# Upload to Google Drive
# =============================================================================
def upload_to_drive(cfg: dict, tar_path: Path):
    gd = cfg.get("google_drive", {})
    root_folder_id = gd.get("folder_id", "")
    keep_history   = gd.get("keep_history", 5)

    if not root_folder_id or root_folder_id == "YOUR_GDRIVE_FOLDER_ID_HERE":
        print("[DRIVE] ⚠️  google_drive.folder_id not set in build_config.yaml")
        print("[DRIVE]    Skipping upload. Set the folder ID to enable uploads.")
        return

    sa_key = os.environ.get(
        "GSHEET_SA_KEY_PATH",
        str(PROJECT_ROOT / "config" / "service_account.json")
    )

    if not Path(sa_key).exists():
        print(f"[DRIVE] ❌ Service account key not found: {sa_key}")
        return

    print(f"\n[DRIVE] Connecting to Google Drive...")
    service = gdrive_service(sa_key)

    # Get or create folder structure: root/latest/ and root/history/
    latest_id  = get_or_create_folder(service, "latest",  root_folder_id)
    history_id = get_or_create_folder(service, "history", root_folder_id)

    # ── Upload to latest/ (replace existing) ──────────────────────────────
    existing_latest = list_files_in_folder(service, latest_id)
    for f in existing_latest:
        delete_file(service, f["id"], f["name"])

    file_id = upload_file(service, tar_path, latest_id)
    link    = make_public_link(service, file_id)
    print(f"[DRIVE] 🔗 Latest build link: {link}")

    # ── Upload to history/ ────────────────────────────────────────────────
    upload_file(service, tar_path, history_id)

    # Prune history — keep only last N builds
    history_files = list_files_in_folder(service, history_id)
    if len(history_files) > keep_history:
        to_delete = history_files[:len(history_files) - keep_history]
        for f in to_delete:
            delete_file(service, f["id"], f["name"])
        print(f"[DRIVE] Pruned {len(to_delete)} old build(s) from history/")

    print(f"\n[DRIVE] ✅ Upload complete!")
    print(f"[DRIVE]    Latest : {link}")
    print(f"[DRIVE]    History: https://drive.google.com/drive/folders/{history_id}")

    # Save link for workflow summary
    link_file = PROJECT_ROOT / "logs" / "gdrive_link.txt"
    link_file.write_text(link)

# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",
                        default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    parser.add_argument("--skip-upload", action="store_true",
                        help="Bundle only, skip Google Drive upload")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    gd  = cfg.get("google_drive", {})

    # Check if upload_on_partial is set — if build had failures, check flag
    build_status_file = PROJECT_ROOT / "logs" / "build_logs" / "build_status.json"
    if build_status_file.exists():
        with open(build_status_file) as f:
            status = json.load(f)
        failed = [k for k, v in status.items() if v == "FAIL"]
        if failed and not gd.get("upload_on_partial", False):
            print(f"[UPLOAD] ⚠️  Some apps failed to build: {failed}")
            print(f"[UPLOAD]    Skipping upload (upload_on_partial=false in config)")
            print(f"[UPLOAD]    Set upload_on_partial: true to upload partial builds")
            sys.exit(0)

    # Build the bundle
    tar_path, bundle_name = build_bundle(cfg)

    if args.skip_upload:
        print(f"\n[UPLOAD] --skip-upload set — bundle saved at: {tar_path}")
        return

    # Upload to Google Drive
    upload_to_drive(cfg, tar_path)

if __name__ == "__main__":
    main()
