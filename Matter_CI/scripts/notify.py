#!/usr/bin/env python3
"""
notify.py
=========
Sends email notification after Matter CI build completes.

Reads build results and sends HTML email to all recipients with:
- Build status (success/partial/failed)
- Download instructions for successful builds
- Error details for failed builds

Usage:
    python3 scripts/notify.py --config config/build_config.yaml
                               --status success|partial|failed
                               --drive-link https://drive.google.com/...
                               --run-id 42
                               --run-url https://github.com/...
"""

import os
import sys
import json
import yaml
import smtplib
import argparse
import subprocess
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent


# =============================================================================
# Config
# =============================================================================
def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)

def get_output_dir() -> Path:
    """Docker build output dir on the Mac mini host (build-info.json, build_status.json)."""
    return Path(os.environ.get("MATTER_OUTPUT_DIR", "~/matter-output")).expanduser()

def get_git_info(cfg: dict) -> tuple[str, str]:
    # Preferred: the container-written build-info.json in the Docker output dir
    # (the Mac mini host has no SDK checkout to run git in).
    info_file = get_output_dir() / "build-info.json"
    if info_file.exists():
        try:
            info = json.loads(info_file.read_text())
            return info.get("commit_short", "unknown"), info.get("branch", "unknown")
        except Exception:
            pass
    # Fallback: git in the SDK dir (legacy RPi build path).
    sdk_dir = Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))
    def run(cmd):
        try:
            return subprocess.run(cmd, cwd=sdk_dir, capture_output=True,
                                  text=True).stdout.strip()
        except Exception:
            return "unknown"
    return run(["git", "rev-parse", "--short", "HEAD"]), \
           run(["git", "rev-parse", "--abbrev-ref", "HEAD"])

def load_build_status() -> dict:
    # Preferred: Docker output dir; fallback: legacy in-repo build_logs path.
    for status_file in (get_output_dir() / "build_status.json",
                        PROJECT_ROOT / "logs" / "build_logs" / "build_status.json"):
        if status_file.exists():
            try:
                with open(status_file) as f:
                    return json.load(f)
            except Exception:
                continue
    return {}


