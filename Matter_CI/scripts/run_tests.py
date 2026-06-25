#!/usr/bin/env python3
"""
run_tests.py
============
For each test case in test_commands.json:
  1. Clean /tmp/chip_* and admin_storage.json
  2. Launch the DUT sample app (background)
  3. Activate python controller venv
  4. Run the python3 test script
  5. Capture full output to <TC_ID>.log
  6. Parse PASS / FAIL / RERUN / ERROR from log
  7. Stop the DUT
  8. Generate HTML report

Usage:
    python3 scripts/run_tests.py [--config config/build_config.yaml]
                                  [--commands logs/test_commands.json]
"""

import os
import re
import sys
import json
import signal
import shutil
import subprocess
import time
import argparse
from datetime import datetime
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent

# =============================================================================
# Global cancel flag — set by SIGTERM/SIGINT handler
# =============================================================================
_CANCEL_REQUESTED = False
_ACTIVE_DUT: "DUTManager | None" = None   # track running DUT for cleanup


def _signal_handler(signum, frame):
    """
    Handles SIGTERM (GitHub Actions cancel) and SIGINT (Ctrl+C).
    Sets cancel flag so the test loop exits cleanly after current TC.
    Also kills the active DUT immediately.
    """
    global _CANCEL_REQUESTED, _ACTIVE_DUT
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    print(f"\n[CANCEL] {sig_name} received — stopping after current test...")
    _CANCEL_REQUESTED = True

    # Kill active DUT immediately so it doesn't keep running
    if _ACTIVE_DUT is not None:
        print("[CANCEL] Stopping active DUT...")
        _ACTIVE_DUT.stop()

    # Kill any stray chip processes
    subprocess.run("pkill -f 'chip-.*-app' 2>/dev/null || true", shell=True)
    subprocess.run("pkill -f 'matter-.*-app' 2>/dev/null || true", shell=True)
    subprocess.run("rm -f /tmp/chip_* 2>/dev/null || true", shell=True)
    print("[CANCEL] Cleanup done. Saving results collected so far...")


# Register signal handlers
signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT,  _signal_handler)


# =============================================================================
# Config
# =============================================================================
def load_config(path: Path) -> dict:
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


# =============================================================================
# Result constants
# =============================================================================
PASS   = "PASS"
FAIL   = "FAIL"
RERUN  = "RERUN"
ERROR  = "ERROR"
CANCEL = "CANCEL"


