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

Upload structure on Google Drive (single folder — no latest/history split):
  Matter-CI-Builds/
  ├── LATEST.txt                              ← pointer: newest build's name/commit/link
  ├── matter-sdk-<branch>-<commit>-arm64.tar.gz   ← one file per build, unique name
  ├── matter-sdk-<branch>-<commit>-arm64.tar.gz
  └── ...                                     ← newest N kept (google_drive.keep_history)

Each build is uploaded ONCE and its own permanent file ID is what the email's
`gdown` link uses — a subsequent run never touches that file, so links stay
valid until the bundle is pruned (after keep_history newer builds). The newest
build is flagged via its Drive description ("LATEST · …") and the LATEST.txt
pointer; older builds are marked "older".

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

# NOTE: this runs on the Mac mini HOST, which has no SDK checkout. Binaries,
# wheels and build metadata are read from the Docker build's output dir
# (~/matter-output) — not resolved from an SDK. So we do NOT import
# discover_targets/resolve_pipeline_apps here.

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

def get_output_dir() -> Path:
    """Docker build output dir on the Mac mini host (binaries + wheels + metadata)."""
    return Path(os.environ.get("MATTER_OUTPUT_DIR", "~/matter-output")).expanduser()

def get_build_info(output_dir: Path) -> tuple[str, str]:
    """(commit_short, branch) from the container-written build-info.json."""
    info_file = output_dir / "build-info.json"
    if info_file.exists():
        try:
            info = json.loads(info_file.read_text())
            return info.get("commit_short", "unknown"), info.get("branch", "unknown")
        except Exception:
            pass
    return "unknown", "unknown"

