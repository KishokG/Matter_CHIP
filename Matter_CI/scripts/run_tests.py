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


# =============================================================================
# Log parser
# Looks for: "Test results: Error X, Executed X, Failed X, Passed X, ..."
# =============================================================================
def parse_result(log_text: str) -> tuple[str, dict]:
    """Returns (status, counts_dict)"""
    pattern = (r"Test results:\s*"
               r"Error\s+(\d+),\s*"
               r"Executed\s+(\d+),\s*"
               r"Failed\s+(\d+),\s*"
               r"Passed\s+(\d+),\s*"
               r"Requested\s+(\d+),\s*"
               r"Skipped\s+(\d+)")

    match = re.search(pattern, log_text, re.IGNORECASE)
    if not match:
        return ERROR, {}

    counts = {
        "error":     int(match.group(1)),
        "executed":  int(match.group(2)),
        "failed":    int(match.group(3)),
        "passed":    int(match.group(4)),
        "requested": int(match.group(5)),
        "skipped":   int(match.group(6)),
    }

    if counts["failed"] > 0:
        return FAIL, counts
    # All steps skipped — needs investigation
    if counts["executed"] > 0 and counts["skipped"] == counts["executed"]:
        return RERUN, counts
    return PASS, counts


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
                                "DUT failed to launch")

        # Build and run python command
        cmd_parts = self._build_python_cmd(py_cmd)
        print(f"  [TEST] Running: {' '.join(cmd_parts[:4])}...")

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
            status, counts = parse_result(log_text)
            elapsed = round(time.time() - start, 2)
            print(f"  [{status}] {tc_id} — {elapsed}s  {counts}")

        except subprocess.TimeoutExpired:
            elapsed = round(time.time() - start, 2)
            status, counts = ERROR, {}
            with open(log_path, "a") as lf:
                lf.write(f"\n\n[CI] TIMEOUT after {self.timeout}s\n")
            print(f"  [TIMEOUT] {tc_id} after {elapsed}s")

        except Exception as exc:
            elapsed = round(time.time() - start, 2)
            status, counts = ERROR, {}
            print(f"  [ERROR] {tc_id}: {exc}")

        finally:
            dut.stop()
            self._clean_storage()   # clean up after test too

        return self._result(tc, status, counts, elapsed, log_path)

    def _result(self, tc, status, counts, elapsed, log_path, note=""):
        return {
            "test_case_id":   tc["test_case_id"],
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

        for i, tc in enumerate(self.commands, 1):
            print(f"\n[{i}/{len(self.commands)}]", end="")
            result = self.run_one(tc, dut)
            self.results.append(result)

        return self.results


# =============================================================================
# HTML Report generator
# =============================================================================
def generate_report(results: list[dict], cfg: dict) -> Path:
    report_path = PROJECT_ROOT / cfg["test_execution"]["report_path"]
    report_path.parent.mkdir(parents=True, exist_ok=True)

    total  = len(results)
    passed = sum(1 for r in results if r["status"] == PASS)
    failed = sum(1 for r in results if r["status"] == FAIL)
    rerun  = sum(1 for r in results if r["status"] == RERUN)
    errors = sum(1 for r in results if r["status"] == ERROR)
    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    colour = {PASS: "#28a745", FAIL: "#dc3545", RERUN: "#fd7e14", ERROR: "#6c757d"}

    def badge(status):
        c = colour.get(status, "#000")
        return f'<span style="background:{c};color:#fff;padding:2px 8px;border-radius:4px;font-size:0.85em">{status}</span>'

    rows_html = ""
    for r in results:
        c = r["counts"]
        counts_str = ""
        if c:
            counts_str = (f"P:{c.get('passed',0)} F:{c.get('failed',0)} "
                          f"S:{c.get('skipped',0)} E:{c.get('error',0)}")
        log_name = Path(r["log_file"]).name
        rows_html += f"""
        <tr>
          <td><b>{r['test_case_id']}</b></td>
          <td>{badge(r['status'])}</td>
          <td>{counts_str}</td>
          <td>{r['elapsed_s']}s</td>
          <td style="font-size:0.75em;color:#555">{r['python_command'][:70]}...</td>
          <td><a href="test_runs/{log_name}" target="_blank">📄 log</a></td>
          <td style="color:#888;font-size:0.75em">{r.get('note','')}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Matter CI — Test Report</title>
  <style>
    body  {{ font-family: Arial, sans-serif; margin: 30px; background: #f8f9fa; color: #333; }}
    h1    {{ color: #2c3e50; }}
    .summary {{ display: flex; gap: 16px; margin: 20px 0; flex-wrap: wrap; }}
    .card {{ padding: 14px 24px; border-radius: 10px; color: #fff; text-align: center; min-width: 90px; }}
    .pass  {{ background: #28a745; }}
    .fail  {{ background: #dc3545; }}
    .rerun {{ background: #fd7e14; }}
    .err   {{ background: #6c757d; }}
    .total {{ background: #2c3e50; }}
    table  {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
    th     {{ background: #2c3e50; color: #fff; padding: 10px 14px; text-align: left; font-size: 0.9em; }}
    td     {{ padding: 8px 14px; font-size: 0.88em; border-bottom: 1px solid #eee; }}
    tr:hover {{ background: #f1f5f9; }}
    .num   {{ font-size: 2em; font-weight: bold; }}
  </style>
</head>
<body>
  <h1>🔬 Matter CI — Test Report</h1>
  <p style="color:#666">Generated: {run_time}</p>

  <div class="summary">
    <div class="card total"><div class="num">{total}</div>TOTAL</div>
    <div class="card pass"><div class="num">{passed}</div>PASSED</div>
    <div class="card fail"><div class="num">{failed}</div>FAILED</div>
    <div class="card rerun"><div class="num">{rerun}</div>RERUN</div>
    <div class="card err"><div class="num">{errors}</div>ERROR</div>
  </div>

  <table>
    <thead>
      <tr>
        <th>TC ID</th>
        <th>Status</th>
        <th>Counts</th>
        <th>Time</th>
        <th>Command</th>
        <th>Log</th>
        <th>Note</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
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
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