# =============================================================================
# Log parser — Multi-signal pass/fail detection
#
# Signal priority:
#   1. Exception/crash detected          → ERROR  (overrides everything)
#   2. CommissioningError detected       → ERROR  (DUT pairing failed)
#   3. Mobly "Test results:" summary     → parse counts → PASS/FAIL/RERUN
#   4. Non-zero exit code, no summary    → FAIL
#   5. Per-step "***** Fail *****" found → FAIL   (fallback)
#   6. Nothing found                     → ERROR
# =============================================================================
def parse_result(log_text: str, exit_code: int = 0) -> tuple[str, dict, str]:
    """
    Returns (status, counts_dict, reason_string).
    reason_string is empty for PASS, populated for all other statuses.
    """

    # ── Signal 1 — Exception / script crash ───────────────────────────────────
    exc_match = re.search(
        r"ERROR\s+Exception occurred in test_(\w+)", log_text, re.IGNORECASE)
    if exc_match:
        exc_type = re.search(
            r"(AssertionError|TimeoutError|AttributeError|ValueError|"
            r"RuntimeError|ChipStackError|InteractionModelError|"
            r"MatterStackException|Exception)[^\r\n]*",
            log_text)
        reason = (
            f"Exception in {exc_match.group(1)}: {exc_type.group(0)}"
            if exc_type else
            f"Exception in {exc_match.group(1)} — script crashed"
        )
        return ERROR, {}, reason

    # ── Signal 2 — Commissioning / pairing failure ────────────────────────────
    if re.search(
            r"CommissioningError|Failed to commission|"
            r"Commissioning complete failed|"
            r"CHIP_ERROR_CONNECTION_ABORTED|"
            r"Failed to pair with device|"
            r"Unable to find the device",
            log_text, re.IGNORECASE):
        return ERROR, {}, (
            "Commissioning failed — DUT could not be paired. "
            "Check discriminator, passcode, and that DUT is in commissioning mode."
        )

    # ── Signal 3 — Mobly summary line ─────────────────────────────────────────
    summary_pattern = (
        r"Test results:\s*"
        r"Error\s+(\d+),\s*"
        r"Executed\s+(\d+),\s*"
        r"Failed\s+(\d+),\s*"
        r"Passed\s+(\d+),\s*"
        r"Requested\s+(\d+),\s*"
        r"Skipped\s+(\d+)"
    )
    match = re.search(summary_pattern, log_text, re.IGNORECASE)
    if match:
        counts = {
            "error":     int(match.group(1)),
            "executed":  int(match.group(2)),
            "failed":    int(match.group(3)),
            "passed":    int(match.group(4)),
            "requested": int(match.group(5)),
            "skipped":   int(match.group(6)),
        }

        if counts["failed"] > 0 or counts["error"] > 0:
            # Find which step failed for better reason message
            fail_step = re.search(
                r"Test Step\s+(\S+)", log_text)
            parts = []
            if counts["failed"] > 0:
                parts.append(f"{counts['failed']} step(s) failed")
            if counts["error"] > 0:
                parts.append(f"{counts['error']} error(s)")
            reason = ", ".join(parts)
            return FAIL, counts, reason

        # All steps skipped → needs investigation
        if counts["executed"] > 0 and counts["skipped"] == counts["executed"]:
            return RERUN, counts, (
                f"All {counts['skipped']}/{counts['executed']} steps skipped — "
                "possible PICS mismatch, unsupported feature, or DUT config issue"
            )

        # Clean pass
        return PASS, counts, ""

    # ── Signal 4 — No summary + non-zero exit code ────────────────────────────
    if exit_code != 0:
        error_lines = [
            line.strip() for line in log_text.splitlines()
            if re.search(r"\bERROR\b|\bFAIL\b|exception|traceback",
                         line, re.IGNORECASE)
        ]
        hint = error_lines[-1][:120] if error_lines else "Check log for details"
        return FAIL, {}, (
            f"Script exited with code {exit_code} — no result summary found. "
            f"Last error hint: {hint}"
        )

    # ── Signal 5 — Per-step fail lines (fallback) ─────────────────────────────
    if re.search(r"\*\*\*\*\*\s*Fail\s*\*\*\*\*\*", log_text, re.IGNORECASE):
        return FAIL, {}, "Step failure detected in log (no summary line found)"

    # ── Signal 6 — Nothing found ──────────────────────────────────────────────
    return ERROR, {}, (
        "No result summary found — script may have crashed, timed out, "
        "or failed before running any steps"
    )

# =============================================================================
# DUT manager
# =============================================================================
class DUTManager:
    def __init__(self, cfg: dict):
        self.sdk_dir   = Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))
        self.cfg       = cfg
        self._proc     = None
        self._log_file = None
        self._app_name = None

    def _find_binary(self, dut_cmd: str) -> Path | None:
        """
        Match binary name in dut_cmd against build_config apps list.
        e.g. './chip-all-clusters-app' → build_dir/chip-all-clusters-app
        """
        # Extract binary name from command (after ./ and before first space/flag)
        match = re.search(r'\./([^\s]+)', dut_cmd)
        if not match:
            return None
        bin_name = match.group(1)

        # Search enabled apps in config
        for app in self.cfg.get("apps", []):
            if app.get("binary_name") == bin_name and app.get("enabled"):
                binary = self.sdk_dir / app["build_dir"] / app["binary_name"]
                if binary.exists():
                    return binary

        # Also check chip_tool binary
        ct = self.cfg.get("chip_tool", {})
        if ct.get("binary_name") == bin_name:
            binary = self.sdk_dir / ct["build_dir"] / ct["binary_name"]
            if binary.exists():
                return binary

        return None

    def launch(self, dut_cmd: str, log_path: Path) -> bool:
        """Launch DUT app in background. Returns True on success."""
        global _ACTIVE_DUT
        _ACTIVE_DUT = self
        binary = self._find_binary(dut_cmd)
        if not binary:
            print(f"  [DUT] Binary not found for command: {dut_cmd[:60]}")
            return False

        # Replace ./binary-name with actual full path
        bin_match = re.search(r'\./([^\s]+)', dut_cmd)
        full_cmd  = dut_cmd.replace(bin_match.group(0), str(binary))

        # The rm -rf part runs first, then the binary
        # We need to run it as shell command so && works
        print(f"  [DUT] Launching: {full_cmd[:80]}...")

        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(log_path, 'w')
        self._proc = subprocess.Popen(
            full_cmd,
            shell=True,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
            cwd=str(binary.parent),   # run from the binary's directory
        )

        wait = self.cfg["test_execution"].get("dut_startup_wait", 5)
        print(f"  [DUT] Waiting {wait}s for startup...")
        time.sleep(wait)

        if self._proc.poll() is not None:
            print(f"  [DUT] Process exited immediately (rc={self._proc.returncode})")
            return False

        print(f"  [DUT] Running (PID {self._proc.pid})")
        return True

    def stop(self):
        global _ACTIVE_DUT
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                self._proc.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            print("  [DUT] Stopped.")
        if self._log_file:
            self._log_file.close()
        self._proc = None
        self._log_file = None
        _ACTIVE_DUT = None