# =============================================================================
# Bundle builder
# =============================================================================
def build_bundle(cfg: dict, output_dir: Path) -> tuple[Path, str]:
    """
    Creates a .tar.gz bundle of all built artifacts, read from the Docker
    build's output dir (~/matter-output) — NOT from an SDK checkout.
    Returns (tar_path, bundle_name).
    """
    commit, branch = get_build_info(output_dir)
    date_str  = datetime.now().strftime("%Y-%m-%d")
    safe_branch = branch.replace("/", "-")

    bundle_name = f"matter-sdk-{safe_branch}-{commit}-arm64"
    bundle_dir  = PROJECT_ROOT / "logs" / "bundle" / bundle_name
    bundle_dir.mkdir(parents=True, exist_ok=True)

    print(f"[BUNDLE] Creating: {bundle_name}")
    print(f"[BUNDLE] Source : {output_dir}")
    print(f"[BUNDLE] Branch : {branch}")
    print(f"[BUNDLE] Commit : {commit}")

    if not output_dir.exists():
        print(f"[BUNDLE] ❌ Output dir not found: {output_dir}")
        print(f"[BUNDLE]    Did the Docker build run and write to ~/matter-output?")
        sys.exit(1)

    # ── 1. Reference app binaries (from <output>/apps/) ───────────────────
    apps_dir = bundle_dir / "apps"
    apps_dir.mkdir(exist_ok=True)
    copied_apps = []

    src_apps = output_dir / "apps"
    for binary in sorted(src_apps.glob("*")) if src_apps.is_dir() else []:
        if not binary.is_file():
            continue
        dest = apps_dir / binary.name
        shutil.copy2(binary, dest)
        size = binary.stat().st_size / 1_048_576
        print(f"[BUNDLE]   ✅ {binary.name} ({size:.1f} MB)")
        copied_apps.append(binary.name)
    if not copied_apps:
        print(f"[BUNDLE]   ⚠️  No app binaries found in {src_apps}")

    # ── 2. chip-tool (from <output>/chip-tool) ────────────────────────────
    ct_binary = output_dir / "chip-tool"
    if ct_binary.exists():
        dest = bundle_dir / "chip-tool"
        shutil.copy2(ct_binary, dest)
        size = ct_binary.stat().st_size / 1_048_576
        print(f"[BUNDLE]   ✅ chip-tool ({size:.1f} MB)")
    else:
        print(f"[BUNDLE]   ⚠️  chip-tool not found — skipping")

    # ── 3. Python wheels (from <output>/wheels/) ──────────────────────────
    wheels_dir = bundle_dir / "wheels"
    wheels_dir.mkdir(exist_ok=True)

    wheel_search_dirs = [output_dir / "wheels"]

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

    # ── 3b. SDK python_testing requirements (per-TC deps) ─────────────────
    # The container copied src/python_testing/requirements*.txt here; carry them
    # into the bundle so install.sh installs exactly what this SDK commit needs.
    test_req_dir = bundle_dir / "test-requirements"
    copied_reqs = []
    src_req_dir = output_dir / "test-requirements"
    if src_req_dir.is_dir():
        test_req_dir.mkdir(exist_ok=True)
        for req in sorted(src_req_dir.glob("*.txt")):
            shutil.copy2(req, test_req_dir / req.name)
            copied_reqs.append(req.name)
            print(f"[BUNDLE]   ✅ test-requirements/{req.name}")
    if not copied_reqs:
        print("[BUNDLE]   ⚠️  No SDK test-requirements found — TCs may miss imports")

    # ── 4. build-info.txt ─────────────────────────────────────────────────
    # This runs on the macOS host, so lsb_release won't exist; the meaningful
    # OS is the Ubuntu container that produced the binaries. Best-effort only.
    try:
        ubuntu_ver = subprocess.run(
            ['lsb_release', '-rs'], capture_output=True, text=True
        ).stdout.strip() or "24.04 (container)"
    except Exception:
        ubuntu_ver = "24.04 (container)"

    build_info = bundle_dir / "build-info.txt"
    build_info.write_text(
        f"Matter SDK Build Information\n"
        f"=============================\n"
        f"Branch    : {branch}\n"
        f"Commit    : {commit}\n"
        f"Date      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Platform  : linux/arm64 (built in Docker on Mac mini, runs on RPi)\n"
        f"Ubuntu    : {ubuntu_ver}\n"
        f"Python    : {sys.version.split()[0]}\n"
        f"\n"
        f"Apps bundled:\n"
        + "".join(f"  - {a}\n" for a in copied_apps)
        + f"\nWheels bundled:\n"
        + "".join(f"  - {w.name}\n" for w in copied_wheels)
    )
    print(f"[BUNDLE]   ✅ build-info.txt written")

    # Also carry the machine-readable build-info.json (full SDK commit) so the
    # RPi test prep can `git checkout` the exact commit the binaries were built at.
    src_info_json = output_dir / "build-info.json"
    if src_info_json.exists():
        shutil.copy2(src_info_json, bundle_dir / "build-info.json")
        print(f"[BUNDLE]   ✅ build-info.json included")

    # ── 5. README.txt — user guide ────────────────────────────────────────
    readme = bundle_dir / "README.txt"
    apps_list = "\n".join(f"    apps/{a}" for a in copied_apps)
    wheels_list = "\n".join(f"    wheels/{w.name}" for w in copied_wheels)
    readme.write_text(
        f"╔══════════════════════════════════════════════════════════════╗\n"
        f"║           Matter SDK Build Bundle — User Guide               ║\n"
        f"╚══════════════════════════════════════════════════════════════╝\n"
        f"\n"
        f"Build Info\n"
        f"──────────\n"
        f"  Branch  : {branch}\n"
        f"  Commit  : {commit}\n"
        f"  Date    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"  Platform: Raspberry Pi ARM64 (Ubuntu {ubuntu_ver})\n"
        f"\n"
        f"Bundle Contents\n"
        f"───────────────\n"
        f"  apps/          ← Matter reference app binaries\n"
        + "".join(f"    {a}\n" for a in copied_apps)
        + f"  chip-tool      ← Matter commissioner and controller\n"
        f"  wheels/        ← Python controller wheels\n"
        + "".join(f"    {w.name}\n" for w in copied_wheels)
        + f"  test-requirements/ ← SDK's python_testing pip requirements (per-TC deps)\n"
        + "".join(f"    {r}\n" for r in copied_reqs)
        + f"  install.sh     ← One-command setup script\n"
        f"  README.txt     ← This file\n"
        f"  build-info.txt ← Detailed build metadata\n"
        f"\n"
        f"Quick Start\n"
        f"───────────\n"
        f"  Step 1 — Run the install script (installs all wheels):\n"
        f"\n"
        f"    chmod +x install.sh && ./install.sh\n"
        f"\n"
        f"  Step 2 — Activate the environment:\n"
        f"\n"
        f"    source chip_env/bin/activate\n"
        f"\n"
        f"  Step 3 — Copy binaries to your preferred location (optional):\n"
        f"\n"
        f"    cp apps/* ~/matter-apps/\n"
        f"    cp chip-tool ~/matter-apps/\n"
        f"\n"
        f"  Step 4 — Launch a sample app:\n"
        f"\n"
        f"    rm -rf /tmp/chip_* && ./apps/chip-all-clusters-app &\n"
        f"\n"
        f"  Step 5 — Run a python test script:\n"
        f"\n"
        f"    python3 /path/to/TC_ACE_1_2.py \\\n"
        f"      --commissioning-method on-network \\\n"
        f"      --discriminator 3840 \\\n"
        f"      --passcode 20202021 \\\n"
        f"      --storage-path admin_storage.json\n"
        f"\n"
        f"Requirements\n"
        f"────────────\n"
        f"  • Raspberry Pi 4 or 5 (ARM64)\n"
        f"  • Ubuntu 22.04 or 24.04 (64-bit)\n"
        f"  • Python 3.10 or higher\n"
        f"  • Same Ubuntu version as build machine ({ubuntu_ver}) recommended\n"
        f"\n"
        f"  System packages (auto-installed by install.sh):\n"
        f"    libavahi-client-dev, libdbus-1-dev, libglib2.0-dev\n"
        f"\n"
        f"Troubleshooting\n"
        f"───────────────\n"
        f"  ImportError: No module named 'chip'\n"
        f"    → Run: source chip_env/bin/activate\n"
        f"    → Or:  pip install wheels/*.whl --break-system-packages\n"
        f"\n"
        f"  ModuleNotFoundError: No module named 'mobly'\n"
        f"    → Run: pip install mobly\n"
        f"    → This is auto-installed by install.sh\n"
        f"\n"
        f"  chip-all-clusters-app: Permission denied\n"
        f"    → Run: chmod +x apps/*\n"
        f"\n"
        f"  CHIP Error 0x00000032: Timeout\n"
        f"    → DUT app may not be running or not in commissioning mode\n"
        f"    → Check: rm -rf /tmp/chip_* before launching DUT\n"
        f"\n"
        f"  Wheel not compatible with this platform\n"
        f"    → This bundle was built for Ubuntu {ubuntu_ver} ARM64\n"
        f"    → Ensure your RPi runs the same Ubuntu version\n"
        f"\n"
        f"Built by Matter CI Pipeline — Granite River Labs (GRL)\n"
        f"For issues contact the Matter GRLPS team.\n"
    )
    print(f"[BUNDLE]   ✅ README.txt written")

    # ── 6. install.sh — one-command setup ────────────────────────────────
    install_sh = bundle_dir / "install.sh"
    install_sh.write_text(
        f"#!/usr/bin/env bash\n"
        f"# =============================================================\n"
        f"# Matter SDK Bundle — Install Script\n"
        f"# Branch: {branch}  Commit: {commit}\n"
        f"# =============================================================\n"
        f"set -e\n"
        f"\n"
        f"SCRIPT_DIR=\"$(cd \"$(dirname \"${{BASH_SOURCE[0]}}\")\" && pwd)\"\n"
        f"cd \"$SCRIPT_DIR\"\n"
        f"\n"
        f"GREEN='\\033[0;32m'; CYAN='\\033[0;36m'; NC='\\033[0m'\n"
        f"log()  {{ echo -e \"${{CYAN}}[INSTALL]${{NC}} $*\"; }}\n"
        f"ok()   {{ echo -e \"${{GREEN}}[  OK  ]${{NC}} $*\"; }}\n"
        f"\n"
        f"echo \"╔══════════════════════════════════════════════════════╗\"\n"
        f"echo \"║       Matter SDK Bundle — Installation               ║\"\n"
        f"echo \"║  Branch : {branch:<42}                               ║\"\n"
        f"echo \"║  Commit : {commit:<42}                               ║\"\n"
        f"echo \"╚══════════════════════════════════════════════════════╝\"\n"
        f"echo\n"
        f"\n"
        f"# ── Step 1: System dependencies ────────────────────────────────\n"
        f"log \"Step 1/4 — Installing system dependencies...\"\n"
        f"sudo apt-get update -qq\n"
        f"sudo apt-get install -y \\\n"
        f"  python3 python3-venv python3-pip \\\n"
        f"  libavahi-client-dev libdbus-1-dev \\\n"
        f"  libglib2.0-dev libgirepository1.0-dev \\\n"
        f"  libcairo2-dev --quiet\n"
        f"ok \"System dependencies installed\"\n"
        f"\n"
        f"# ── Step 2: Create virtual environment ─────────────────────────\n"
        f"log \"Step 2/4 — Creating Python virtual environment...\"\n"
        f"python3 -m venv chip_env\n"
        f"source chip_env/bin/activate\n"
        f"pip install --upgrade pip --quiet\n"
        f"ok \"Virtual environment ready: chip_env/\"\n"
        f"\n"
        f"# ── Step 3: Install python wheels + dependencies ───────────────\n"
        f"log \"Step 3/4 — Installing Matter python wheels and dependencies...\"\n"
        f"\n"
        f"# Install the SDK's OWN python_testing requirements — the authoritative,\n"
        f"# per-test-case dep list for THIS build's commit (pycountry, validators,\n"
        f"# zeroconf, ...). This is what keeps new TCs from failing on missing imports.\n"
        f"if ls test-requirements/*.txt &>/dev/null; then\n"
        f"  for req in test-requirements/*.txt; do\n"
        f"    log \"Installing $req ...\"\n"
        f"    pip install -r \"$req\" --quiet\n"
        f"  done\n"
        f"  ok \"SDK test requirements installed\"\n"
        f"else\n"
        f"  echo \"[WARN] No test-requirements/ in bundle — installing fallback deps only\"\n"
        f"fi\n"
        f"\n"
        f"# A few runner/CLI deps not always in the SDK requirements.\n"
        f"pip install click colorama pyserial --quiet\n"
        f"ok \"Runner deps installed (click, colorama, pyserial)\"\n"
        f"\n"
        f"# Install Matter wheels\n"
        f"if ls wheels/*.whl &>/dev/null; then\n"
        f"  pip install wheels/*.whl --quiet\n"
        f"  ok \"Matter wheels installed successfully\"\n"
        f"else\n"
        f"  echo \"[WARN] No wheels found in wheels/ directory\"\n"
        f"fi\n"
        f"\n"
        f"# ── Step 4: Make binaries executable ───────────────────────────\n"
        f"log \"Step 4/4 — Setting binary permissions...\"\n"
        f"chmod +x apps/* chip-tool 2>/dev/null || true\n"
        f"ok \"Binaries are executable\"\n"
        f"\n"
        f"# ── Done ────────────────────────────────────────────────────────\n"
        f"echo\n"
        f"echo \"╔══════════════════════════════════════════════════════╗\"\n"
        f"echo \"║  ✅  Installation complete!                          ║\"\n"
        f"echo \"╚══════════════════════════════════════════════════════╝\"\n"
        f"echo\n"
        f"echo \"  Next steps:\"\n"
        f"echo \"    source chip_env/bin/activate\"\n"
        f"echo \"    ./apps/chip-all-clusters-app &\"\n"
        f"echo \"    python3 /path/to/TC_ACE_1_2.py --commissioning-method on-network ...\"\n"
        f"echo\n"
        f"echo \"  See README.txt for full usage guide.\"\n"
        f"echo\n"
    )
    # Make install.sh executable
    import stat as stat_mod
    install_sh.chmod(install_sh.stat().st_mode | stat_mod.S_IEXEC | stat_mod.S_IXGRP | stat_mod.S_IXOTH)
    print(f"[BUNDLE]   ✅ install.sh written")

    # ── 6b. pip-requirements.txt ──────────────────────────────────────────
    pip_reqs = bundle_dir / "pip-requirements.txt"
    pip_reqs.write_text(
        "# Matter SDK Python Dependencies\n"
        "# Install: pip install -r pip-requirements.txt\n"
        "# (install.sh handles this automatically)\n"
        "mobly\n"
        "click\n"
        "colorama\n"
        "pyserial\n"
    )
    print(f"[BUNDLE]   ✅ pip-requirements.txt written")

    # ── 7. Create tar.gz ──────────────────────────────────────────────────
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

