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

# Reference apps are resolved dynamically from the SDK (see discover_targets.py)
# instead of a hardcoded apps: block in build_config.yaml.
sys.path.insert(0, str(SCRIPT_DIR))
from discover_targets import resolve_pipeline_apps

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
PASS      = "PASS"
PASS_WARN = "PASS*"    # pass with some steps skipped (PICS/feature-gated or precondition)
FAIL      = "FAIL"
RERUN     = "RERUN"
ERROR     = "ERROR"
CANCEL    = "CANCEL"


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
def _clean_detail(text: str) -> str:
    """Tidy a failure detail: drop the 'Details=' prefix and ', Extras=None' tail."""
    text = text.strip()
    text = re.sub(r"^Details=", "", text)
    text = re.sub(r",?\s*Extras=None\s*$", "", text).strip()
    return text[:300]


def parse_result(log_text: str, exit_code: int = 0) -> tuple[str, dict, str]:
    """
    Returns (status, counts_dict, reason_string).
    reason_string is empty for PASS, populated for all other statuses.
    """

    # ── Signal 1 — Exception / script crash ─────────────────────────────────
    # Covers both: "Exception occurred in test_XXX"
    # and:         "Error in ClassName#setup_class"
    exc_match = re.search(
        r"ERROR\s+(?:Exception occurred in test_(\w+)|Error in (\w+)#setup_class)",
        log_text, re.IGNORECASE)
    if exc_match:
        test_name = exc_match.group(1) or exc_match.group(2)
        is_setup  = "setup_class" in exc_match.group(0)
        phase     = "setup_class" if is_setup else "test"

        # IMPORTANT: extract the reason from the ACTUAL failure — search only the
        # log AFTER the exception marker. Grepping the whole log grabs benign
        # startup errors (e.g. the WiFi-PAF / NFC / ThreadMeshcop discovery
        # "CHIP Error 0x2F: Invalid argument" printed before commissioning),
        # which are NOT why the test failed.
        tail = log_text[exc_match.start():]

        # A mobly TestFailure / AssertionError is a genuine test FAILURE (the DUT
        # gave a wrong result), not a harness ERROR. Extract its Details message.
        is_assertion = False
        reason_detail = None
        m = re.search(r"(?:mobly\.signals\.TestFailure|AssertionError):\s*(.+)", tail)
        if m:
            is_assertion = True
            reason_detail = _clean_detail(m.group(1))
        if not reason_detail:
            # The "failed for the following reason:" banner (multi-line, each
            # continuation prefixed with "* ").
            m = re.search(
                r"failed for the following reason:\s*\n\*\s*(.+?)\n\*\s*(?:\n|File)",
                tail, re.DOTALL)
            if m:
                reason_detail = _clean_detail(re.sub(r"\n\*\s*", " ", m.group(1)))
        if not reason_detail:
            # A genuine crash — pick the real exception type/message from the
            # traceback (NOT a stray CHIP error from before the test).
            m = re.search(
                r"\b(ChipStackError|TimeoutError|asyncio\.TimeoutError|"
                r"AttributeError|ValueError|KeyError|IndexError|TypeError|"
                r"RuntimeError|InteractionModelError)\b[^\r\n]*", tail)
            if m:
                reason_detail = m.group(0).strip()
        if not reason_detail:
            # Last resort: a CHIP error, but only one that appears AFTER the
            # exception marker (i.e. in the failure/traceback region).
            chip_errs = re.findall(r"CHIP Error (0x[0-9A-Fa-f]+):\s*([^\n]+)", tail)
            if chip_errs:
                code, msg = chip_errs[0]
                reason_detail = f"CHIP Error {code}: {msg.strip()}"

        if reason_detail:
            reason = f"{phase} failed in {test_name}: {reason_detail}"
        else:
            reason = (f"{phase} failed in {test_name} — "
                      f"crashed before running any test steps")

        # Assertion failures in a test body → FAIL (DUT behaved incorrectly).
        # Everything else (setup crashes, real exceptions) → ERROR (harness/DUT
        # couldn't complete the test).
        status = FAIL if (is_assertion and not is_setup) else ERROR
        return status, {}, reason

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

        # ── Signal 3b — Step-level skips (PICS-gated steps) ─────────────────
        # Mobly's "Skipped" counter = test-level skips (whole test skipped)
        # PICS-gated step skips appear as "**** Skipping: N" lines in the log
        # These do NOT appear in the summary counts — must be parsed separately
        step_skips = re.findall(r"\*\*\*\*\s*Skipping:\s*(\d+)", log_text)
        # Deduplicate — each skipped step appears twice in the log
        unique_skipped_steps = len(set(step_skips))

        # Partial skip — some steps passed, some steps skipped → PASS*
        # NOTE: we do NOT claim the skips are caused by missing PICS. A step can
        # skip for several reasons (a PICS/feature guard, an unmet precondition,
        # or a genuine issue). We state the fact and point to where to look.
        if unique_skipped_steps > 0 and counts["passed"] > 0:
            return PASS_WARN, counts, (
                f"Partial execution: {counts['passed']} step(s) passed, "
                f"{unique_skipped_steps} step(s) skipped. Skips may be "
                f"PICS/feature-gated (if so, set pics_folder) or due to an unmet "
                f"precondition — check the Ctrl Log for each 'Skipping' reason."
            )

        # All steps skipped at STEP level (no passed steps at all)
        if unique_skipped_steps > 0 and counts["passed"] == 0:
            return RERUN, counts, (
                f"All {unique_skipped_steps} step(s) skipped — this may be "
                f"PICS/feature-gated (if the test needs it, set pics_folder), an "
                f"unsupported feature, or another issue. Check the Ctrl Log for "
                f"the 'Skipping' reasons."
            )

        # Partial skip from summary counts (test-level)
        if counts["skipped"] > 0 and counts["passed"] > 0:
            return PASS_WARN, counts, (
                f"Partial execution: {counts['passed']} step(s) passed, "
                f"{counts['skipped']}/{counts['executed']} step(s) skipped. Skips "
                f"may be PICS/feature-gated (if so, set pics_folder) or due to an "
                f"unmet precondition — check the Ctrl Log."
            )

        # Clean pass — all steps executed and passed
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
        # Resolve the reference-app list once (dynamic discovery) — same list
        # the build produced. Used to map a DUT command to its binary.
        self.apps      = resolve_pipeline_apps(self.sdk_dir, cfg)
        self._proc     = None
        self._log_file = None
        self._app_name = None

    def _find_binary(self, dut_cmd: str) -> tuple[Path | None, str]:
        """
        Find binary for DUT command from the dynamically discovered app list
        (plus chip_tool). Returns (binary_path, error_message).
        If binary not found, returns (None, reason).
        """
        match = re.search(r"\./([^\s]+)", dut_cmd)
        if not match:
            return None, "Could not extract binary name from DUT command"
        bin_name = match.group(1)

        # Check the dynamically discovered reference apps
        for app in self.apps:
            if app.get("binary_name") == bin_name:
                if not app.get("enabled"):
                    return None, (
                        f"App '{app['name']}' is not enabled for build. "
                        f"Add it to discovery.include in build_config.yaml."
                    )
                binary = self.sdk_dir / app["build_dir"] / app["binary_name"]
                if binary.exists():
                    return binary, ""
                else:
                    return None, (
                        f"Binary '{bin_name}' is configured but not built. "
                        f"Expected at: {binary}. "
                        f"Run pipeline with build mode to compile it first."
                    )

        # Check chip_tool
        ct = self.cfg.get("chip_tool", {})
        if ct.get("binary_name") == bin_name:
            binary = self.sdk_dir / ct["build_dir"] / ct["binary_name"]
            if binary.exists():
                return binary, ""
            return None, f"chip-tool not built. Expected at: {binary}"

        # Not among discovered apps or chip_tool
        return None, (
            f"Binary '{bin_name}' was not produced by this build. "
            f"Add the app's SDK shorthand to discovery.include in "
            f"build_config.yaml and rebuild, or check that the DUT command "
            f"uses the correct binary name."
        )


    def launch(self, dut_cmd: str, log_path: Path) -> tuple[bool, str]:
        """
        Launch DUT app in background.
        Returns (True, "") on success, (False, error_reason) on failure.
        """
        global _ACTIVE_DUT
        _ACTIVE_DUT = self
        binary, err = self._find_binary(dut_cmd)
        if not binary:
            print(f"  [DUT] ❌ {err}")
            return False, err

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
            cwd=str(binary.parent),
        )

        wait = self.cfg["test_execution"].get("dut_startup_wait", 5)
        print(f"  [DUT] Waiting {wait}s for startup...")
        time.sleep(wait)

        if self._proc.poll() is not None:
            rc = self._proc.returncode
            print(f"  [DUT] ❌ Process exited immediately (rc={rc})")
            return False, f"DUT process exited immediately with rc={rc}"

        print(f"  [DUT] ✅ Running (PID {self._proc.pid})")
        return True, ""

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
        if not self.venv_python.exists():
            print(f"[ERROR] Python venv not found: {self.venv_python}")
            print(f"[ERROR] Run pipeline with build mode to install python controller first")
            sys.exit(1)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.results: list[dict] = []
        # Retry settings
        self.retry_on_commissioning = cfg["test_execution"].get(
            "retry_on_commissioning_failure", 3)
        self.retry_on_step_failure  = cfg["test_execution"].get(
            "retry_on_step_failure", 1)
        # PICS folder path (resolved at runtime for --PICS placeholder)
        # The SDK reads all XML files from the folder and picks the right one per cluster
        pics_folder = cfg["test_execution"].get("pics_folder", "")
        self.pics_folder = pics_folder  # expected to be an absolute path on RPi

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
        """
        Replace 'python3' with venv python, expand TC script path,
        and resolve --PICS placeholder with actual PICS file path from config.
        """
        cmd = raw_py_cmd

        # Fix 4: Resolve --PICS placeholder with PICS folder path
        # The SDK reads all XML files in the folder and picks the right one per cluster
        if "__PICS_PLACEHOLDER__" in cmd:
            if self.pics_folder and Path(self.pics_folder).is_dir():
                cmd = cmd.replace("--PICS __PICS_PLACEHOLDER__",
                                  f"--PICS {self.pics_folder}")
                print(f"  [PICS] Using PICS folder: {self.pics_folder}")
            else:
                # Remove --PICS entirely if folder not configured or not found
                cmd = cmd.replace("--PICS __PICS_PLACEHOLDER__", "").strip()
                if self.pics_folder:
                    print(f"  [WARN] PICS folder not found: {self.pics_folder} — removing --PICS flag")
                else:
                    print("  [WARN] --PICS in command but pics_folder not set in config — removing --PICS flag")

        if cmd.startswith("python3 "):
            parts = cmd.split()
            script_name = parts[1]
            script_path = self.scripts_dir / script_name
            if script_path.exists():
                parts[1] = str(script_path)
            parts[0] = str(self.venv_python)
            return parts
        return cmd.split()

    def _run_attempt(self, tc: dict, dut: DUTManager,
                     attempt: int, log_path: Path, dut_log: Path) -> tuple:
        """Single test attempt. Returns (status, counts, reason, elapsed)."""
        tc_id   = tc["test_case_id"]
        dut_cmd = tc["dut_command"]
        py_cmd  = tc["python_command"]

        if attempt > 1:
            suffix = f"_attempt{attempt}"
            log_path  = log_path.parent / (log_path.stem + suffix + ".log")
            dut_log   = dut_log.parent  / (dut_log.stem  + suffix + ".log")
            print(f"  [RETRY] Attempt {attempt}...")

        self._clean_storage()
        start = time.time()

        launched, launch_err = dut.launch(dut_cmd, dut_log)
        if not launched:
            elapsed = round(time.time() - start, 2)
            return ERROR, {}, launch_err, elapsed

        cmd_parts = self._build_python_cmd(py_cmd)
        print(f"  [TEST] Running: {' '.join(str(p) for p in cmd_parts[:4])}...")

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
            log_text  = log_path.read_text(errors="replace")
            status, counts, reason = parse_result(log_text, exit_code=proc.returncode)

        except subprocess.TimeoutExpired:
            status, counts, reason = ERROR, {}, f"Test timed out after {self.timeout}s"
            with open(log_path, "a") as lf:
                lf.write(f"\n\n[CI] TIMEOUT after {self.timeout}s\n")

        except Exception as exc:
            status, counts, reason = ERROR, {}, f"Runner exception: {exc}"

        finally:
            dut.stop()
            self._clean_storage()

        elapsed = round(time.time() - start, 2)
        return status, counts, reason, elapsed

    def run_one(self, tc: dict, dut: DUTManager) -> dict:
        tc_id    = tc["test_case_id"]
        log_path = self.log_dir / f"{tc_id}.log"
        dut_log  = self.log_dir / f"{tc_id}_dut.log"

        print(f"\n── {tc_id} ──────────────────────────────────")

        # ── Fix 1 & 2: Retry logic ────────────────────────────────────────────
        # Commissioning failure → retry up to retry_on_commissioning times
        # Step failure         → retry up to retry_on_step_failure times

        commissioning_attempts = 0
        step_retry_done        = False
        status = counts = reason = None
        elapsed = 0.0
        final_log = log_path

        max_comm_retries = self.retry_on_commissioning
        max_step_retries = self.retry_on_step_failure

        attempt = 1
        while True:
            status, counts, reason, elapsed = self._run_attempt(
                tc, dut, attempt, log_path, dut_log)

            reason_short = f" | {reason[:70]}" if reason else ""
            print(f"  [{status}] {tc_id} — {elapsed}s  {counts}{reason_short}")

            # Determine if we should retry
            is_commissioning_error = (
                status == ERROR and
                reason and "Commissioning" in reason
            )
            is_step_failure = status == FAIL

            if is_commissioning_error and commissioning_attempts < max_comm_retries:
                commissioning_attempts += 1
                print(f"  [RETRY] Commissioning failed — retry {commissioning_attempts}/{max_comm_retries}")
                time.sleep(3)   # brief wait before retry
                attempt += 1
                continue

            if is_step_failure and not step_retry_done and max_step_retries > 0:
                step_retry_done = True
                print(f"  [RETRY] Step failure — retrying once...")
                time.sleep(2)
                attempt += 1
                continue

            # No more retries needed/available
            break

        # Build retry note for report
        retry_note = ""
        if commissioning_attempts > 0:
            retry_note = f"(commissioning retried {commissioning_attempts}x) "
        if step_retry_done:
            retry_note += "(step failure retried 1x) "
        if retry_note:
            reason = retry_note.strip() + " | " + (reason or "")

        # Use the last log file as final log
        if attempt > 1:
            final_log = log_path.parent / f"{log_path.stem}_attempt{attempt}.log"
            if not final_log.exists():
                final_log = log_path

        return self._result(tc, status, counts, elapsed, final_log, note=reason)

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


