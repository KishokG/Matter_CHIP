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

def get_git_info(cfg: dict) -> tuple[str, str]:
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
    status_file = PROJECT_ROOT / "logs" / "build_logs" / "build_status.json"
    if status_file.exists():
        with open(status_file) as f:
            return json.load(f)
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
            '<td style="padding:10px 0 10px 14px;vertical-align:middle">'
            '<table cellpadding="0" cellspacing="0" role="presentation"><tr>'
            '<td style="width:11px;height:11px;border-radius:50%;'
            'background:#FF5F57;font-size:1px;line-height:1px">&nbsp;</td>'
            '<td style="width:6px">&nbsp;</td>'
            '<td style="width:11px;height:11px;border-radius:50%;'
            'background:#FFBD2E;font-size:1px;line-height:1px">&nbsp;</td>'
            '<td style="width:6px">&nbsp;</td>'
            '<td style="width:11px;height:11px;border-radius:50%;'
            'background:#28C840;font-size:1px;line-height:1px">&nbsp;</td>'
            '</tr></table></td>'
            '<td style="padding:10px 14px;font-size:10px;color:#6E7681;'
            'font-weight:500;vertical-align:middle">Raspberry Pi terminal</td>'
            '</tr></table>'

            # Code body — each line as separate <div> for reliable rendering
            '<div style="background:#0D1117;padding:14px 16px;'
            'font-family:Courier New,Courier,monospace;font-size:12px;'
            'line-height:2;color:#E6EDF3;word-break:break-word">'
            '<div><span style="color:#7EE787"># install gdown</span></div>'
            '<div>pip3 install gdown --break-system-packages</div>'
            '<div><span style="color:#7EE787"># download bundle</span></div>'
            f'<div>gdown {file_id}</div>'
            '<div><span style="color:#7EE787"># extract and install</span></div>'
            '<div>tar -xzf matter-sdk*.tar.gz</div>'
            '<div>cd matter-sdk-*/</div>'
            '<div>chmod +x install.sh &amp;&amp; ./install.sh</div>'
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

    lines = [
        "=" * 60,
        "  Matter SDK Nightly Build — Granite River Labs",
        f"  {date_str}",
        "=" * 60,
        f"  Status : {status.upper()}",
        f"  Branch : {branch}",
        f"  Commit : {commit}",
        f"  Run ID : #{run_id}",
        "",
    ]

    if failed_apps:
        lines += ["Failed Apps:", *[f"  ❌ {a}" for a in failed_apps], ""]
    if passed_apps:
        lines += ["Built Apps:", *[f"  ✅ {a}" for a in passed_apps], ""]

    if drive_link and status in ("success", "partial"):
        lines += [
            "Download & Install:",
            "  pip3 install gdown --break-system-packages",
            f"  gdown {file_id}",
            "  tar -xzf matter-sdk*.tar.gz",
            "  cd matter-sdk-*/",
            "  chmod +x install.sh && ./install.sh",
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
    parser.add_argument("--status",    required=True,
                        choices=["success", "partial", "failed"])
    parser.add_argument("--drive-link", default="")
    parser.add_argument("--run-id",    default="")
    parser.add_argument("--run-url",   default="")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    commit, branch = get_git_info(cfg)
    build_status   = load_build_status()

    failed_apps = [k for k, v in build_status.items() if v == "FAIL"]
    passed_apps = [k for k, v in build_status.items() if v != "FAIL"]

    # Build subject line
    icons = {"success": "✅", "partial": "⚠️", "failed": "🔴"}
    labels = {"success": "SUCCESS", "partial": "PARTIAL SUCCESS", "failed": "FAILED"}
    subject = (f"{icons[args.status]} Matter SDK Nightly Build — "
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