def list_files_in_folder(service, folder_id: str) -> list[dict]:
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,createdTime,description)",
        orderBy="createdTime"
    ).execute()
    return results.get("files", [])

def set_description(service, file_id: str, description: str):
    """Set a file's Drive description (used to flag LATEST vs older builds)."""
    service.files().update(fileId=file_id, body={"description": description}).execute()

def upsert_text_file(service, folder_id: str, name: str, content: str):
    """Create or overwrite a small text file (e.g. LATEST.txt) in the folder."""
    tmp = PROJECT_ROOT / "logs" / name
    tmp.write_text(content)
    media = MediaFileUpload(str(tmp), mimetype="text/plain", resumable=False)
    query = (f"name='{name}' and '{folder_id}' in parents and trashed=false")
    existing = service.files().list(q=query, fields="files(id,name)").execute().get("files", [])
    if existing:
        service.files().update(fileId=existing[0]["id"], media_body=media).execute()
        print(f"[DRIVE] ✅ Updated pointer: {name}")
    else:
        meta = {"name": name, "parents": [folder_id]}
        service.files().create(body=meta, media_body=media, fields="id").execute()
        print(f"[DRIVE] ✅ Created pointer: {name}")

def upload_file(service, file_path: Path, folder_id: str) -> str:
    """Upload file to Google Drive folder. Returns file ID."""
    print(f"[DRIVE] Uploading: {file_path.name} ({file_path.stat().st_size/1_048_576:.1f} MB)...")

    meta  = {"name": file_path.name, "parents": [folder_id]}
    media = MediaFileUpload(str(file_path), resumable=True,
                             mimetype="application/gzip")

    request = service.files().create(body=meta, media_body=media, fields="id")
    response = None
    last_decile = -1   # print at 0/10/20/…% (newlines, not \r, so CI renders it)
    while response is None:
        status, response = request.next_chunk()
        if status:
            pct = int(status.progress() * 100)
            if pct // 10 > last_decile:
                last_decile = pct // 10
                print(f"[DRIVE]   Uploading... {pct}%")

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