# =============================================================================
# Test runner
# =============================================================================
class TestRunner:
    def __init__(self, cfg: dict, commands: list[dict]):
        self.cfg      = cfg
        self.commands = commands
        self.sdk_dir  = Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))
        self.timeout  = cfg["test_execution"].get("timeout_seconds", 600)
        self.log_dir  = PROJECT_ROOT / cfg["test_execution"]["log_dir"]
        self.admin_storage = cfg["test_execution"].get("admin_storage", "admin_storage.json")
        self.scripts_dir   = self.sdk_dir / "src" / "python_testing"
        self.venv_name     = cfg["python_controller"].get("install_venv_name", "python_env")
        self.venv_python   = self.sdk_dir / self.venv_name / "bin" / "python3"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.results: list[dict] = []

    def _clean_storage(self):
        """Remove admin_storage.json before AND after each test.
        Checks both PROJECT_ROOT and scripts_dir (where python runs from).
        """
        storage_name = self.admin_storage

        # Remove from all possible locations
        for search_dir in [PROJECT_ROOT, self.scripts_dir]:
            admin = search_dir / storage_name
            if admin.exists():
                admin.unlink()
                print(f"  [CLEAN] Removed {admin}")

        # Clean chip tmp files (prevents state bleed between tests)
        subprocess.run(
            "rm -f /tmp/chip_*.ini /tmp/chip_*.json /tmp/chip_kvs",
            shell=True, capture_output=True
        )

    def _build_python_cmd(self, raw_py_cmd: str) -> list[str]:
        """Replace 'python3' with venv python and expand TC script path."""
        # Replace python3 with venv python
        cmd = raw_py_cmd
        if cmd.startswith("python3 "):
            # Extract script name (e.g. TC_ACE_1_2.py)
            parts = cmd.split()
            script_name = parts[1]
            script_path = self.scripts_dir / script_name
            if script_path.exists():
                parts[1] = str(script_path)
            parts[0] = str(self.venv_python)
            return parts
        return cmd.split()

    def run_one(self, tc: dict, dut: DUTManager) -> dict:
        tc_id     = tc["test_case_id"]
        dut_cmd   = tc["dut_command"]
        py_cmd    = tc["python_command"]
        log_path  = self.log_dir / f"{tc_id}.log"
        dut_log   = self.log_dir / f"{tc_id}_dut.log"

        print(f"\n── {tc_id} ──────────────────────────────────")

        start = time.time()

        # Clean storage before test
        self._clean_storage()

        # Launch DUT
        if not dut.launch(dut_cmd, dut_log):
            elapsed = round(time.time() - start, 2)
            return self._result(tc, ERROR, {}, elapsed, log_path,
                                note="DUT failed to launch — binary not found or exited immediately")

        # Build and run python command
        cmd_parts = self._build_python_cmd(py_cmd)
        print(f"  [TEST] Running: {' '.join(cmd_parts[:4])}...")

        reason = ""
        try:
            with open(log_path, "w") as lf:
                proc = subprocess.run(
                    cmd_parts,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    timeout=self.timeout,
                    cwd=str(self.scripts_dir),
                    env={**os.environ,
                         "PATH": f"{self.venv_python.parent}:{os.environ.get('PATH','')}"},
                )
            log_text = log_path.read_text(errors="replace")
            # Pass exit code for Signal 4 detection
            status, counts, reason = parse_result(log_text, exit_code=proc.returncode)
            elapsed = round(time.time() - start, 2)
            reason_short = f" | {reason[:60]}" if reason else ""
            print(f"  [{status}] {tc_id} — {elapsed}s  {counts}{reason_short}")

        except subprocess.TimeoutExpired:
            elapsed = round(time.time() - start, 2)
            status, counts, reason = ERROR, {}, f"Test timed out after {self.timeout}s"
            with open(log_path, "a") as lf:
                lf.write(f"\n\n[CI] TIMEOUT after {self.timeout}s\n")
            print(f"  [TIMEOUT] {tc_id} after {elapsed}s")

        except Exception as exc:
            elapsed = round(time.time() - start, 2)
            status, counts, reason = ERROR, {}, f"Runner exception: {exc}"
            print(f"  [ERROR] {tc_id}: {exc}")

        finally:
            dut.stop()
            self._clean_storage()   # clean up after test too

        return self._result(tc, status, counts, elapsed, log_path, note=reason)

    def _result(self, tc, status, counts, elapsed, log_path, note=""):
        return {
            "test_case_id":   tc["test_case_id"],
            "cluster":        tc.get("cluster", ""),
            "dut_command":    tc["dut_command"],
            "python_command": tc["python_command"],
            "status":         status,
            "counts":         counts,
            "elapsed_s":      elapsed,
            "log_file":       str(log_path),
            "note":           note,
        }

    def run_all(self) -> list[dict]:
        dut = DUTManager(self.cfg)
        print(f"\n[TEST] Running {len(self.commands)} test case(s)...")
        print(f"[TEST] Python venv : {self.venv_python}")
        print(f"[TEST] Scripts dir : {self.scripts_dir}")
        print(f"[TEST] Send SIGTERM or click Cancel in GitHub to stop cleanly.")

        for i, tc in enumerate(self.commands, 1):
            # Check cancel flag before starting each new test
            if _CANCEL_REQUESTED:
                print(f"\n[CANCEL] Cancelled before TC {tc['test_case_id']} — stopping.")
                # Mark remaining TCs as cancelled
                for remaining in self.commands[i-1:]:
                    self.results.append({
                        "test_case_id":   remaining["test_case_id"],
                        "cluster":        remaining.get("cluster", ""),
                        "dut_command":    remaining["dut_command"],
                        "python_command": remaining["python_command"],
                        "status":         "CANCEL",
                        "counts":         {},
                        "elapsed_s":      0,
                        "log_file":       "",
                        "note":           "Cancelled by user (SIGTERM/SIGINT)",
                    })
                break

            print(f"\n[{i}/{len(self.commands)}]", end="")
            result = self.run_one(tc, dut)
            self.results.append(result)

        if _CANCEL_REQUESTED:
            cancelled = sum(1 for r in self.results if r["status"] == "CANCEL")
            ran       = len(self.results) - cancelled
            print(f"\n[CANCEL] Ran {ran} test(s) before cancel. {cancelled} skipped.")

        return self.results


