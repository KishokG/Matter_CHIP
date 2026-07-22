#!/usr/bin/env python3
"""
upload_test_results.py

Bundles the CI TEST-EXECUTION results (HTML report + JSON summary + per-test
Ctrl/DUT logs) into a single .tar.gz and uploads it to Google Drive, returning a
permanent public download link (written to logs/results_drive_link.txt for the
notification email + job summary).

This is the test-side counterpart of upload_artifacts.py (which uploads the built
binaries). It REUSES that module's Drive helpers so auth/upload/prune behave
identically. Results go to google_drive.results_folder_id when set, else the same
folder_id as builds — pruning only ever touches result archives (matched by the
`matter-ci-results-` name prefix), never build bundles.

Usage (from the repo root, on the RPi):
    export GSHEET_SA_KEY_PATH=/path/to/service_account.json
    python3 Matter_CI/scripts/upload_test_results.py \
        --config Matter_CI/config/build_config.yaml \
        --run-id 123
"""
import os
import sys
import json
import argparse
import tarfile
from pathlib import Path
from datetime import datetime

# Reuse the Drive helpers from the build uploader (same dir on sys.path).
from upload_artifacts import (
    PROJECT_ROOT,
    load_config,
    gdrive_service,
    upload_file,
    make_public_link,
    list_files_in_folder,
    delete_file,
    ensure_space_for_upload,
)

RESULTS_PREFIX = "matter-ci-results-"   # names we own → safe to prune


def _commit_short() -> str:
    """Read the SDK commit under test from the build-info bundled with results."""
    info = PROJECT_ROOT / "logs" / "build-info.json"
    if info.exists():
        try:
            d = json.loads(info.read_text())
            return d.get("commit_short") or (d.get("commit", "") or "")[:9] or "unknown"
        except Exception:
            pass
    return "unknown"


def build_results_bundle(run_id: str) -> Path:
    """Tar the report + JSON summary + per-test logs into logs/<name>.tar.gz."""
    logs = PROJECT_ROOT / "logs"
    members = [
        logs / "report.html",
        logs / "test_results.json",
        logs / "test_runs",       # per-test Ctrl/DUT logs (directory)
        logs / "preflight.json",
        logs / "build-info.json",
    ]
    present = [m for m in members if m.exists()]
    if not present:
        print("[RESULTS] ⚠️  No result files found in logs/ — nothing to upload.")
        return None

    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    rid = (run_id or "0").strip()
    name = f"{RESULTS_PREFIX}run{rid}-{_commit_short()}-{date_str}.tar.gz"
    out = logs / name
    print(f"[RESULTS] Bundling {len(present)} item(s) → {name}")
    with tarfile.open(out, "w:gz") as tar:
        for m in present:
            tar.add(str(m), arcname=m.name)
    print(f"[RESULTS] ✅ Bundle: {out} ({out.stat().st_size/1_048_576:.1f} MB)")
    return out


def upload_results_to_drive(cfg: dict, tar_path: Path, run_id: str) -> str:
    gd = cfg.get("google_drive", {})
    # Prefer a dedicated results folder; fall back to the build folder.
    folder_id = gd.get("results_folder_id") or gd.get("folder_id", "")
    keep_history = gd.get("keep_history", 10)

    if not folder_id or folder_id in ("YOUR_GDRIVE_FOLDER_ID_HERE", ""):
        print("[RESULTS] ⚠️  google_drive.folder_id/results_folder_id not set — skipping upload.")
        return ""

    sa_key = os.environ.get(
        "GSHEET_SA_KEY_PATH",
        str(PROJECT_ROOT / "config" / "service_account.json"),
    )
    if not Path(sa_key).exists():
        print(f"[RESULTS] ❌ Service account key not found: {sa_key}")
        return ""

    print("[RESULTS] Connecting to Google Drive...")
    service = gdrive_service(sa_key)

    # Same-name re-run: replace in place.
    existing = list_files_in_folder(service, folder_id)
    for f in existing:
        if f["name"] == tar_path.name:
            delete_file(service, f["id"], f["name"])
    existing = [f for f in existing if f["name"] != tar_path.name]

    # Prune BEFORE uploading so we free a slot first — pruning after can't
    # self-heal a full Drive (the upload throws 403 storageQuotaExceeded before
    # the prune runs). Keep the newest (keep_history - 1) so this run's archive
    # makes keep_history total. Scoped to RESULTS_PREFIX (never build bundles).
    keep_before = max(keep_history - 1, 0)
    old_results = [f for f in existing
                   if f["name"].startswith(RESULTS_PREFIX) and f["name"].endswith(".tar.gz")]
    if len(old_results) > keep_before:
        for f in old_results[:len(old_results) - keep_before]:   # oldest-first
            delete_file(service, f["id"], f["name"])
        print(f"[RESULTS] Pruned {len(old_results) - keep_before} old result set(s) "
              f"before upload (keeping newest {keep_before} + this one = {keep_history})")

    # Safety net: ensure quota room before uploading (empty trash → delete oldest
    # matter-ci-results-* if needed). Complements the count-based prune above.
    ensure_space_for_upload(service, folder_id, tar_path.stat().st_size, RESULTS_PREFIX)

    file_id = upload_file(service, tar_path, folder_id)
    link = make_public_link(service, file_id)
    print(f"[RESULTS] 🔗 Results link (permanent): {link}")

    (PROJECT_ROOT / "logs" / "results_drive_link.txt").write_text(link)
    return link


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",
                        default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    tar_path = build_results_bundle(args.run_id)
    if not tar_path:
        return   # nothing to upload — not an error (email still sends a summary)
    link = upload_results_to_drive(cfg, tar_path, args.run_id)
    if link:
        print(f"\n[RESULTS] ✅ Done. Download: {link}")


if __name__ == "__main__":
    main()