def storage_free_bytes(service):
    """Free bytes in the SERVICE ACCOUNT's Drive quota, or None if the quota is
    pooled/unlimited (Shared Drive / Workspace — nothing to manage)."""
    try:
        q = service.about().get(fields="storageQuota").execute().get("storageQuota", {})
    except Exception as e:  # noqa: BLE001 — treat as "unknown", skip management
        print(f"[DRIVE] Could not read storage quota: {e}")
        return None
    if q.get("limit") is None:
        return None
    return max(int(q["limit"]) - int(q.get("usage", 0) or 0), 0)


def ensure_space_for_upload(service, folder_id, needed_bytes, name_prefix, margin=0.10):
    """Safety net run BEFORE an upload: guarantee room for `needed_bytes`.
    1) if enough free → return; 2) empty the SA trash; 3) if still short, delete
    OLDEST `name_prefix`*.tar.gz files (that kind ONLY — never other data) until it
    fits. Frees are tracked locally (Drive quota usage updates lazily). No-op on a
    pooled/unlimited (Shared Drive) quota."""
    free = storage_free_bytes(service)
    if free is None:
        return
    mb = 1_048_576
    need = int(needed_bytes * (1 + margin))
    if free >= need:
        print(f"[DRIVE] Space OK: {free // mb} MB free ≥ {need // mb} MB needed.")
        return
    print(f"[DRIVE] ⚠️  Only {free // mb} MB free, need ~{need // mb} MB — reclaiming…")
    try:
        service.files().emptyTrash().execute()
        print("[DRIVE] Emptied service-account trash.")
    except Exception as e:  # noqa: BLE001
        print(f"[DRIVE] emptyTrash failed (non-fatal): {e}")
    free = storage_free_bytes(service) or 0
    if free >= need:
        print(f"[DRIVE] Trash cleared → {free // mb} MB free. OK.")
        return
    # Delete oldest same-kind archives until it fits (track freed locally).
    resp = service.files().list(
        q=(f"'{folder_id}' in parents and trashed=false and "
           f"name contains '{name_prefix}'"),
        fields="files(id,name,size,createdTime)", orderBy="createdTime").execute()
    files = [f for f in resp.get("files", []) if f["name"].endswith(".tar.gz")]
    freed = 0
    for f in files:
        if free + freed >= need:
            break
        delete_file(service, f["id"], f["name"])
        freed += int(f.get("size", 0) or 0)
    avail = free + freed
    if avail >= need:
        print(f"[DRIVE] Reclaimed {freed // mb} MB → ~{avail // mb} MB available.")
    else:
        print(f"[DRIVE] ⚠️  Still only ~{avail // mb} MB after cleanup (need "
              f"{need // mb} MB) — quota may be filled by other data; upload may fail.")

