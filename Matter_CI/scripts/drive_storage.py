#!/usr/bin/env python3
"""
drive_storage.py — inspect and reclaim the SERVICE ACCOUNT's Google Drive.

The CI uploads (build bundles + result archives) are OWNED BY THE SERVICE ACCOUNT,
so they count against the SA's own Drive quota — NOT your personal Drive. That's
why your Drive can show ~0 used with an empty trash, yet uploads still fail with
"The user's Drive storage quota has been exceeded" (the SA's quota is full). The
SA has no web UI, so this script is how you see and free its storage.

Run where the SA key is available (RPi/mac-mini, or a one-off CI step):
    export GSHEET_SA_KEY_PATH=/path/to/service_account.json   # the CREDENTIALS_JSON key
    python3 Matter_CI/scripts/drive_storage.py report         # quota + everything the SA owns
    python3 Matter_CI/scripts/drive_storage.py empty-trash    # permanently empty the SA trash
    python3 Matter_CI/scripts/drive_storage.py prune --keep 3 # keep newest N matter-sdk-*/matter-ci-results-*
    python3 Matter_CI/scripts/drive_storage.py nuke-orphans   # delete SA-owned files NOT in the config folder
"""
import os
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from upload_artifacts import gdrive_service, load_config, PROJECT_ROOT  # noqa: E402


def _svc():
    sa = os.environ.get("GSHEET_SA_KEY_PATH",
                        str(PROJECT_ROOT / "config" / "service_account.json"))
    if not Path(sa).exists():
        sys.exit(f"[ERROR] Service account key not found: {sa}\n"
                 f"        Set GSHEET_SA_KEY_PATH to the CREDENTIALS_JSON key file.")
    return gdrive_service(sa)


def _mb(n):
    try:
        return f"{int(n) / 1_048_576:.1f} MB"
    except (TypeError, ValueError):
        return "?"


def _all_owned(service, trashed=None):
    """All files owned by the SA (paginated). trashed=None → both."""
    q = "'me' in owners"
    if trashed is True:
        q += " and trashed=true"
    elif trashed is False:
        q += " and trashed=false"
    files, page = [], None
    while True:
        resp = service.files().list(
            q=q, spaces="drive",
            fields="nextPageToken, files(id,name,size,createdTime,trashed,parents)",
            pageSize=1000, pageToken=page).execute()
        files.extend(resp.get("files", []))
        page = resp.get("nextPageToken")
        if not page:
            break
    return files


def cmd_report(service, cfg):
    about = service.about().get(fields="storageQuota,user").execute()
    q = about.get("storageQuota", {})
    limit = q.get("limit")
    print("=== Service account ===")
    print(f"  {about.get('user', {}).get('emailAddress', '?')}")
    print("=== Storage quota (SERVICE ACCOUNT, not your personal Drive) ===")
    print(f"  Used total : {_mb(q.get('usage'))}")
    print(f"  In Drive   : {_mb(q.get('usageInDrive'))}")
    print(f"  In trash   : {_mb(q.get('usageInDriveTrash'))}")
    print(f"  Limit      : {_mb(limit) if limit else 'unlimited / not reported'}")

    owned = _all_owned(service)
    live = [f for f in owned if not f.get("trashed")]
    trash = [f for f in owned if f.get("trashed")]
    tot = sum(int(f.get("size", 0) or 0) for f in owned)
    print(f"\n=== Files owned by the SA: {len(owned)} ({_mb(tot)}) — "
          f"{len(live)} live, {len(trash)} trashed ===")
    for f in sorted(owned, key=lambda x: int(x.get("size", 0) or 0), reverse=True)[:30]:
        flag = "TRASH" if f.get("trashed") else "     "
        print(f"  [{flag}] {_mb(f.get('size',0)):>10}  {f['name']}")
    if len(owned) > 30:
        print(f"  … and {len(owned) - 30} more")


def cmd_empty_trash(service, cfg):
    trash = _all_owned(service, trashed=True)
    freed = sum(int(f.get("size", 0) or 0) for f in trash)
    print(f"Emptying SA trash: {len(trash)} file(s), {_mb(freed)} ...")
    service.files().emptyTrash().execute()
    print("✅ Trash emptied (permanent).")


def cmd_prune(service, cfg, keep):
    folder_id = cfg.get("google_drive", {}).get("folder_id", "")
    for prefix in ("matter-sdk-", "matter-ci-results-"):
        files = [f for f in _all_owned(service, trashed=False)
                 if f["name"].startswith(prefix) and f["name"].endswith(".tar.gz")]
        files.sort(key=lambda x: x.get("createdTime", ""))   # oldest first
        drop = files[:max(len(files) - keep, 0)]
        print(f"{prefix}*: {len(files)} found, deleting {len(drop)} (keeping newest {keep})")
        for f in drop:
            service.files().delete(fileId=f["id"]).execute()
            print(f"  🗑  {f['name']} ({_mb(f.get('size',0))})")


def cmd_nuke_orphans(service, cfg):
    folder_id = cfg.get("google_drive", {}).get("folder_id", "")
    if not folder_id:
        sys.exit("[ERROR] google_drive.folder_id not set in config.")
    owned = _all_owned(service, trashed=False)
    orphans = [f for f in owned if folder_id not in (f.get("parents") or [])]
    freed = sum(int(f.get("size", 0) or 0) for f in orphans)
    print(f"SA-owned files NOT in folder {folder_id}: {len(orphans)} ({_mb(freed)})")
    for f in orphans:
        print(f"  🗑  {f['name']} ({_mb(f.get('size',0))})")
        service.files().delete(fileId=f["id"]).execute()
    print("✅ Orphans deleted.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["report", "empty-trash", "prune", "nuke-orphans"])
    ap.add_argument("--config", default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    ap.add_argument("--keep", type=int, default=3, help="prune: keep newest N of each kind")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    service = _svc()
    if args.action == "report":
        cmd_report(service, cfg)
    elif args.action == "empty-trash":
        cmd_empty_trash(service, cfg)
    elif args.action == "prune":
        cmd_prune(service, cfg, args.keep)
    elif args.action == "nuke-orphans":
        cmd_nuke_orphans(service, cfg)


if __name__ == "__main__":
    main()
