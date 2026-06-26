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
            f'<div class="app-row" style="background:#fff5f5">❌ {app}</div>'
            for app in failed_apps)
        failed_section = f"""
        <div style="margin-bottom:16px">
          <div class="section-title" style="color:#c0392b">Failed apps</div>
          <div style="border-radius:6px;overflow:hidden;border:1px solid #fde8e8">
            {items}
          </div>
        </div>"""

    # Passed apps section
    passed_section = ""
    if passed_apps:
        items = "".join(
            f'<div class="app-row-pass" style="background:#f0fff4">✅ {app}</div>'
            for app in passed_apps)
        passed_section = f"""
        <div style="margin-bottom:16px">
          <div class="section-title" style="color:#27ae60">Successfully built</div>
          <div style="border-radius:6px;overflow:hidden;border:1px solid #e8f8ee">
            {items}
          </div>
        </div>"""

    # Download section (only for success/partial)
    download_section = ""
    if drive_link and status in ("success", "partial"):
        download_section = f"""
        <div style="margin-bottom:16px">
          <div class="section-label">Download and install</div>
          <div class="code-wrap">
            <div class="code-hdr">
              <div class="code-dot" style="background:#FF5F57"></div>
              <div class="code-dot" style="background:#FFBD2E"></div>
              <div class="code-dot" style="background:#28C840"></div>
              <span class="code-label">Raspberry Pi terminal</span>
            </div>
            <div class="code-body">
              <span class="code-comment"># install gdown</span><br>
              pip3 install gdown --break-system-packages<br>
              <span class="code-comment"># download bundle</span><br>
              gdown {file_id}<br>
              <span class="code-comment"># extract and install</span><br>
              tar -xzf matter-sdk*.tar.gz<br>
              cd matter-sdk-*/<br>
              chmod +x install.sh &amp;&amp; ./install.sh
            </div>
          </div>
          <a href="{drive_link}" class="drive-btn">
            <div class="drive-btn-title">Open in Google Drive</div>
            <div class="drive-btn-sub">{bundle_name}</div>
          </a>
        </div>"""

    # Actions link section
    actions_section = ""
    if run_url:
        actions_section = f"""
        <a href="{run_url}" class="gh-btn">
          <span class="gh-btn-text">View GitHub Actions run #{run_id}</span>
        </a>"""

    # Status pill colours
    if status == "success":
        pill_bg    = "#052E16"
        pill_border= "#166534"
        pill_dot   = "#4ADE80"
        pill_text  = "#4ADE80"
    elif status == "partial":
        pill_bg    = "#422006"
        pill_border= "#92400E"
        pill_dot   = "#FBBF24"
        pill_text  = "#FBBF24"
    else:
        pill_bg    = "#1C0A0A"
        pill_border= "#7F1D1D"
        pill_dot   = "#F87171"
        pill_text  = "#F87171"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="x-apple-disable-message-reformatting">
  <title>Matter SDK Build</title>
  <style>
    body {{
      margin: 0; padding: 0;
      background: #ECEEF2;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
      -webkit-text-size-adjust: 100%;
      -ms-text-size-adjust: 100%;
    }}
    .wrapper {{
      width: 100%;
      padding: 20px 12px;
      box-sizing: border-box;
    }}
    .card {{
      max-width: 580px;
      margin: 0 auto;
      background: #ffffff;
      border-radius: 16px;
      overflow: hidden;
      border: 1px solid #E0E3E8;
    }}
    .hdr {{
      background: #0F1923;
      padding: 28px 28px 20px;
    }}
    .hdr-brand-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 14px;
    }}
    .hdr-dot {{
      width: 7px; height: 7px;
      border-radius: 50%;
      background: #3B82F6;
      flex-shrink: 0;
    }}
    .hdr-brand {{
      font-size: 10px;
      font-weight: 600;
      color: #475569;
      letter-spacing: 1.2px;
      text-transform: uppercase;
    }}
    .hdr-title {{
      font-size: 22px;
      font-weight: 700;
      color: #F8FAFC;
      letter-spacing: -0.3px;
      line-height: 1.2;
      margin-bottom: 6px;
    }}
    .hdr-sub {{
      font-size: 12px;
      color: #64748B;
    }}
    .status-area {{
      background: #0F1923;
      padding: 0 28px 22px;
    }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      background: {pill_bg};
      border: 1px solid {pill_border};
      border-radius: 8px;
      padding: 8px 14px;
      margin-bottom: 8px;
    }}
    .status-pill-dot {{
      width: 7px; height: 7px;
      border-radius: 50%;
      background: {pill_dot};
      flex-shrink: 0;
    }}
    .status-pill-text {{
      font-size: 12px;
      font-weight: 700;
      color: {pill_text};
      letter-spacing: 0.3px;
      text-transform: uppercase;
    }}
    .status-desc {{
      font-size: 12px;
      color: #64748B;
    }}
    .body {{
      padding: 22px 28px;
    }}
    /* 3-col meta — collapses to 1-col on small screens */
    .meta-table {{
      width: 100%;
      border-collapse: collapse;
      border-radius: 10px;
      overflow: hidden;
      border: 1px solid #E0E3E8;
      margin-bottom: 20px;
    }}
    .meta-table td {{
      background: #F8FAFC;
      padding: 12px 14px;
      vertical-align: top;
      width: 33.33%;
      border-right: 1px solid #E0E3E8;
    }}
    .meta-table td:last-child {{
      border-right: none;
    }}
    .meta-label {{
      font-size: 10px;
      font-weight: 600;
      color: #94A3B8;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      margin-bottom: 4px;
      display: block;
    }}
    .meta-value {{
      font-size: 13px;
      font-weight: 700;
      color: #1E293B;
      word-break: break-all;
    }}
    .meta-mono {{ font-family: 'Courier New', monospace; }}
    /* Download section label */
    .section-label {{
      font-size: 10px;
      font-weight: 600;
      color: #94A3B8;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      margin-bottom: 12px;
      border-bottom: 1px solid #E0E3E8;
      padding-bottom: 8px;
    }}
    /* Code block */
    .code-wrap {{
      background: #0D1117;
      border-radius: 10px;
      overflow: hidden;
      border: 1px solid #1E293B;
      margin-bottom: 14px;
    }}
    .code-hdr {{
      background: #161B22;
      padding: 9px 14px;
      border-bottom: 1px solid #1E293B;
      display: flex;
      align-items: center;
      gap: 7px;
    }}
    .code-dot {{
      width: 10px; height: 10px;
      border-radius: 50%;
      flex-shrink: 0;
    }}
    .code-label {{
      font-size: 10px;
      color: #64748B;
      font-weight: 500;
      margin-left: 4px;
    }}
    .code-body {{
      padding: 14px 16px;
      font-family: 'Courier New', Courier, monospace;
      font-size: 12px;
      line-height: 1.9;
      color: #E2E8F0;
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }}
    .code-comment {{ color: #6A9955; }}
    /* Drive button */
    .drive-btn {{
      display: block;
      background: #1D4ED8;
      border-radius: 9px;
      padding: 14px 20px;
      text-align: center;
      text-decoration: none;
      margin-bottom: 10px;
    }}
    .drive-btn-title {{
      font-size: 14px;
      font-weight: 700;
      color: #ffffff;
      margin-bottom: 3px;
    }}
    .drive-btn-sub {{
      font-size: 10px;
      color: #93C5FD;
      word-break: break-all;
    }}
    /* GitHub button */
    .gh-btn {{
      display: block;
      background: #F8FAFC;
      border: 1px solid #E0E3E8;
      border-radius: 9px;
      padding: 11px 20px;
      text-align: center;
      text-decoration: none;
      margin-bottom: 4px;
    }}
    .gh-btn-text {{
      font-size: 12px;
      color: #475569;
      font-weight: 500;
    }}
    /* Failed / passed sections */
    .section-title {{
      font-size: 12px;
      font-weight: 600;
      margin-bottom: 8px;
    }}
    .app-row {{
      padding: 6px 12px;
      font-size: 12px;
      border-bottom: 1px solid #fde8e8;
    }}
    .app-row-pass {{
      padding: 6px 12px;
      font-size: 12px;
      border-bottom: 1px solid #e8f8ee;
    }}
    /* Footer */
    .footer {{
      padding: 14px 28px;
      border-top: 1px solid #F1F5F9;
      background: #FAFBFC;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 6px;
    }}
    .footer-brand {{
      font-size: 10px;
      font-weight: 700;
      color: #94A3B8;
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }}
    .footer-auto {{
      font-size: 10px;
      color: #CBD5E1;
    }}
    /* Mobile overrides */
    @media only screen and (max-width: 480px) {{
      .hdr, .status-area, .body {{ padding-left: 18px !important; padding-right: 18px !important; }}
      .hdr-title {{ font-size: 19px !important; }}
      .meta-table, .meta-table tbody,
      .meta-table tr, .meta-table td {{
        display: block !important;
        width: 100% !important;
        box-sizing: border-box !important;
      }}
      .meta-table td {{
        border-right: none !important;
        border-bottom: 1px solid #E0E3E8 !important;
      }}
      .meta-table td:last-child {{ border-bottom: none !important; }}
      .code-body {{ font-size: 11px !important; }}
      .drive-btn-sub {{ font-size: 9px !important; }}
      .footer {{ flex-direction: column; align-items: flex-start; }}
    }}
  </style>
</head>
<body>
<div class="wrapper">
<div class="card">

  <div class="hdr">
    <div class="hdr-brand-row">
      <div class="hdr-dot"></div>
      <span class="hdr-brand">Granite River Labs &nbsp;—&nbsp; Matter CI</span>
    </div>
    <div class="hdr-title">Matter SDK build</div>
    <div class="hdr-sub">{date_str} &nbsp;·&nbsp; Raspberry Pi ARM64</div>
  </div>

  <div class="status-area">
    <div class="status-pill">
      <div class="status-pill-dot"></div>
      <span class="status-pill-text">{banner_text}</span>
    </div>
    <div class="status-desc">{sub_text}</div>
  </div>

  <div class="body">

    <!-- Meta grid — 3 col desktop, 1 col mobile -->
    <table class="meta-table" cellpadding="0" cellspacing="0" role="presentation">
      <tr>
        <td><span class="meta-label">Branch</span>
            <span class="meta-value">{branch}</span></td>
        <td><span class="meta-label">Commit</span>
            <span class="meta-value meta-mono">{commit}</span></td>
        <td><span class="meta-label">Run ID</span>
            <span class="meta-value">#{run_id}</span></td>
      </tr>
    </table>

    {failed_section}
    {passed_section}
    {download_section}
    {actions_section}

  </div>

  <div class="footer">
    <span class="footer-brand">GRL &nbsp;·&nbsp; GRLPS Matter Team</span>
    <span class="footer-auto">Automated notification</span>
  </div>

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
