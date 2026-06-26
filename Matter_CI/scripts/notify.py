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

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M IST")
    file_id  = drive_link.split("/d/")[1].split("/")[0] if "/d/" in drive_link else ""

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
        items = "".join(f"""
            <tr>
              <td style="padding:6px 12px;border-bottom:1px solid #fde8e8">
                ❌ {app}
              </td>
            </tr>""" for app in failed_apps)
        failed_section = f"""
        <div style="margin:20px 0">
          <p style="font-weight:600;color:#c0392b;margin:0 0 8px">Failed Apps:</p>
          <table style="width:100%;border-collapse:collapse;background:#fff5f5;
                        border-radius:6px;overflow:hidden">
            {items}
          </table>
        </div>"""

    # Passed apps section
    passed_section = ""
    if passed_apps:
        items = "".join(f"""
            <tr>
              <td style="padding:6px 12px;border-bottom:1px solid #e8f8ee">
                ✅ {app}
              </td>
            </tr>""" for app in passed_apps)
        passed_section = f"""
        <div style="margin:20px 0">
          <p style="font-weight:600;color:#27ae60;margin:0 0 8px">
            Successfully Built:
          </p>
          <table style="width:100%;border-collapse:collapse;background:#f0fff4;
                        border-radius:6px;overflow:hidden">
            {items}
          </table>
        </div>"""

    # Download section (only for success/partial)
    download_section = ""
    if drive_link and status in ("success", "partial"):
        download_section = f"""
        <div style="margin:24px 0;padding:20px;background:#f8f9fa;
                    border-radius:8px;border-left:4px solid #2c3e50">
          <p style="font-weight:700;font-size:15px;margin:0 0 12px;color:#2c3e50">
            📥 Download & Install
          </p>
          <p style="margin:0 0 10px;color:#555;font-size:13px">
            Run these commands on your Raspberry Pi:
          </p>
          <div style="background:#1e1e1e;border-radius:6px;padding:16px;
                      font-family:monospace;font-size:13px;color:#d4d4d4">
            <div style="color:#569cd6"># Install gdown</div>
            <div style="margin-bottom:8px">
              pip3 install gdown --break-system-packages
            </div>
            <div style="color:#569cd6"># Download bundle</div>
            <div style="margin-bottom:8px">gdown {file_id}</div>
            <div style="color:#569cd6"># Extract and install</div>
            <div style="margin-bottom:4px">tar -xzf matter-sdk*.tar.gz</div>
            <div style="margin-bottom:4px">cd matter-sdk-*/</div>
            <div>chmod +x install.sh &amp;&amp; ./install.sh</div>
          </div>
          <div style="margin-top:14px;text-align:center">
            <a href="{drive_link}"
               style="display:inline-block;background:#2c3e50;color:#fff;
                      padding:10px 24px;border-radius:6px;text-decoration:none;
                      font-weight:600;font-size:14px">
              🔗 Open in Google Drive
            </a>
          </div>
        </div>"""

    # Actions link section
    actions_section = ""
    if run_url:
        actions_section = f"""
        <div style="margin-top:20px;text-align:center">
          <a href="{run_url}"
             style="display:inline-block;background:#f8f9fa;color:#2c3e50;
                    padding:8px 20px;border-radius:6px;text-decoration:none;
                    font-size:13px;border:1px solid #dee2e6">
            📋 View GitHub Actions Run #{run_id}
          </a>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Segoe UI',Arial,sans-serif">
  <div style="max-width:620px;margin:30px auto;background:#fff;
              border-radius:10px;overflow:hidden;
              box-shadow:0 2px 12px rgba(0,0,0,0.1)">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#1B3A5C,#2E6DA4);
                padding:28px 32px;text-align:center">
      <div style="font-size:13px;color:rgba(255,255,255,0.7);margin-bottom:4px">
        Granite River Labs — Matter CI Pipeline
      </div>
      <div style="font-size:22px;font-weight:700;color:#fff">
        🔬 Matter SDK Nightly Build
      </div>
      <div style="font-size:13px;color:rgba(255,255,255,0.8);margin-top:4px">
        {date_str}
      </div>
    </div>

    <!-- Status Banner -->
    <div style="background:{banner_color};padding:16px 32px;text-align:center">
      <div style="font-size:20px;font-weight:700;color:#fff">
        {banner_icon} {banner_text}
      </div>
      <div style="font-size:13px;color:rgba(255,255,255,0.9);margin-top:4px">
        {sub_text}
      </div>
    </div>

    <!-- Body -->
    <div style="padding:28px 32px">

      <!-- Build info table -->
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;
                    border-radius:8px;overflow:hidden;
                    border:1px solid #e9ecef">
        <tr style="background:#f8f9fa">
          <td style="padding:8px 16px;font-weight:600;font-size:13px;
                     color:#555;width:35%;border-bottom:1px solid #e9ecef">
            Branch
          </td>
          <td style="padding:8px 16px;font-size:13px;
                     border-bottom:1px solid #e9ecef">
            {branch}
          </td>
        </tr>
        <tr>
          <td style="padding:8px 16px;font-weight:600;font-size:13px;
                     color:#555;background:#f8f9fa;border-bottom:1px solid #e9ecef">
            Commit
          </td>
          <td style="padding:8px 16px;font-size:13px;font-family:monospace;
                     border-bottom:1px solid #e9ecef">
            {commit}
          </td>
        </tr>
        <tr style="background:#f8f9fa">
          <td style="padding:8px 16px;font-weight:600;font-size:13px;color:#555">
            Run ID
          </td>
          <td style="padding:8px 16px;font-size:13px">
            #{run_id}
          </td>
        </tr>
      </table>

      {failed_section}
      {passed_section}
      {download_section}
      {actions_section}

    </div>

    <!-- Footer -->
    <div style="background:#f8f9fa;padding:16px 32px;text-align:center;
                border-top:1px solid #e9ecef">
      <p style="margin:0;font-size:12px;color:#aaa">
        This is an automated notification from Matter CI Pipeline.<br>
        Granite River Labs — GRLPS Matter Team
      </p>
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