# =============================================================================
# HTML Email template
# =============================================================================
def build_html(status: str, cfg: dict, commit: str, branch: str,
               drive_link: str, run_url: str, run_id: str,
               failed_apps: list, passed_apps: list) -> str:

    date_str    = datetime.now().strftime("%Y-%m-%d %H:%M IST")
    safe_branch = branch.replace("/", "-")
    bundle_name = f"matter-sdk-{safe_branch}-{commit}-arm64.tar.gz"
    bundle_dir  = f"matter-sdk-{safe_branch}-{commit}-arm64"   # extracted folder name
    file_id     = drive_link.split("/d/")[1].split("/")[0] if "/d/" in drive_link else ""


    # Status banner
    if status == "success":
        banner_color = "#27ae60"
        banner_icon  = "✅"
        banner_text  = "BUILD SUCCESS"
        sub_text     = "All targets built successfully. Bundle uploaded to Google Drive."
    elif status == "partial":
        banner_color = "#f39c12"
        banner_icon  = "⚠️"
        banner_text  = "BUILD PARTIAL SUCCESS"
        sub_text     = f"{len(failed_apps)} app(s) failed. Bundle uploaded with successful builds."
    else:
        banner_color = "#c0392b"
        banner_icon  = "🔴"
        banner_text  = "BUILD FAILED"
        sub_text     = "Critical build failure. No bundle uploaded."

    # Failed apps section
    failed_section = ""
    if failed_apps:
        items = "".join(
            f'<div class="app-row">&#10060; {app}</div>'
            for app in failed_apps)
        failed_section = (
            '<div class="app-section">' +
            '<div class="app-title" style="color:#991B1B">Failed apps</div>' +
            '<div style="border-radius:8px;overflow:hidden;border:1px solid #FEE2E2">' +
            items +
            '</div></div>'
        )

    # Passed apps section
    passed_section = ""
    if passed_apps:
        items = "".join(
            f'<div class="app-row-pass">&#9989; {app}</div>'
            for app in passed_apps)
        passed_section = (
            '<div class="app-section">' +
            '<div class="app-title" style="color:#065F46">Built successfully</div>' +
            '<div style="border-radius:8px;overflow:hidden;border:1px solid #D1FAE5">' +
            items +
            '</div></div>'
        )

    # Download section (only for success/partial)
    download_section = ""
    if drive_link and status in ("success", "partial"):
        download_section = (
            '<div style="margin-bottom:16px">'
            '<div style="font-size:10px;font-weight:600;color:#9CA3AF;'
            'text-transform:uppercase;letter-spacing:0.8px;'
            'border-bottom:1px solid #E5E7EB;padding-bottom:8px;'
            'margin-bottom:14px">Download and install</div>'

            # Code block wrapper
            '<div style="border-radius:10px;overflow:hidden;'
            'border:1px solid #30363D;margin-bottom:14px">'

            # Mac dots header — table based for Gmail Android
            '<table width="100%" cellpadding="0" cellspacing="0" '
            'style="background:#161B22;border-bottom:1px solid #30363D" '
            'role="presentation"><tr>'
            # Fixed-size inline-block spans render as true circles; table cells
            # with border-radius get stretched by the row into ovals.
            '<td style="padding:10px 0 10px 14px;vertical-align:middle;'
            'line-height:0;white-space:nowrap;font-size:0">'
            '<span style="display:inline-block;width:11px;height:11px;'
            'border-radius:11px;background:#FF5F57;margin-right:7px"></span>'
            '<span style="display:inline-block;width:11px;height:11px;'
            'border-radius:11px;background:#FFBD2E;margin-right:7px"></span>'
            '<span style="display:inline-block;width:11px;height:11px;'
            'border-radius:11px;background:#28C840"></span>'
            '</td>'
            '<td style="padding:10px 14px;font-size:10px;color:#6E7681;'
            'font-weight:500;vertical-align:middle">Raspberry Pi terminal</td>'
            '</tr></table>'

            # Code body — each line a separate <div>; white-space:pre-wrap +
            # overflow-wrap keeps every line (incl. the long gdown file id)
            # rendering uniformly instead of collapsing/boxing awkwardly.
            '<div style="background:#0D1117;padding:14px 16px;'
            'font-family:Courier New,Courier,monospace;font-size:12px;'
            'line-height:1.9;color:#E6EDF3;white-space:pre-wrap;'
            'overflow-wrap:anywhere">'
            '<div><span style="color:#7EE787"># 1. download the bundle + install the python wheels</span></div>'
            '<div>pip3 install gdown --break-system-packages</div>'
            f'<div>gdown {file_id}</div>'
            f'<div>tar -xzf {bundle_name}</div>'
            f'<div>cd {bundle_dir}/</div>'
            '<div>chmod +x install.sh &amp;&amp; ./install.sh</div>'
            '<div>&nbsp;</div>'
            '<div><span style="color:#7EE787"># 2. match your connectedhomeip to THIS build\'s commit</span></div>'
            '<div><span style="color:#7EE787">#    (so the test scripts match the bundled binaries)</span></div>'
            '<div>cd ~/connectedhomeip</div>'
            f'<div>git fetch origin &amp;&amp; git checkout {commit}</div>'
            '<div>python3 scripts/checkout_submodules.py --shallow --platform linux</div>'
            '</div>'
            '</div>'

            # Drive button
            f'<a href="{drive_link}" style="display:block;'
            'background:#1a5fa8;border-radius:10px;padding:15px 20px;'
            'text-align:center;text-decoration:none;margin-bottom:10px">'
            '<span style="display:block;font-size:14px;font-weight:700;'
            'color:#FFFFFF;margin-bottom:4px">Open in Google Drive</span>'
            f'<span style="display:block;font-size:11px;'
            'color:rgba(255,255,255,0.7);word-break:break-all">'
            f'{bundle_name}</span>'
            '</a>'
            '</div>'
        )
    # Actions link section
    actions_section = ""
    if run_url:
        actions_section = (
            f'<a href="{run_url}" class="gh-btn">' +
            f'<span class="gh-text">View GitHub Actions run #{run_id}</span>' +
            '</a>'
        )

    # Status pill colours
    if status == "success":
        pill_bg    = "rgba(255,255,255,0.15)"
        pill_border= "rgba(255,255,255,0.35)"
        pill_dot   = "#4ADE80"
        pill_text  = "#FFFFFF"
    elif status == "partial":
        pill_bg    = "rgba(251,191,36,0.2)"
        pill_border= "rgba(251,191,36,0.5)"
        pill_dot   = "#FBBF24"
        pill_text  = "#FEF3C7"
    else:
        pill_bg    = "rgba(239,68,68,0.2)"
        pill_border= "rgba(239,68,68,0.5)"
        pill_dot   = "#F87171"
        pill_text  = "#FEE2E2"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="x-apple-disable-message-reformatting">
  <title>Matter SDK Build</title>
  <style>
    body{{margin:0;padding:0;background:#ECEEF2;
         font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
         -webkit-text-size-adjust:100%;-ms-text-size-adjust:100%}}
    .outer{{padding:24px 12px}}
    .card{{max-width:560px;margin:0 auto;background:#ffffff;
           border-radius:16px;overflow:hidden;border:1px solid #DDE1E7}}
    .meta{{width:100%;border-collapse:collapse;
           border:1px solid #E5E7EB;border-radius:10px;
           margin-bottom:22px;table-layout:fixed}}
    .meta td{{background:#F9FAFB;padding:13px 14px;
              border-right:1px solid #E5E7EB;vertical-align:top}}
    .meta td:last-child{{border-right:none}}
    .meta-lbl{{display:block;font-size:10px;font-weight:600;color:#9CA3AF;
               text-transform:uppercase;letter-spacing:0.8px;margin-bottom:5px}}
    .meta-val{{display:block;font-size:13px;font-weight:700;
               color:#111827;word-break:break-all}}
    .mono{{font-family:'Courier New',monospace}}
    .sec-lbl{{font-size:10px;font-weight:600;color:#9CA3AF;
              text-transform:uppercase;letter-spacing:0.8px;
              border-bottom:1px solid #E5E7EB;padding-bottom:8px;
              margin-bottom:14px}}
    .code-wrap{{border-radius:10px;overflow:hidden;
                border:1px solid #30363D;margin-bottom:14px}}
    .code-hdr{{background:#161B22;padding:9px 14px;
               border-bottom:1px solid #30363D;
               display:flex;align-items:center;gap:6px}}
    .cdot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
    .code-lbl{{font-size:10px;color:#6E7681;font-weight:500;margin-left:4px}}
    .code-body{{display:block;background:#0D1117;padding:14px 16px;
                font-family:'Courier New',Courier,monospace;
                font-size:12px;line-height:2;color:#E6EDF3;
                word-wrap:break-word;overflow-wrap:break-word}}
    .cc{{color:#7EE787}}
    .drive-btn{{display:block;border-radius:10px;padding:15px 20px;
                text-align:center;text-decoration:none;margin-bottom:10px;
                background:#1a5fa8}}
    .drive-title{{display:block;font-size:14px;font-weight:700;
                  color:#FFFFFF;margin-bottom:4px}}
    .drive-sub{{display:block;font-size:11px;color:rgba(255,255,255,0.7);
                word-break:break-all;line-height:1.4}}
    .gh-btn{{display:block;background:#FFFFFF;border:1px solid #E5E7EB;
             border-radius:10px;padding:11px 20px;text-align:center;
             text-decoration:none}}
    .gh-text{{font-size:12px;color:#4B5563;font-weight:500}}
    .app-section{{margin-bottom:18px}}
    .app-title{{font-size:12px;font-weight:700;margin-bottom:8px}}
    .app-row{{padding:8px 12px;font-size:12px;
              border-bottom:1px solid #FEE2E2;
              background:#FFF5F5;color:#7F1D1D}}
    .app-row-pass{{padding:8px 12px;font-size:12px;
                   border-bottom:1px solid #D1FAE5;
                   background:#F0FDF4;color:#14532D}}
    .app-row:last-child,.app-row-pass:last-child{{border-bottom:none}}
    @media only screen and (max-width:480px){{
      .outer{{padding:12px 6px}}
      .pad{{padding-left:16px!important;padding-right:16px!important}}
      .hdr-title{{font-size:20px!important}}
      .meta,.meta tbody,.meta tr,.meta td{{
        display:block!important;width:100%!important;
        box-sizing:border-box!important}}
      .meta td{{border-right:none!important;
                border-bottom:1px solid #E5E7EB!important}}
      .meta td:last-child{{border-bottom:none!important}}
      .code-body{{font-size:11px!important}}
    }}
  </style>
</head>
<body>
<div class="outer">
<div class="card">

  <!--
    HEADER — single wide <td> with bgcolor fallback for Gmail
    + CSS gradient for modern clients. No column split (looks blocky).
  -->
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
    <tr>
      <td bgcolor="#1a5fa8"
          style="background-color:#1a5fa8;
                 background-image:linear-gradient(135deg,#0F2752 0%,#1a5fa8 55%,#0e7dc2 100%);
                 padding:28px 28px 16px"
          class="pad">
        <span style="display:block;font-size:10px;font-weight:600;
                     color:rgba(255,255,255,0.6);letter-spacing:1.8px;
                     text-transform:uppercase;margin-bottom:14px">
          Granite River Labs &nbsp;&mdash;&nbsp; Matter CI Pipeline
        </span>
        <p class="hdr-title"
           style="font-size:22px;font-weight:700;color:#FFFFFF;
                  letter-spacing:-0.3px;margin:0 0 8px;line-height:1.2">
          Matter SDK build
        </p>
        <p style="font-size:12px;color:rgba(255,255,255,0.6);margin:0">
          {date_str} &nbsp;&middot;&nbsp; Raspberry Pi ARM64
        </p>
      </td>
    </tr>
    <tr>
      <td bgcolor="#0e7dc2"
          style="background-color:#0e7dc2;
                 background-image:linear-gradient(135deg,#1a5fa8 0%,#0e7dc2 100%);
                 padding:14px 28px 22px"
          class="pad">
        <table cellpadding="0" cellspacing="0" role="presentation">
          <tr>
            <td style="background:rgba(255,255,255,0.15);
                       border:1px solid rgba(255,255,255,0.35);
                       border-radius:20px;padding:6px 14px 6px 10px">
              <table cellpadding="0" cellspacing="0" role="presentation">
                <tr>
                  <td style="vertical-align:middle;padding-right:7px">
                    <div style="width:8px;height:8px;border-radius:50%;
                                background:{pill_dot}"></div>
                  </td>
                  <td style="vertical-align:middle">
                    <span style="font-size:11px;font-weight:700;
                                 color:#FFFFFF;letter-spacing:0.5px;
                                 text-transform:uppercase">{banner_text}</span>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
        <p style="font-size:12px;color:rgba(255,255,255,0.65);
                  margin:10px 0 0">
          {sub_text}
        </p>
      </td>
    </tr>
  </table>

  <!-- Body -->
  <div style="padding:24px 28px" class="pad">

    <table class="meta" cellpadding="0" cellspacing="0" role="presentation">
      <tr>
        <td>
          <span class="meta-lbl">Branch</span>
          <span class="meta-val">{branch}</span>
        </td>
        <td>
          <span class="meta-lbl">Commit</span>
          <span class="meta-val mono">{commit}</span>
        </td>
        <td>
          <span class="meta-lbl">Run ID</span>
          <span class="meta-val">#{run_id}</span>
        </td>
      </tr>
    </table>

    <!-- Prominent build-commit callout — QA must sync their SDK to this -->
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation"
           style="margin:16px 0 4px">
      <tr>
        <td style="background:#0D1117;border:1px solid #30363D;
                   border-left:4px solid {banner_color};border-radius:8px;
                   padding:14px 16px">
          <div style="font-size:11px;color:#8B949E;letter-spacing:0.5px;
                      text-transform:uppercase;margin-bottom:5px">
            SDK commit under test
          </div>
          <div style="font-family:Courier New,Courier,monospace;font-size:20px;
                      font-weight:700;color:#E6EDF3;word-break:break-all">
            {commit}
          </div>
          <div style="font-size:12px;color:#8B949E;margin-top:7px;line-height:1.5">
            Branch <b style="color:#E6EDF3">{branch}</b>. Check out your
            <b style="color:#E6EDF3">connectedhomeip</b> to this commit
            <b style="color:#E6EDF3">before</b> running the tests, so the test
            scripts match these binaries (steps below).
          </div>
        </td>
      </tr>
    </table>

    {failed_section}
    {passed_section}
    {download_section}
    {actions_section}

  </div>

  <!-- Footer — two centered lines -->
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
    <tr>
      <td style="background:#F9FAFB;padding:16px 28px;
                 border-top:1px solid #F1F5F9;text-align:center">
        <div style="font-size:12px;color:#9CA3AF;margin-bottom:4px">
          This is an automated notification from Matter CI Pipeline.
        </div>
        <div style="font-size:12px;color:#9CA3AF">
          Granite River Labs &mdash; GRLPS Matter Team
        </div>
      </td>
    </tr>
  </table>

</div>
</div>
</body>
</html>"""


def build_plain_text(status: str, commit: str, branch: str,
                     drive_link: str, run_url: str, run_id: str,
                     failed_apps: list, passed_apps: list) -> str:
    """Plain text fallback for email clients that don't support HTML."""
    file_id = drive_link.split("/d/")[1].split("/")[0] if "/d/" in drive_link else ""
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M IST")
    safe_branch = branch.replace("/", "-")
    bundle_name = f"matter-sdk-{safe_branch}-{commit}-arm64.tar.gz"
    bundle_dir  = f"matter-sdk-{safe_branch}-{commit}-arm64"

    lines = [
        "=" * 60,
        "  Matter SDK Nightly Build — Granite River Labs",
        f"  {date_str}",
        "=" * 60,
        f"  Status : {status.upper()}",
        f"  Branch : {branch}",
        f"  Run ID : #{run_id}",
        "",
        ">>> SDK COMMIT UNDER TEST: " + commit,
        f"    Check out your connectedhomeip to {commit} (branch {branch})",
        "    BEFORE running the tests, so the scripts match these binaries.",
        "",
    ]

    if failed_apps:
        lines += ["Failed Apps:", *[f"  ❌ {a}" for a in failed_apps], ""]
    if passed_apps:
        lines += ["Built Apps:", *[f"  ✅ {a}" for a in passed_apps], ""]

    if drive_link and status in ("success", "partial"):
        lines += [
            "Download & Install:",
            "  # 1. download the bundle + install the python wheels",
            "  pip3 install gdown --break-system-packages",
            f"  gdown {file_id}",
            f"  tar -xzf {bundle_name}",
            f"  cd {bundle_dir}/",
            "  chmod +x install.sh && ./install.sh",
            "",
            "  # 2. match your connectedhomeip to THIS build's commit so the",
            "  #    test scripts match the bundled binaries:",
            "  cd ~/connectedhomeip",
            f"  git fetch origin && git checkout {commit}",
            "  python3 scripts/checkout_submodules.py --shallow --platform linux",
            "",
            f"  Google Drive link: {drive_link}",
            "",
        ]

    if run_url:
        lines.append(f"GitHub Actions: {run_url}")

    lines += ["", "=" * 60,
              "Automated notification — Matter CI Pipeline",
              "Granite River Labs — GRLPS Matter Team"]

    return "\n".join(lines)


# =============================================================================
# Send email
# =============================================================================
# =============================================================================
# Test-execution email (separate from the build email)
# =============================================================================
def compute_test_summary(results_path: Path) -> dict:
    """Read test_results.json → totals, per-status counts, and pass %."""
    try:
        results = json.loads(results_path.read_text())
    except Exception:
        results = []
    def n(*statuses):
        return sum(1 for r in results if r.get("status") in statuses)
    total = len(results)
    passed    = n("PASS")
    pass_warn = n("PASS*")
    failed    = n("FAIL")
    errored   = n("ERROR")
    rerun     = n("RERUN")
    cancelled = n("CANCEL")
    accepted  = passed + pass_warn           # PASS* is an accepted (partial) pass
    pct = round(accepted / total * 100) if total else 0
    # Failing TCs (name + short note), failures first — for the email body.
    ORDER = {"FAIL": 0, "ERROR": 1, "RERUN": 2, "PASS*": 3, "CANCEL": 4, "PASS": 5}
    failing = sorted(
        [r for r in results if r.get("status") in ("FAIL", "ERROR")],
        key=lambda r: (ORDER.get(r.get("status"), 9), r.get("test_case_id", "")),
    )
    return {
        "total": total, "passed": passed, "pass_warn": pass_warn,
        "failed": failed, "errored": errored, "rerun": rerun,
        "cancelled": cancelled, "accepted": accepted, "pct": pct,
        "failing": failing,
    }


def _exec_status(s: dict) -> str:
    """Overall execution verdict from the counts."""
    if s["total"] == 0:
        return "empty"
    if s["failed"] or s["errored"]:
        return "failed"
    if s["pass_warn"] or s["rerun"] or s["cancelled"]:
        return "partial"
    return "success"


def build_test_html(cfg: dict, commit: str, branch: str, drive_link: str,
                    run_url: str, run_id: str, s: dict) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M IST")
    verdict  = _exec_status(s)
    accent   = {"success": "#1a7f37", "partial": "#9a6700",
                "failed": "#b42318", "empty": "#6E7681"}[verdict]
    label    = {"success": "ALL PASSED", "partial": "PARTIAL PASS",
                "failed": "FAILURES", "empty": "NO TESTS"}[verdict]
    icon     = {"success": "✅", "partial": "⚠️", "failed": "🔴", "empty": "⚪"}[verdict]

    def stat(color, num, lbl):
        return (f'<td style="background:#F9FAFB;padding:12px 8px;text-align:center;'
                f'border-right:1px solid #E5E7EB">'
                f'<span style="display:block;font-size:22px;font-weight:800;color:{color}">{num}</span>'
                f'<span style="display:block;font-size:9px;font-weight:600;color:#6B7280;'
                f'text-transform:uppercase;letter-spacing:0.6px;margin-top:3px">{lbl}</span></td>')

    stat_cells = (
        stat("#111827", s["total"], "Total")
        + stat("#1a7f37", s["passed"], "Pass")
        + stat("#9a6700", s["pass_warn"], "Pass*")
        + stat("#b42318", s["failed"], "Fail")
        + stat("#b42318", s["errored"], "Error")
        + stat("#6E7681", s["rerun"] + s["cancelled"], "Rerun/Cxl")
    )

    # Failing-TC rows (cap at 25 to keep the email compact).
    fail_rows = ""
    for r in s["failing"][:25]:
        note = (r.get("note", "") or "").replace("<", "&lt;").replace(">", "&gt;")[:140]
        note_html = (f'<br><span style="color:#9B2C2C;font-size:11px">{note}</span>'
                     if note else "")
        tc_id  = r.get("test_case_id", "?")
        status = r.get("status", "")
        fail_rows += (
            f'<div style="padding:8px 12px;font-size:12px;border-bottom:1px solid #FEE2E2;'
            f'background:#FFF5F5;color:#7F1D1D">'
            f'<b>{tc_id}</b> '
            f'<span style="color:#b42318;font-weight:600">[{status}]</span>'
            f'{note_html}</div>')
    more = len(s["failing"]) - 25
    if more > 0:
        fail_rows += (f'<div style="padding:8px 12px;font-size:11px;color:#6B7280">'
                      f'…and {more} more — see the full report.</div>')
    fail_block = ""
    if fail_rows:
        fail_block = (
            f'<div style="margin:22px 0 6px"><span class="sec-lbl">Failing test cases</span></div>'
            f'<div style="border:1px solid #FEE2E2;border-radius:10px;overflow:hidden;margin-bottom:8px">{fail_rows}</div>')

    drive_block = ""
    if drive_link:
        drive_block = (
            f'<a href="{drive_link}" class="drive-btn" style="background:#1a5fa8">'
            f'<span class="drive-title">📥 Download test results</span></a>')
    else:
        drive_block = (
            '<div style="padding:12px;border:1px dashed #E5E7EB;border-radius:10px;'
            'font-size:12px;color:#6B7280;text-align:center;margin-bottom:8px">'
            'Results archive not on Drive for this run — download it from the run\'s '
            'GitHub artifacts. (See the "Upload test results" step log for the reason.)</div>')

    gh_block = ""
    if run_url:
        gh_block = (f'<a href="{run_url}" class="gh-btn"><span class="gh-text">'
                    f'↗ View run #{run_id} on GitHub (logs &amp; artifacts)</span></a>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="x-apple-disable-message-reformatting">
  <title>Matter CI Test Execution</title>
  <style>
    body{{margin:0;padding:0;background:#ECEEF2;
         font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif}}
    .outer{{padding:24px 12px}}
    .card{{max-width:560px;margin:0 auto;background:#fff;
           border-radius:16px;overflow:hidden;border:1px solid #DDE1E7}}
    .pad{{padding:0 28px}}
    .sec-lbl{{font-size:10px;font-weight:600;color:#9CA3AF;text-transform:uppercase;
              letter-spacing:0.8px}}
    .meta{{width:100%;border-collapse:collapse;border:1px solid #E5E7EB;
           border-radius:10px;margin:22px 0;table-layout:fixed}}
    .meta td{{background:#F9FAFB;padding:13px 14px;border-right:1px solid #E5E7EB;
              vertical-align:top}}
    .meta td:last-child{{border-right:none}}
    .meta-lbl{{display:block;font-size:10px;font-weight:600;color:#9CA3AF;
               text-transform:uppercase;letter-spacing:0.8px;margin-bottom:5px}}
    .meta-val{{display:block;font-size:13px;font-weight:700;color:#111827;word-break:break-all}}
    .mono{{font-family:'Courier New',monospace}}
    .drive-btn{{display:block;border-radius:10px;padding:15px 20px;text-align:center;
                text-decoration:none;margin-bottom:10px}}
    .drive-title{{display:block;font-size:14px;font-weight:700;color:#fff}}
    .gh-btn{{display:block;background:#fff;border:1px solid #E5E7EB;border-radius:10px;
             padding:11px 20px;text-align:center;text-decoration:none}}
    .gh-text{{font-size:12px;color:#4B5563;font-weight:500}}
    @media only screen and (max-width:480px){{.outer{{padding:12px 6px}}.pad{{padding:0 16px}}}}
  </style>
</head>
<body>
<div class="outer"><div class="card">
  <table width="100%" cellpadding="0" cellspacing="0" role="presentation">
    <tr><td bgcolor="#0F2752"
        style="background-color:#0F2752;background-image:linear-gradient(135deg,#0F2752 0%,#1a5fa8 55%,#0e7dc2 100%);padding:28px 28px 22px">
      <span style="display:block;font-size:10px;font-weight:600;color:rgba(255,255,255,0.6);
                   letter-spacing:1.8px;text-transform:uppercase;margin-bottom:14px">
        Granite River Labs &nbsp;&mdash;&nbsp; Matter CI Pipeline</span>
      <p style="font-size:22px;font-weight:700;color:#fff;margin:0 0 8px;line-height:1.2">
        Test execution results</p>
      <p style="font-size:12px;color:rgba(255,255,255,0.6);margin:0">
        {date_str} &nbsp;&middot;&nbsp; Raspberry Pi ARM64</p>
    </td></tr>
  </table>

  <div class="pad">
    <!-- Verdict + pass % banner -->
    <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="margin-top:22px">
      <tr>
        <td style="background:{accent};border-radius:12px;padding:18px 20px">
          <span style="display:block;font-size:12px;font-weight:700;color:rgba(255,255,255,0.85);
                       letter-spacing:0.5px">{icon} {label}</span>
          <span style="display:block;font-size:30px;font-weight:800;color:#fff;margin-top:4px">
            {s['pct']}% <span style="font-size:15px;font-weight:600;color:rgba(255,255,255,0.85)">
            pass &nbsp;({s['accepted']}/{s['total']})</span></span>
        </td>
      </tr>
    </table>

    <!-- Per-status counts -->
    <table class="meta" role="presentation"><tr>{stat_cells}</tr></table>

    <!-- Run metadata -->
    <table class="meta" role="presentation">
      <tr>
        <td><span class="meta-lbl">Run</span><span class="meta-val">#{run_id}</span></td>
        <td><span class="meta-lbl">Branch</span><span class="meta-val">{branch}</span></td>
        <td><span class="meta-lbl">SDK commit</span><span class="meta-val mono">{commit}</span></td>
      </tr>
    </table>

    {fail_block}

    <div style="margin:18px 0 10px"><span class="sec-lbl">Artifacts</span></div>
    {drive_block}
    {gh_block}

    <p style="font-size:11px;color:#9CA3AF;margin:20px 0 4px;line-height:1.6">
      Pass&nbsp;% counts full passes and partial passes (PASS*). Partial = some
      steps skipped (PICS/feature-gated or unmet precondition) but enough passed
      to accept. Full per-step detail is in the HTML report inside the archive.</p>
  </div>
  <div style="height:20px"></div>
</div></div>
</body>
</html>"""


def build_test_plain(commit: str, branch: str, drive_link: str,
                     run_url: str, run_id: str, s: dict) -> str:
    lines = [
        f"Matter CI — Test Execution Results (Run #{run_id})",
        "=" * 48,
        f"Verdict     : {_exec_status(s).upper()}",
        f"Pass rate   : {s['pct']}%  ({s['accepted']}/{s['total']} accepted)",
        f"Branch      : {branch}",
        f"SDK commit  : {commit}",
        "",
        f"Total {s['total']} | Pass {s['passed']} | Pass* {s['pass_warn']} | "
        f"Fail {s['failed']} | Error {s['errored']} | Rerun {s['rerun']} | "
        f"Cancelled {s['cancelled']}",
        "",
    ]
    if s["failing"]:
        lines.append("Failing test cases:")
        for r in s["failing"][:40]:
            note = (r.get("note", "") or "")[:120]
            lines.append(f"  - {r.get('test_case_id','?')} [{r.get('status','')}]"
                         + (f" — {note}" if note else ""))
        if len(s["failing"]) > 40:
            lines.append(f"  …and {len(s['failing']) - 40} more.")
        lines.append("")
    if drive_link:
        lines.append(f"Download results : {drive_link}")
    if run_url:
        lines.append(f"GitHub run       : {run_url}")
    return "\n".join(lines)


def send_email(cfg: dict, subject: str, html_body: str, plain_body: str):
    sender   = os.environ.get("GMAIL_SENDER", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    emails   = os.environ.get("NOTIFY_EMAILS", "")

    if not sender:
        print("[NOTIFY] ⚠️  GMAIL_SENDER not set — skipping email")
        return
    if not password:
        print("[NOTIFY] ⚠️  GMAIL_APP_PASSWORD not set — skipping email")
        return
    if not emails:
        print("[NOTIFY] ⚠️  NOTIFY_EMAILS not set — skipping email")
        return

    # Clean emails — remove spaces, newlines, carriage returns
    emails = emails.replace("\n", ",").replace("\r", "").replace(" ", "")
    recipients = [e.strip() for e in emails.split(",") if e.strip()]
    print(f"[NOTIFY] Sending email to {len(recipients)} recipient(s)...")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Matter CI <{sender}>"
    msg["To"]      = ", ".join(recipients)

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body,  "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipients, msg.as_string())
        print(f"[NOTIFY] ✅ Email sent to {len(recipients)} recipient(s)")
    except Exception as e:
        print(f"[NOTIFY] ❌ Email failed: {e}")
        sys.exit(1)


# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",
                        default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    # 'build' (default) → build email; 'execution' → test-results email.
    parser.add_argument("--mode", choices=["build", "execution"], default="build")
    parser.add_argument("--status", default="success",
                        choices=["success", "partial", "failed"])
    parser.add_argument("--drive-link", default="")
    parser.add_argument("--run-id",    default="")
    parser.add_argument("--run-url",   default="")
    # Test-execution mode: path to the test_results.json summary.
    parser.add_argument("--results",
                        default=str(PROJECT_ROOT / "logs" / "test_results.json"))
    # Optional explicit commit/branch — the caller (workflow) passes these from
    # the same build-info.json the job summary uses, guaranteeing the email
    # commit matches the build. Falls back to reading build-info.json itself.
    parser.add_argument("--commit",    default="")
    parser.add_argument("--branch",    default="")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    commit, branch = get_git_info(cfg)
    # Explicit args win over the auto-derived values.
    if args.commit:
        commit = args.commit
    if args.branch:
        branch = args.branch

    # ── Test-execution email ──────────────────────────────────────────────
    if args.mode == "execution":
        s = compute_test_summary(Path(args.results))
        verdict = _exec_status(s)
        icons = {"success": "✅", "partial": "⚠️", "failed": "🔴", "empty": "⚪"}
        subject = (f"{icons[verdict]} Matter CI Tests #{args.run_id} — "
                   f"{s['accepted']}/{s['total']} passed ({s['pct']}%) | {branch} | {commit}")
        html_body  = build_test_html(cfg, commit, branch, args.drive_link,
                                     args.run_url, args.run_id, s)
        plain_body = build_test_plain(commit, branch, args.drive_link,
                                      args.run_url, args.run_id, s)
        send_email(cfg, subject, html_body, plain_body)
        return

    # ── Build email (default) ─────────────────────────────────────────────
    build_status   = load_build_status()

    failed_apps = [k for k, v in build_status.items() if v == "FAIL"]
    passed_apps = [k for k, v in build_status.items() if v != "FAIL"]

    # Build subject line. Include the run number so every email has a UNIQUE
    # subject — otherwise same-commit rebuilds share a subject, Gmail threads
    # them into one conversation, and collapses the "repeated" parts behind a
    # "•••" (show-trimmed-content) toggle. Unique subject → no threading → no •••.
    icons = {"success": "✅", "partial": "⚠️", "failed": "🔴"}
    labels = {"success": "SUCCESS", "partial": "PARTIAL SUCCESS", "failed": "FAILED"}
    subject = (f"{icons[args.status]} Matter SDK Nightly Build #{args.run_id} — "
               f"{labels[args.status]} | {branch} | {commit}")

    html_body  = build_html(
        args.status, cfg, commit, branch,
        args.drive_link, args.run_url, args.run_id,
        failed_apps, passed_apps
    )
    plain_body = build_plain_text(
        args.status, commit, branch,
        args.drive_link, args.run_url, args.run_id,
        failed_apps, passed_apps
    )

    send_email(cfg, subject, html_body, plain_body)


if __name__ == "__main__":
    main()