def read_build_info() -> dict:
    """
    Build metadata (commit/branch/date) of the DUT binaries under test, written
    by prepare_rpi_tests.py to logs/build-info.json from the downloaded bundle.
    Returns {} if unavailable (e.g. a local build that skipped the bundle prep).
    """
    info_file = PROJECT_ROOT / "logs" / "build-info.json"
    if info_file.exists():
        try:
            return json.loads(info_file.read_text())
        except Exception:
            pass
    return {}


def generate_report(results: list[dict], cfg: dict) -> Path:
    report_path = PROJECT_ROOT / cfg["test_execution"]["report_path"]
    report_path.parent.mkdir(parents=True, exist_ok=True)

    bi          = read_build_info()
    bi_commit   = bi.get("commit_short") or bi.get("commit") or "unknown"
    bi_branch   = bi.get("branch", "unknown")
    bi_date     = bi.get("date", "")
    build_meta  = (f" · SDK commit <b>{bi_commit}</b> (branch <b>{bi_branch}</b>"
                   + (f", built {bi_date}" if bi_date else "") + ")")

    total     = len(results)
    passed    = sum(1 for r in results if r["status"] == PASS)
    pass_warn = sum(1 for r in results if r["status"] == PASS_WARN)
    failed    = sum(1 for r in results if r["status"] == FAIL)
    rerun     = sum(1 for r in results if r["status"] == RERUN)
    errors    = sum(1 for r in results if r["status"] == ERROR)
    cancelled = sum(1 for r in results if r["status"] == CANCEL)
    run_time  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Collect unique clusters for filter dropdown
    clusters = sorted(set(extract_cluster(r["test_case_id"], r.get("cluster", "")) for r in results))

    colour = {
        PASS:      "#28a745",
        PASS_WARN: "#5a9e3a",   # slightly muted green with warning indicator
        FAIL:      "#dc3545",
        RERUN:     "#fd7e14",
        ERROR:     "#6c757d",
        CANCEL:    "#8e44ad",
    }

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
        if status == PASS_WARN:
            return note   # show the partial skip warning
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
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 24px;
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
    .card {{ position: relative; transition: transform .15s; cursor: default; }}
    .card:hover {{ transform: translateY(-2px); }}
    .card .num {{ font-size: 2em; font-weight: 600; line-height: 1; }}
    .card .lbl {{ font-size: 0.7em; font-weight: 600; letter-spacing: .8px; text-transform: uppercase; margin-top: 4px; opacity: .88; }}
    .card .tip {{
      position: absolute; bottom: calc(100% + 6px); left: 50%;
      transform: translateX(-50%); background: #fff; color: #444;
      border: 1px solid #ddd; border-radius: 6px; padding: 7px 10px;
      font-size: 11px; line-height: 1.5; white-space: normal; text-align: left;
      min-width: 160px; max-width: 220px; pointer-events: none;
      opacity: 0; transition: opacity .2s; z-index: 99;
    }}
    .card:hover .tip {{ opacity: 1; }}
    .c-total    {{ background: #2c3e50; }}
    .c-pass     {{ background: #27ae60; }}
    .c-passwarn {{ background: #1e8449; border: 2px solid #f39c12; }}
    .c-fail     {{ background: #c0392b; }}
    .c-rerun    {{ background: #d35400; }}
    .c-err      {{ background: #7f8c8d; }}
    .c-cancel   {{ background: #7d3c98; }}

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
      <div class="meta">Generated: {run_time}{build_meta}</div>
    </div>
  </div>

  <div class="summary">
    <div class="card c-total">
      <div class="num">{total}</div><div class="lbl">Total</div>
      <div class="tip">Total number of test cases in this run</div>
    </div>
    <div class="card c-pass">
      <div class="num">{passed}</div><div class="lbl">Passed</div>
      <div class="tip">All steps executed and passed — clean result</div>
    </div>
    <div class="card c-passwarn">
      <div class="num">{pass_warn}</div><div class="lbl">Pass*</div>
      <div class="tip"><b>Partial execution</b> — some steps passed, others skipped. Skips may be PICS/feature-gated (if so, set pics_folder) or due to an unmet precondition — check the Ctrl Log for each reason.</div>
    </div>
    <div class="card c-fail">
      <div class="num">{failed}</div><div class="lbl">Failed</div>
      <div class="tip">One or more test steps failed. Check Reason column and Ctrl Log for details.</div>
    </div>
    <div class="card c-rerun">
      <div class="num">{rerun}</div><div class="lbl">Rerun</div>
      <div class="tip">All steps skipped — may be PICS/feature-gated, an unsupported feature, or another issue. Check the Ctrl Log for the skip reasons.</div>
    </div>
    <div class="card c-err">
      <div class="num">{errors}</div><div class="lbl">Error</div>
      <div class="tip">Script crashed, timed out, or commissioning failed before steps ran. Check Ctrl Log for traceback.</div>
    </div>
    <div class="card c-cancel">
      <div class="num">{cancelled}</div><div class="lbl">Cancelled</div>
      <div class="tip">Run was cancelled (SIGTERM / GitHub Actions cancel). These TCs were not executed.</div>
    </div>
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
      <option value="PASS*">PASS* (partial)</option>
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
    try:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        with open(results_path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"[INFO] Results JSON : {results_path}")
    except Exception as e:
        print(f"[WARN] Could not save results JSON: {e}")
        print(f"[WARN] Results will not be available for report or summary")

    failed = sum(1 for r in results if r["status"] in (FAIL, ERROR))
    # PASS_WARN is not a failure — just a warning that PICS was not configured
    # Exit 0 on cancel — partial results are expected
    if _CANCEL_REQUESTED:
        print("[CANCEL] Exiting with code 0 — partial results saved.")
        sys.exit(0)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