# =============================================================================
# HTML Report generator — enhanced with filters, cluster grouping, log links
# =============================================================================
def extract_cluster(tc_id: str, cluster: str = "") -> str:
    """
    Returns cluster name from test_commands.json (full name from tc_list.json),
    falls back to extracting abbreviation from TC ID.
    e.g. "Access Control Enforcement" or "ACE"
    """
    if cluster:
        return cluster
    match = re.match(r'TC-([A-Z]+(?:-[A-Z]+)*)-', tc_id, re.IGNORECASE)
    return match.group(1).upper() if match else "OTHER"


def generate_report(results: list[dict], cfg: dict) -> Path:
    report_path = PROJECT_ROOT / cfg["test_execution"]["report_path"]
    report_path.parent.mkdir(parents=True, exist_ok=True)

    total    = len(results)
    passed   = sum(1 for r in results if r["status"] == PASS)
    failed   = sum(1 for r in results if r["status"] == FAIL)
    rerun    = sum(1 for r in results if r["status"] == RERUN)
    errors   = sum(1 for r in results if r["status"] == ERROR)
    cancelled= sum(1 for r in results if r["status"] == CANCEL)
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Collect unique clusters for filter dropdown
    clusters = sorted(set(extract_cluster(r["test_case_id"], r.get("cluster", "")) for r in results))

    colour = {PASS: "#28a745", FAIL: "#dc3545", RERUN: "#fd7e14", ERROR: "#6c757d", CANCEL: "#8e44ad"}

    def badge(status):
        c = colour.get(status, "#000")
        return (f'<span class="badge" style="background:{c}">{status}</span>')

    def status_reason(r):
        """
        Returns reason string for display in report.
        For FAIL/RERUN/ERROR: uses the reason from parse_result (stored in note).
        """
        status = r["status"]
        note   = r.get("note", "")

        if status == PASS:
            return ""
        if status == CANCEL:
            return "Cancelled by user before this test started"
        # For all other statuses — reason is pre-populated by parse_result
        # Fall back to generic messages only if note is empty
        if note:
            return note
        counts = r.get("counts", {})
        if status == FAIL:
            f = counts.get("failed", 0)
            e = counts.get("error", 0)
            parts = []
            if f: parts.append(f"{f} step(s) failed")
            if e: parts.append(f"{e} error(s)")
            return ", ".join(parts) if parts else "Test failed"
        if status == RERUN:
            s = counts.get("skipped", 0)
            ex = counts.get("executed", 0)
            return f"All {s}/{ex} steps skipped — re-run to investigate"
        if status == ERROR:
            return "Test script did not produce a result — may have crashed or timed out"
        return ""

    # Build rows
    rows_html = ""
    for r in results:
        tc_id   = r["test_case_id"]
        cluster = extract_cluster(tc_id, r.get("cluster", ""))
        status  = r["status"]
        counts  = r.get("counts", {})
        elapsed = r["elapsed_s"]
        log_file = Path(r.get("log_file", ""))
        dut_log  = log_file.parent / f"{tc_id}_dut.log" if log_file.name else None

        counts_str = ""
        if counts:
            counts_str = (f"✅{counts.get('passed',0)} "
                          f"❌{counts.get('failed',0)} "
                          f"⏭{counts.get('skipped',0)} "
                          f"⚠️{counts.get('error',0)}")

        reason = status_reason(r)

        # Log links
        log_links = ""
        if log_file.exists() or log_file.name:
            log_links += (f'<a href="test_runs/{log_file.name}" target="_blank" ')
            log_links += f'class="log-link ctrl-log">📋 Ctrl Log</a> '
        if dut_log and (dut_log.exists() or dut_log.name):
            log_links += (f'<a href="test_runs/{dut_log.name}" target="_blank" ')
            log_links += f'class="log-link dut-log">🖥 DUT Log</a>'

        row_class = f"row-{status.lower()}"
        reason_cell = f'<span class="reason">{reason}</span>' if reason else ""

        rows_html += f"""
        <tr class="tc-row {row_class}" data-cluster="{cluster}" data-status="{status}">
          <td><b>{tc_id}</b><br><small class="cluster-tag">{cluster}</small></td>
          <td>{badge(status)}</td>
          <td class="counts">{counts_str}</td>
          <td>{elapsed}s</td>
          <td class="log-cell">{log_links}</td>
          <td class="reason-cell">{reason_cell}</td>
        </tr>"""

    cluster_options = "".join(
        f'<option value="{c}">{c}</option>' for c in clusters
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Matter CI — Test Report</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', Arial, sans-serif;
      background: #f0f2f5;
      color: #2c3e50;
      padding: 24px;
    }}
    .header {{
      background: linear-gradient(135deg, #1a252f, #2c3e50);
      color: white;
      padding: 24px 32px;
      border-radius: 12px;
      margin-bottom: 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .header h1 {{ font-size: 1.6em; font-weight: 600; }}
    .header .meta {{ font-size: 0.85em; opacity: 0.8; margin-top: 4px; }}

    .summary {{
      display: flex;
      gap: 14px;
      margin-bottom: 24px;
      flex-wrap: wrap;
    }}
    .card {{
      flex: 1;
      min-width: 100px;
      padding: 16px 20px;
      border-radius: 10px;
      color: #fff;
      text-align: center;
      box-shadow: 0 2px 8px rgba(0,0,0,0.12);
    }}
    .card .num {{ font-size: 2.2em; font-weight: 700; line-height: 1; }}
    .card .lbl {{ font-size: 0.75em; font-weight: 600; letter-spacing: 1px; margin-top: 4px; opacity: 0.9; }}
    .c-total  {{ background: linear-gradient(135deg, #2c3e50, #34495e); }}
    .c-pass   {{ background: linear-gradient(135deg, #27ae60, #2ecc71); }}
    .c-fail   {{ background: linear-gradient(135deg, #c0392b, #e74c3c); }}
    .c-rerun  {{ background: linear-gradient(135deg, #d35400, #e67e22); }}
    .c-err    {{ background: linear-gradient(135deg, #7f8c8d, #95a5a6); }}
    .c-cancel {{ background: linear-gradient(135deg, #7d3c98, #8e44ad); }}

    .filters {{
      background: #fff;
      border-radius: 10px;
      padding: 16px 20px;
      margin-bottom: 20px;
      display: flex;
      gap: 16px;
      align-items: center;
      flex-wrap: wrap;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    }}
    .filters label {{ font-size: 0.85em; font-weight: 600; color: #555; }}
    .filters select, .filters input {{
      padding: 6px 12px;
      border: 1px solid #ddd;
      border-radius: 6px;
      font-size: 0.88em;
      background: #fafafa;
      cursor: pointer;
    }}
    .filter-btn {{
      padding: 6px 16px;
      border: none;
      border-radius: 6px;
      font-size: 0.85em;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.2s;
    }}
    .filter-btn:hover {{ opacity: 0.8; }}
    .btn-all  {{ background: #2c3e50; color: #fff; }}
    .btn-fail {{ background: #e74c3c; color: #fff; }}
    .btn-pass {{ background: #27ae60; color: #fff; }}
    .btn-rerun{{ background: #e67e22; color: #fff; }}

    .table-wrap {{
      background: #fff;
      border-radius: 10px;
      box-shadow: 0 1px 4px rgba(0,0,0,0.08);
      overflow: hidden;
    }}
    table {{ border-collapse: collapse; width: 100%; }}
    th {{
      background: #2c3e50;
      color: #fff;
      padding: 12px 16px;
      text-align: left;
      font-size: 0.85em;
      font-weight: 600;
      letter-spacing: 0.5px;
    }}
    td {{ padding: 10px 16px; font-size: 0.88em; border-bottom: 1px solid #f0f2f5; vertical-align: top; }}
    tr.tc-row:hover {{ background: #f8fafc; }}
    tr.tc-row {{ transition: background 0.15s; }}

    .badge {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 20px;
      color: #fff;
      font-size: 0.8em;
      font-weight: 700;
      letter-spacing: 0.5px;
    }}
    .cluster-tag {{
      background: #eaf0fb;
      color: #2980b9;
      padding: 1px 6px;
      border-radius: 4px;
      font-size: 0.75em;
      margin-top: 3px;
      display: inline-block;
    }}
    .counts {{ font-size: 0.82em; color: #555; white-space: nowrap; }}

    .log-link {{
      display: inline-block;
      padding: 3px 8px;
      border-radius: 5px;
      font-size: 0.8em;
      font-weight: 600;
      text-decoration: none;
      margin-right: 4px;
      margin-bottom: 2px;
    }}
    .ctrl-log {{ background: #eaf0fb; color: #2980b9; border: 1px solid #aed6f1; }}
    .ctrl-log:hover {{ background: #d6eaf8; }}
    .dut-log  {{ background: #fef9e7; color: #d68910; border: 1px solid #f9e79f; }}
    .dut-log:hover {{ background: #fdebd0; }}

    .reason {{ font-size: 0.82em; color: #666; line-height: 1.4; }}
    tr.row-fail .reason {{ color: #c0392b; }}
    tr.row-rerun .reason {{ color: #d35400; }}
    tr.row-error .reason {{ color: #7f8c8d; }}

    .hidden {{ display: none; }}

    .no-results {{
      text-align: center;
      padding: 40px;
      color: #aaa;
      font-size: 1em;
    }}

    #result-count {{
      font-size: 0.85em;
      color: #888;
      margin-left: auto;
    }}
  </style>
</head>
<body>
  <div class="header">
    <div>
      <h1>🔬 Matter CI — Test Report</h1>
      <div class="meta">Generated: {run_time}</div>
    </div>
  </div>

  <div class="summary">
    <div class="card c-total" ><div class="num">{total}</div><div class="lbl">TOTAL</div></div>
    <div class="card c-pass"  ><div class="num">{passed}</div><div class="lbl">PASSED</div></div>
    <div class="card c-fail"  ><div class="num">{failed}</div><div class="lbl">FAILED</div></div>
    <div class="card c-rerun" ><div class="num">{rerun}</div><div class="lbl">RERUN</div></div>
    <div class="card c-err"   ><div class="num">{errors}</div><div class="lbl">ERROR</div></div>
    <div class="card c-cancel"><div class="num">{cancelled}</div><div class="lbl">CANCELLED</div></div>
  </div>

  <div class="filters">
    <label>Cluster:</label>
    <select id="clusterFilter" onchange="applyFilters()">
      <option value="ALL">All Clusters</option>
      {cluster_options}
    </select>

    <label>Status:</label>
    <select id="statusFilter" onchange="applyFilters()">
      <option value="ALL">All Statuses</option>
      <option value="PASS">PASS</option>
      <option value="FAIL">FAIL</option>
      <option value="RERUN">RERUN</option>
      <option value="ERROR">ERROR</option>
      <option value="CANCEL">CANCELLED</option>
    </select>

    <label>Search:</label>
    <input type="text" id="searchFilter" placeholder="TC ID..." oninput="applyFilters()">

    <button class="filter-btn btn-all"  onclick="setStatus('ALL')">All</button>
    <button class="filter-btn btn-fail" onclick="setStatus('FAIL')">Failed</button>
    <button class="filter-btn btn-pass" onclick="setStatus('PASS')">Passed</button>
    <button class="filter-btn btn-rerun" onclick="setStatus('RERUN')">Rerun</button>

    <span id="result-count"></span>
  </div>

  <div class="table-wrap">
    <table id="resultsTable">
      <thead>
        <tr>
          <th>TC ID</th>
          <th>Status</th>
          <th>Steps</th>
          <th>Time</th>
          <th>Logs</th>
          <th>Reason / Notes</th>
        </tr>
      </thead>
      <tbody id="tableBody">
        {rows_html}
      </tbody>
    </table>
    <div class="no-results hidden" id="noResults">No test cases match the current filters.</div>
  </div>

  <script>
    function applyFilters() {{
      const cluster = document.getElementById('clusterFilter').value;
      const status  = document.getElementById('statusFilter').value;
      const search  = document.getElementById('searchFilter').value.toLowerCase();
      const rows    = document.querySelectorAll('.tc-row');
      let visible   = 0;

      rows.forEach(row => {{
        const rowCluster = row.dataset.cluster;
        const rowStatus  = row.dataset.status;
        const rowText    = row.textContent.toLowerCase();

        const clusterOk = cluster === 'ALL' || rowCluster === cluster;
        const statusOk  = status  === 'ALL' || rowStatus  === status;
        const searchOk  = search  === ''    || rowText.includes(search);

        if (clusterOk && statusOk && searchOk) {{
          row.classList.remove('hidden');
          visible++;
        }} else {{
          row.classList.add('hidden');
        }}
      }});

      document.getElementById('result-count').textContent =
        `Showing ${{visible}} of {total} test cases`;
      document.getElementById('noResults').classList.toggle('hidden', visible > 0);
    }}

    function setStatus(s) {{
      document.getElementById('statusFilter').value = s;
      applyFilters();
    }}

    // Init count
    document.getElementById('result-count').textContent = 'Showing {total} of {total} test cases';
  </script>
</body>
</html>"""

    report_path.write_text(html)
    print(f"\n[REPORT] Written to: {report_path}")
    return report_path


# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",   default=str(PROJECT_ROOT / "config" / "build_config.yaml"))
    parser.add_argument("--commands", default=str(PROJECT_ROOT / "logs" / "test_commands.json"))
    args = parser.parse_args()

    cfg = load_config(Path(args.config))

    cmd_path = Path(args.commands)
    if not cmd_path.exists():
        print(f"[ERROR] {cmd_path} not found. Run fetch_test_commands.py first.")
        sys.exit(1)

    with open(cmd_path) as f:
        commands = json.load(f)

    if not commands:
        print("[WARN] No commands to run.")
        sys.exit(0)

    runner  = TestRunner(cfg, commands)
    results = runner.run_all()
    generate_report(results, cfg)

    # Save JSON results too
    results_path = PROJECT_ROOT / "logs" / "test_results.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"[INFO] Results JSON: {results_path}")

    failed = sum(1 for r in results if r["status"] in (FAIL, ERROR))
    # Exit 0 on cancel — partial results are expected
    if _CANCEL_REQUESTED:
        print("[CANCEL] Exiting with code 0 — partial results saved.")
        sys.exit(0)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