# =============================================================================
# Upload to Google Drive
# =============================================================================
def upload_to_drive(cfg: dict, tar_path: Path, commit: str = "", branch: str = ""):
    """
    Single-folder upload: every build is one uniquely-named file in the same
    Drive folder. We email THAT file's own permanent ID, so a link keeps working
    until the bundle is pruned (never invalidated by the next run). The newest
    build is flagged via its Drive description + a LATEST.txt pointer; older
    builds are marked "older". Only the newest `keep_history` bundles are kept.
    """
    gd = cfg.get("google_drive", {})
    folder_id    = gd.get("folder_id", "")
    keep_history = gd.get("keep_history", 10)

    if not folder_id or folder_id == "YOUR_GDRIVE_FOLDER_ID_HERE":
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

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Snapshot the folder before uploading (used for same-name replace,
    # description downgrade, and pruning).
    existing = list_files_in_folder(service, folder_id)

    # ── Same-commit re-run: replace the identically-named bundle in place ──
    for f in existing:
        if f["name"] == tar_path.name:
            delete_file(service, f["id"], f["name"])
    existing = [f for f in existing if f["name"] != tar_path.name]

    # ── Prune BEFORE uploading so we free a slot first ────────────────────
    # Pruning after the upload can't self-heal a full Drive: the upload throws
    # (403 storageQuotaExceeded) before the prune ever runs. Deleting the oldest
    # bundles down to (keep_history - 1) up front leaves room for the incoming
    # one → keep_history total after. Scoped to matter-sdk-* so result archives
    # (matter-ci-results-*) sharing this folder are never touched.
    keep_before = max(keep_history - 1, 0)
    old_bundles = [f for f in existing
                   if f["name"].startswith("matter-sdk-")
                   and f["name"].endswith(".tar.gz")]   # oldest-first (createdTime)
    if len(old_bundles) > keep_before:
        for f in old_bundles[:len(old_bundles) - keep_before]:
            delete_file(service, f["id"], f["name"])
        print(f"[DRIVE] Pruned {len(old_bundles) - keep_before} old build(s) "
              f"before upload (keeping newest {keep_before} + this one = {keep_history})")

    # ── Safety net: ensure the SA quota has room for this upload ──────────
    # keep_history + prune-before should already keep us bounded, but guard
    # against a full/shared quota (empty trash, then delete oldest matter-sdk-*).
    ensure_space_for_upload(service, folder_id, tar_path.stat().st_size, "matter-sdk-")

    # ── Upload this build ONCE; its own ID is the permanent, emailed link ──
    file_id = upload_file(service, tar_path, folder_id)
    link    = make_public_link(service, file_id)
    print(f"[DRIVE] 🔗 Build link (permanent): {link}")

    # ── Flag newest vs older via Drive description ────────────────────────
    set_description(service, file_id,
                    f"LATEST · {date_str} · {branch or '?'} @ {commit or '?'}")
    for f in existing:
        if not f["name"].endswith(".tar.gz"):
            continue
        if (f.get("description") or "").startswith("LATEST"):
            try:
                set_description(service, f["id"], "older")
            except Exception as e:  # noqa: BLE001 — non-fatal cosmetic marking
                print(f"[DRIVE]   ⚠️  Could not mark {f['name']} as older: {e}")

    # ── Refresh the LATEST.txt pointer so browsers see what's current ─────
    upsert_text_file(
        service, folder_id, "LATEST.txt",
        f"Latest Matter SDK build\n"
        f"=======================\n"
        f"Bundle : {tar_path.name}\n"
        f"Branch : {branch or 'unknown'}\n"
        f"Commit : {commit or 'unknown'}\n"
        f"Built  : {date_str}\n"
        f"Link   : {link}\n"
        f"\n"
        f"Download: pip3 install gdown --break-system-packages && gdown {file_id}\n"
    )

    # (Pruning happens BEFORE the upload above — see the prune-before block.)

    print(f"\n[DRIVE] ✅ Upload complete!")
    print(f"[DRIVE]    Build  : {link}")
    print(f"[DRIVE]    Folder : https://drive.google.com/drive/folders/{folder_id}")

    # Save link for workflow summary + email (per-build permanent link)
    link_file = PROJECT_ROOT / "logs" / "gdrive_link.txt"
    link_file.write_text(link)

# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",
                        default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    parser.add_argument("--output", default=None,
                        help="Docker build output dir (default: $MATTER_OUTPUT_DIR "
                             "or ~/matter-output)")
    parser.add_argument("--skip-upload", action="store_true",
                        help="Bundle only, skip Google Drive upload")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    gd  = cfg.get("google_drive", {})

    output_dir = Path(args.output).expanduser() if args.output else get_output_dir()

    # Check if upload_on_partial is set — if build had failures, check flag.
    # build_status.json is written into the Docker output dir by the container.
    build_status_file = output_dir / "build_status.json"
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
    tar_path, bundle_name = build_bundle(cfg, output_dir)

    if args.skip_upload:
        print(f"\n[UPLOAD] --skip-upload set — bundle saved at: {tar_path}")
        return

    # Upload to Google Drive (single folder; email uses the per-build ID)
    commit, branch = get_build_info(output_dir)
    upload_to_drive(cfg, tar_path, commit=commit, branch=branch)

if __name__ == "__main__":
    main()
