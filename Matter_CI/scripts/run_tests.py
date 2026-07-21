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
import shlex
import shutil
import subprocess
import time
import threading
import queue
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


def count_steps(log_text: str) -> dict:
    """
    Count the REAL test steps from the log — not the mobly test-level 'Executed'
    number (which counts CommissionDeviceTest + the TC as 2). Each step is
    announced by '***** Test Step <id> :' and a skipped one by '**** Skipping:
    <id>'. Both lines are emitted 2-3x per step, so we dedupe by <id> (which may
    be alphanumeric like '1a', '20a'). Returns step_total / step_skipped /
    step_passed (passed = ran-and-not-skipped; a failing step is subtracted at
    display time based on final status).
    """
    step_ids = set(re.findall(r"\*{5}\s*Test Step\s+([\w.]+)\s*:", log_text))
    skip_ids = set(re.findall(r"\*{4}\s*Skipping:\s*([\w.]+)", log_text))
    all_ids  = step_ids | skip_ids            # skips should be a subset of steps
    total    = len(all_ids)
    skipped  = len(skip_ids)
    return {
        "step_total":   total,
        "step_skipped": skipped,
        "step_failed":  0,                       # set by caller when a step fails
        "step_passed":  max(total - skipped, 0),  # caller subtracts failed steps
    }


def parse_result(log_text: str, exit_code: int = 0,
                 pass_threshold: float = 0.75) -> tuple[str, dict, str]:
    """
    Returns (status, counts_dict, reason_string).
    reason_string is empty for a clean PASS, populated for all other statuses.
    pass_threshold: fraction of steps that must pass for a run with some skipped
    steps to still count as a full PASS (default 0.75 = 75%).
    """
    steps = count_steps(log_text)   # real step-level counts (deduped)

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
        step_counts = dict(steps)
        if status == FAIL and step_counts["step_total"] > 0:
            # The test stopped at a failing step — count it as failed, not passed.
            step_counts["step_failed"] = 1
            step_counts["step_passed"] = max(
                step_counts["step_total"] - step_counts["step_skipped"] - 1, 0)
        return status, step_counts, reason

    # ── Signal 2 — Commissioning / pairing failure ────────────────────────────
    # The authoritative outcome is the mobly summary. Some tests (e.g. TC-CGEN-2.4)
    # DELIBERATELY drive commissioning into failure states ("Failed to commission
    # … UNSUPPORTED_ACCESS") as part of the procedure and still PASS — so only
    # treat these log lines as a real failure when the run did NOT pass cleanly.
    # Without this guard, such tests are mis-flagged ERROR on every attempt (and
    # needlessly commissioning-retried) even though mobly reports Passed, Failed 0.
    passed_cleanly = re.search(
        r"Test results:\s*Error\s+0,\s*Executed\s+[1-9]\d*,\s*"
        r"Failed\s+0,\s*Passed\s+[1-9]\d*",
        log_text, re.IGNORECASE)
    if not passed_cleanly and re.search(
            r"CommissioningError|Failed to commission|"
            r"Commissioning complete failed|"
            r"CHIP_ERROR_CONNECTION_ABORTED|"
            r"Failed to pair with device|"
            r"Unable to find the device",
            log_text, re.IGNORECASE):
        return ERROR, dict(steps), (
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

        # Merge the real, deduped STEP-level counts (from the log) into the
        # result — these drive both the Steps column and the pass tolerance.
        counts.update(steps)
        total_steps   = steps["step_total"]
        skipped_steps = steps["step_skipped"]
        passed_steps  = steps["step_passed"]

        # ── Step-level skips ────────────────────────────────────────────────
        # We do NOT assume skips are caused by missing PICS — a step can skip for
        # a PICS/feature guard, an unmet precondition, or another reason.
        if total_steps > 0 and skipped_steps > 0:
            pass_ratio = passed_steps / total_steps
            pct = round(pass_ratio * 100)
            thr = round(pass_threshold * 100)

            if passed_steps == 0:
                # Nothing actually ran → not a meaningful pass.
                return RERUN, counts, (
                    f"All {total_steps} step(s) skipped — may be PICS/feature-gated "
                    f"(if the test needs it, set pics_folder), an unsupported "
                    f"feature, or another issue. Check the Ctrl Log."
                )
            if pass_ratio >= pass_threshold:
                # Enough steps passed → accept as a full PASS; the remaining
                # skips are tolerated (often DUT-implementation / feature based).
                return PASS, counts, (
                    f"{passed_steps}/{total_steps} steps passed, {skipped_steps} "
                    f"skipped ({pct}% ≥ {thr}% threshold — skips accepted)."
                )
            # Too many skips to be confident → flag as partial.
            return PASS_WARN, counts, (
                f"Partial execution: {passed_steps}/{total_steps} steps passed, "
                f"{skipped_steps} skipped ({pct}% < {thr}% threshold). Skips may be "
                f"PICS/feature-gated (set pics_folder) or an unmet precondition — "
                f"check the Ctrl Log for each 'Skipping' reason."
            )

        # Fallback: no step markers found, but mobly reports test-level skips.
        if total_steps == 0 and counts["skipped"] > 0 and counts["passed"] > 0:
            return PASS_WARN, counts, (
                f"Partial execution: {counts['passed']} test(s) passed, "
                f"{counts['skipped']}/{counts['executed']} skipped — check the Ctrl Log."
            )
        if total_steps == 0 and counts["executed"] > 0 and counts["skipped"] == counts["executed"]:
            return RERUN, counts, (
                f"All {counts['skipped']}/{counts['executed']} test(s) skipped — "
                "check the Ctrl Log for the reason."
            )

        # Clean pass — all steps executed and passed
        return PASS, counts, ""

    # ── Signal 4 — No summary + non-zero exit code ────────────────────────────
    if exit_code != 0:
        # argparse / CLI errors (usually exit 2) — surface the offending argument
        # directly. This is a command-construction problem (bad/duplicate/unknown
        # arg in the python command), not a DUT test failure → ERROR.
        m = re.search(r"^[^\n]*:\s*error:\s*(.+)$", log_text, re.MULTILINE)
        if m and ("unrecognized arguments" in m.group(1)
                  or "argument" in m.group(1) or exit_code == 2):
            return ERROR, {}, (
                f"Bad test command (exit {exit_code}) — argparse: "
                f"{m.group(1).strip()[:180]}. Check the executed python command's "
                f"arguments (see the executed_python_command / top of the Ctrl Log)."
            )
        error_lines = [
            line.strip() for line in log_text.splitlines()
            if re.search(r"\bERROR\b|\bFAIL\b|exception|traceback|error:",
                         line, re.IGNORECASE)
        ]
        hint = error_lines[-1][:140] if error_lines else "Check log for details"
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
# Discriminator override
# =============================================================================
def apply_discriminator(cmd: str, value) -> str:
    """
    Force `cmd` to use our configured discriminator instead of the SDK default
    (3840). If the command already has a --discriminator flag, its value is
    replaced; otherwise the flag is appended. Used for BOTH the DUT launch (so
    the app advertises on our discriminator) and the python test command (so the
    controller commissions to it) — this avoids collisions when several people
    on the same network all use the default 3840.
    """
    if value in (None, ""):
        return cmd
    # Commands that commission via --qr-code / --manual-code carry the
    # discriminator+passcode ENCODED in that payload. Adding --discriminator here
    # would create a discriminator with no matching --passcode ("supplied number
    # of discriminators does not match number of passcodes"). Leave them alone —
    # the pairing code is substituted from the DUT's real log instead.
    if re.search(r"--(?:qr-code|manual-code)\b", cmd):
        return cmd
    value = str(value).strip()
    if re.search(r"--discriminator(?:\s+|=)\S+", cmd):
        return re.sub(r"--discriminator(?:\s+|=)\S+",
                      f"--discriminator {value}", cmd, count=1)
    return f"{cmd.rstrip()} --discriminator {value}"


def is_controller_app(cmd: str) -> bool:
    """True if the DUT command launches a CONTROLLER app rather than a
    commissionable server. e.g. chip-camera-controller (WEBRTCR/WEBRTCP tests,
    run as "interactive server") acts as the TH's controller and launches its own
    peer app via --string-arg th_server_app_path. Controllers do NOT advertise
    for commissioning, so --discriminator must NOT be injected (it's meaningless
    and the app rejects/ignores it)."""
    return bool(re.search(r"\bchip-camera-controller\b|\bcamera-controller\b", cmd))


def ensure_camera_controller_server_args(cmd: str) -> str:
    """chip-camera-controller (WEBRTCR/WEBRTCP) MUST run as 'interactive server' —
    that starts the WebSocket server (ws://localhost:9002) the test drives, and
    the process STAYS alive. 'interactive start' is a readline REPL that hits EOF
    in CI (no TTY) and exits immediately (rc=0) → "DUT process exited immediately".
    Its CI header authoritatively declares `app-args: interactive server`, so
    normalize whatever the Sheet has to that — no Sheet edit needed.
    """
    if not is_controller_app(cmd):
        return cmd
    if re.search(r"\binteractive\s+server\b", cmd):
        return cmd                                   # already correct
    if re.search(r"\binteractive\s+\w+", cmd):       # e.g. 'interactive start'
        return re.sub(r"\binteractive\s+\w+", "interactive server", cmd, count=1)
    if re.search(r"\binteractive\b", cmd):           # bare 'interactive'
        return re.sub(r"\binteractive\b", "interactive server", cmd, count=1)
    return f"{cmd.rstrip()} interactive server"      # no mode at all → append


def set_cmd_flag(cmd: str, flag: str, value: str) -> str:
    """Replace-or-append `flag value` in a command string (e.g. --app-pipe)."""
    pat = re.compile(rf"{re.escape(flag)}(?:\s+|=)\S+")
    if pat.search(cmd):
        return pat.sub(f"{flag} {value}", cmd, count=1)
    return f"{cmd.rstrip()} {flag} {value}"


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
        self.last_straggler_count = 0   # leftover DUTs seen before the last launch
        self.last_full_cmd = ""         # the exact DUT shell command last launched

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


    def launch(self, dut_cmd: str, log_path: Path, append: bool = False) -> tuple[bool, str]:
        """
        Launch DUT app in background.
        Returns (True, "") on success, (False, error_reason) on failure.
        append=True keeps the existing DUT log (used for a mid-test factory-reset
        relaunch, so the pre- and post-reset logs are both preserved).
        """
        global _ACTIVE_DUT
        _ACTIVE_DUT = self
        binary, err = self._find_binary(dut_cmd)
        if not binary:
            print(f"  [DUT] ❌ {err}")
            return False, err

        # Detect + kill any DUT left over from a previous test BEFORE launching a
        # new one. A missed kill leaves a stale app advertising on the same
        # discriminator, so the commissioner may pair with the wrong/dead instance
        # → PASE "Incorrect state" (deterministically breaks AccessChecker/
        # TC-ACE-2.x, which re-commissions from scratch in setup_class). Match by
        # the SDK out/ path so it covers every app type (chip-*, matter-*, *-app,
        # fabric-*, lit-icd, …). The count is surfaced in the run log + summary so
        # kill-races are visible without SSHing into the RPi.
        # NOTE: filter out the pgrep/sh pipeline itself — its own command line
        # contains the '<sdk>/out/' pattern and would otherwise self-match and
        # report a phantom "leftover".
        ps = subprocess.run(
            f"pgrep -af '{self.sdk_dir}/out/' 2>/dev/null | grep -Ev 'pgrep|grep -' || true",
            shell=True, capture_output=True, text=True)
        strays = [ln for ln in ps.stdout.splitlines() if ln.strip()]
        self.last_straggler_count = len(strays)
        if strays:
            print(f"  [DUT] ⚠️  {len(strays)} leftover DUT process(es) still running "
                  f"before launch — killing (indicates a prior kill race):")
            for ln in strays[:5]:
                print(f"          {ln[:110]}")
            subprocess.run(f"pkill -f '{self.sdk_dir}/out/' 2>/dev/null || true",
                           shell=True)
            time.sleep(self.cfg["test_execution"].get("dut_settle_wait", 2))

        # Advertise the DUT on our configured discriminator (not the default 3840).
        # EXCEPT controller apps (e.g. chip-camera-controller for WEBRTCR/WEBRTCP):
        # they aren't commissionable devices and must NOT be launched with a
        # --discriminator.
        disc = self.cfg["test_execution"].get("discriminator", "")
        if is_controller_app(dut_cmd):
            before = dut_cmd
            dut_cmd = ensure_camera_controller_server_args(dut_cmd)
            print("  [DUT] Controller app (chip-camera-controller) — "
                  "not injecting --discriminator (not a commissionable device)")
            if dut_cmd != before:
                print("  [DUT] Normalized to 'interactive server' (WebSocket server "
                      "mode) — required so it stays alive for the test")
        else:
            dut_cmd = apply_discriminator(dut_cmd, disc)
            if disc not in (None, ""):
                print(f"  [DUT] Using discriminator {disc}")

        # Replace ./binary-name with actual full path
        bin_match = re.search(r'\./([^\s]+)', dut_cmd)
        full_cmd  = dut_cmd.replace(bin_match.group(0), str(binary))
        self.last_full_cmd = full_cmd   # exposed for logging the executed command

        # The rm -rf part runs first, then the binary
        # We need to run it as shell command so && works
        print(f"  [DUT] Launching (full): {full_cmd}")

        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_file = open(log_path, 'a' if append else 'w')
        if append:
            self._log_file.write("\n[CI] ===== DUT factory-reset relaunch "
                                 "(fresh KVS, back in commissioning mode) =====\n")
            self._log_file.flush()
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
            # Surface WHY it died — a bare rc=127 is usually a missing runtime
            # shared library on the RPi (e.g. camera app needs ffmpeg/gstreamer
            # runtime libs, which are separate from the build-time -dev packages).
            detail, hint = "", ""
            try:
                tail = log_path.read_text(errors="replace")
                m = re.search(r"error while loading shared libraries:[^\n]+", tail)
                if m:
                    detail = f" — {m.group(0).strip()}"
                    hint = (" The DUT is missing a RUNTIME shared library on the RPi. "
                            "Install the app's runtime libs (camera: `sudo apt-get install "
                            "-y ffmpeg gstreamer1.0-plugins-base gstreamer1.0-plugins-good "
                            "gstreamer1.0-plugins-bad gstreamer1.0-libav libcurl4`).")
            except OSError:
                pass
            if not detail and rc == 127:
                detail = " — rc=127 (missing binary or shared library)"
            print(f"  [DUT] ❌ Process exited immediately (rc={rc}){detail}")
            return False, f"DUT process exited immediately (rc={rc}){detail}.{hint}"

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
            # Brief settle so the killed instance's mDNS records / UDP ports are
            # released before the next test relaunches on the SAME discriminator
            # (avoids the commissioner briefly targeting a stale advertisement).
            time.sleep(self.cfg["test_execution"].get("dut_settle_wait", 2))
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
        # Discriminator to advertise the DUT on + commission to (overrides the
        # SDK default 3840; avoids collisions with others on the same network).
        self.discriminator = cfg["test_execution"].get("discriminator", "")
        if self.discriminator not in (None, ""):
            try:
                if not (0 <= int(self.discriminator) <= 4095):
                    raise ValueError
            except (TypeError, ValueError):
                print(f"[WARN] test_execution.discriminator '{self.discriminator}' "
                      f"is not a valid 12-bit value (0-4095) — ignoring it.")
                self.discriminator = ""
        # A run with some skipped steps still counts as a full PASS if at least
        # this fraction of steps passed (default 75%). Skips are often DUT-
        # implementation / feature dependent and acceptable.
        try:
            self.pass_threshold = float(
                cfg["test_execution"].get("pass_threshold_percent", 75)) / 100.0
        except (TypeError, ValueError):
            self.pass_threshold = 0.75
        self.pass_threshold = min(max(self.pass_threshold, 0.0), 1.0)
        # App-pipe: auto-drive DUT state changes via the SDK named pipe
        # (is_pics_sdk_ci_only) so "operator-required" tests run unattended.
        # NOTE: this SIMULATES DUT state in software — great for regression CI,
        # but NOT equivalent to a physical certification run.
        self.enable_app_pipe = bool(cfg["test_execution"].get("enable_app_pipe", False))
        self._app_pipe_cache: dict[str, bool] = {}   # script name -> uses pipe?
        # Auto-apply each test's SDK CI-header args (the SDK's own declaration):
        #   --enable-key <k>  → DUT (test-event-trigger key; else TestEventTrigger
        #                       is rejected: "Event Triggers are not enabled")
        #   --bool/int/hex/string-arg NAME:VAL → python cmd (e.g. simulate_mounting,
        #                       simulate_occupancy, PIXIT.* keys) if not already set.
        # Self-updating from the SDK; fixes the malformed/missing Sheet values.
        self.apply_ci_test_args = bool(cfg["test_execution"].get("apply_ci_test_args", True))
        self._ci_header_cache: dict[str, str] = {}   # script name -> CI header text
        self._sdk_app_map_cache = None               # ${ENV_KEY} -> binary path

    def _ci_header(self, script_name: str) -> str:
        """The test's '=== BEGIN CI TEST ARGUMENTS ===' block, comment-stripped (cached)."""
        if script_name not in self._ci_header_cache:
            text = ""
            try:
                raw = (self.scripts_dir / script_name).read_text(errors="replace")
                m = re.search(r"BEGIN CI TEST ARGUMENTS(.*?)END CI TEST ARGUMENTS",
                              raw, re.DOTALL)
                if m:
                    text = "\n".join(re.sub(r"^\s*#\s?", "", ln)
                                     for ln in m.group(1).splitlines())
            except OSError:
                pass
            self._ci_header_cache[script_name] = text
        return self._ci_header_cache[script_name]

    def _apply_ci_test_args(self, dut_cmd: str, py_cmd: str) -> tuple[str, str]:
        """Inject the test's declared CI args so operator/CI-sim tests run
        unattended — from the SDK CI header, so values are always correct and
        self-updating (fixes missing --enable-key and simulate_* flags)."""
        if not self.apply_ci_test_args:
            return dut_cmd, py_cmd
        m = re.search(r"\b(TC_\w+\.py)\b", py_cmd)
        if not m:
            return dut_cmd, py_cmd
        hdr = self._ci_header(m.group(1))
        if not hdr:
            return dut_cmd, py_cmd
        # 1) DUT test-event-trigger key
        ek = re.search(r"--enable-key\s+([0-9a-fA-F]{2,})", hdr)
        if ek and "--enable-key" not in dut_cmd:
            dut_cmd = set_cmd_flag(dut_cmd, "--enable-key", ek.group(1))
            print(f"  [DUT] +--enable-key (test event triggers) from CI header")
        # 1b) python per-test timeout. Some tests (e.g. TC-CADMIN window-timing,
        # long failsafe/OTA tests) monitor a full commissioning window and declare
        # a --timeout in their CI header FAR larger than the framework default
        # (90s). Without it the test is cancelled mid-run (asyncio TimeoutError →
        # ERROR). Inject the header's value when the Sheet didn't set one. First
        # match = run1's value (the primary run), which is the right one for the
        # main test in a multi-run header.
        tm = re.search(r"--timeout\s+(\d+)", hdr)
        if tm and not re.search(r"--timeout\b", py_cmd):
            py_cmd = f"{py_cmd.rstrip()} --timeout {tm.group(1)}"
            print(f"  [CI-ARG] +--timeout {tm.group(1)}s (from CI header)")
        # 2) python typed args (bool/int/hex/string/float)-arg NAME:VAL. For each
        #    arg the SDK header declares, with its value resolved from ${...}:
        #      - Sheet has NAME:<placeholder>  → replace the placeholder value
        #        (e.g. jfc_server_app:</root/jfc-app> → real jfc-app path)
        #      - Sheet has NAME:realvalue      → keep the Sheet's value
        #      - Sheet lacks NAME              → inject it
        # Some header args belong to SDK-CI-only replay runs and must NEVER be
        # auto-injected into a live single-test run. test_from_file (e.g.
        # TC_DeviceBasicComposition run9) replays a device dump generated by an
        # earlier CI run — that file doesn't exist here, and it's mutually
        # exclusive with live commissioning (--manual-code/--qr-code). A test
        # with multiple test-runner-runs mixes such args into one header, so we
        # blocklist them unless the Sheet explicitly asked for one.
        NO_AUTOINJECT_ARGS = {"test_from_file"}
        for typ, name, val in re.findall(
                r"--(bool|int|hex|string|float)-arg\s+([\w.]+):(\S+)", hdr):
            if name in NO_AUTOINJECT_ARGS and not re.search(
                    rf"-arg\s+{re.escape(name)}:", py_cmd):
                print(f"  [CI-ARG] skip {name} (SDK-CI replay arg — not for live runs)")
                continue
            val = self._resolve_sdk_placeholders(val)
            if "${" in val:
                print(f"  [CI-ARG] skip {name} (unresolved SDK placeholder {val})")
                continue
            if re.search(rf"{re.escape(name)}:<[^>]*>", py_cmd):
                py_cmd = re.sub(rf"{re.escape(name)}:<[^>]*>",
                                lambda mm: f"{name}:{val}", py_cmd)
                print(f"  [CI-ARG] resolved {name} → {val}")
            elif re.search(rf"-arg\s+{re.escape(name)}:", py_cmd):
                continue
            else:
                py_cmd = f"{py_cmd.rstrip()} --{typ}-arg {name}:{val}"
                print(f"  [CI-ARG] +--{typ}-arg {name}:{val}")

        # Strip any leftover angle-bracket placeholder on a named arg whose value
        # is concrete (e.g. dut_rpc_server_ip:<127.0.0.1> → 127.0.0.1). A bracketed
        # value with spaces is left alone (it's a description, not a value).
        py_cmd = re.sub(r"([\w.]+):<([^<>\s]+)>", r"\1:\2", py_cmd)

        # Joint-Fabric: the JF controller connects to the JF admin app's RPC port,
        # which is HARDCODED in the SDK test. Force dut_rpc_server_port to that real
        # port (the Sheet placeholder is often wrong → "Connection refused") and
        # dut_rpc_server_ip to localhost (both apps run on this RPi).
        if re.search(r"dut_rpc_server_(?:port|ip)\b", py_cmd):
            try:
                src = (self.scripts_dir / m.group(1)).read_text(errors="replace")
            except OSError:
                src = ""
            pm = re.search(r"--rpc-server-port[\"',\s]+(\d+)", src)
            if pm:
                py_cmd = re.sub(r"(dut_rpc_server_port):\S+", rf"\1:{pm.group(1)}", py_cmd)
                print(f"  [CI-ARG] dut_rpc_server_port → {pm.group(1)} (from the test's admin app)")
            py_cmd = re.sub(r"(dut_rpc_server_ip):\S+", r"\1:127.0.0.1", py_cmd)
        return dut_cmd, py_cmd

    def _sdk_app_map(self) -> dict:
        """
        Map SDK ${ENV_KEY} placeholders → the built binary path on the RPi, from
        the SDK's own scripts/tests/local.py env_key→binary table crossed with the
        binaries under out/ (placed by prepare_rpi_tests). Self-updating. Cached.
        e.g. ${JF_ADMIN_APP} → out/jf-admin-app/jfa-app, ${ALL_CLUSTERS_APP} → …
        """
        if self._sdk_app_map_cache is None:
            m = {}
            try:
                text = (self.sdk_dir / "scripts" / "tests" / "local.py").read_text(errors="replace")
                for em in re.finditer(r'env_key="([A-Z0-9_]+)"[^)]*?binary="([^"]+)"',
                                      text, re.DOTALL):
                    key, binname = em.group(1), em.group(2)
                    found = next(iter((self.sdk_dir / "out").glob(f"*/{binname}")), None)
                    if found:
                        m[key] = str(found)
            except OSError:
                pass
            # The push-av server is a script, not a built app target.
            m["PUSH_AV_SERVER"] = str(
                self.sdk_dir / "src" / "tools" / "push_av_server" / "src" / "server.py")
            self._sdk_app_map_cache = m
        return self._sdk_app_map_cache

    def _resolve_sdk_placeholders(self, text: str) -> str:
        """
        Resolve SDK CI-header ${...} placeholders the SDK's own test runner would
        substitute — otherwise they reach the test literally (e.g.
        th_server_app_path:${PUSH_AV_SERVER} → "can't open file '${PUSH_AV_SERVER}'",
        or jfc_server_app:${JF_CONTROL_APP} → path-does-not-exist).
        """
        for key, path in self._sdk_app_map().items():
            text = text.replace("${%s}" % key, path)
        return text

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

        # Resolve any SDK ${...} placeholders (e.g. ${PUSH_AV_SERVER}) that came
        # from the Sheet or a CI-header arg — the SDK runner would substitute them.
        cmd = self._resolve_sdk_placeholders(cmd)

        # Resolve SDK-root-relative path string-args to real paths under the SDK
        # checkout. The SDK CI runs from the SDK root, so its headers use paths like
        # cd_cert_dir:credentials/development/cd-certs; we run from src/python_testing,
        # and the Sheet may even carry a leading-slash absolute (/credentials/...)
        # that resolves to the filesystem root. Either way the dir isn't found
        # (TC_DA_1_2: "FileNotFoundError: /credentials/development/cd-certs").
        for arg in ("cd_cert_dir", "paa_trust_store_path"):
            am = re.search(rf"{arg}:(?:'|\")?([^\s'\"]+)", cmd)
            if am and not Path(am.group(1)).is_dir():
                cand = self.sdk_dir / am.group(1).lstrip("/")
                if cand.is_dir():
                    cmd = re.sub(rf"{arg}:(?:'|\")?[^\s'\"]+(?:'|\")?",
                                 f"{arg}:{cand}", cmd)
                    print(f"  [CI-ARG] {arg} → {cand} (resolved against SDK)")

        # Override the discriminator so the controller commissions to the same
        # value the DUT advertises on (see DUTManager.launch). For self-launching
        # tests (JFDS) this becomes discriminators[0], which they pass to their
        # own apps — so it's still correct. Skipped only for qr/manual-code cmds.
        cmd = apply_discriminator(cmd, self.discriminator)

        # Ensure a --passcode accompanies --discriminator: the framework requires
        # equal counts, and self-launching tests read setup_passcodes[0] (which
        # the Sheet often omits, e.g. JFDS). Uses the standard test passcode.
        cmd = self._ensure_passcode(cmd)

        # Fix 4: Resolve --PICS placeholder with the configured PICS path.
        # --PICS accepts a DIRECTORY of per-cluster PICS XMLs OR a single flat
        # PICS values FILE (e.g. ci-pics-values, which also carries
        # PICS_SDK_CI_ONLY=1 — needed for the app-pipe / is_pics_sdk_ci_only path).
        # So accept either (exists), not just a directory.
        if "__PICS_PLACEHOLDER__" in cmd:
            if self.pics_folder and Path(self.pics_folder).exists():
                cmd = cmd.replace("--PICS __PICS_PLACEHOLDER__",
                                  f"--PICS {self.pics_folder}")
                print(f"  [PICS] Using PICS: {self.pics_folder}")
            else:
                # Remove --PICS entirely if folder not configured or not found
                cmd = cmd.replace("--PICS __PICS_PLACEHOLDER__", "").strip()
                if self.pics_folder:
                    print(f"  [WARN] PICS folder not found: {self.pics_folder} — removing --PICS flag")
                else:
                    print("  [WARN] --PICS in command but pics_folder not set in config — removing --PICS flag")

        # Split like a shell so QUOTED args are de-quoted (subprocess runs without
        # a shell). Naive .split() would keep the literal quotes, e.g.
        # --string-arg "th_server_app_path:..." → argparse "invalid str_named_arg".
        try:
            parts = shlex.split(cmd)
        except ValueError:
            parts = cmd.split()   # unbalanced quotes — best effort
        if parts and parts[0] == "python3":
            parts[0] = str(self.venv_python)
            if len(parts) > 1:
                script_path = self.scripts_dir / parts[1]
                if script_path.exists():
                    parts[1] = str(script_path)
        return parts

    def _uses_app_pipe(self, py_cmd: str) -> bool:
        """
        True if this test drives DUT state via the SDK WRITE named pipe. Detected
        from the SDK test script (cached per script) by EITHER:
          - its CI-arguments header declaring `--app-pipe <path>` (the SDK's own
            authoritative declaration — this also catches tests whose actual
            write_to_app_pipe() call lives in a shared base module, e.g.
            TC_OPSTATE_2_1 → TC_OpstateCommon, OVENOPSTATE, RVCOPSTATE), OR
          - the file directly calling write_to_app_pipe.
        The `(?!-out)` guard excludes `--app-pipe-out` (the READ pipe), which is a
        different mechanism we don't inject here.
        """
        if not self.enable_app_pipe:
            return False
        m = re.search(r"\b(TC_\w+\.py)\b", py_cmd)
        if not m:
            return False
        script = m.group(1)
        if script not in self._app_pipe_cache:
            try:
                text = (self.scripts_dir / script).read_text(errors="replace")
                uses = bool(re.search(r"--app-pipe(?!-out)[ =]", text)) or \
                    ("write_to_app_pipe" in text)
                self._app_pipe_cache[script] = uses
            except OSError:
                self._app_pipe_cache[script] = False
        return self._app_pipe_cache[script]

    def _ensure_pics(self, cmd: str) -> str:
        """
        Ensure --PICS is present so PICS_SDK_CI_ONLY (in ci-pics-values) is active —
        without it, is_pics_sdk_ci_only is False and the test prompts an operator.
        Leaves an existing --PICS (or placeholder) alone; else appends the folder.
        """
        if "--PICS" in cmd:
            return cmd
        if self.pics_folder and Path(self.pics_folder).exists():
            return f"{cmd.rstrip()} --PICS {self.pics_folder}"
        return cmd

    def _ensure_passcode(self, cmd: str) -> str:
        """
        Add the standard test passcode when a --discriminator is present but no
        --passcode is. The framework requires #discriminators == #passcodes, and
        self-launching tests (JFDS) read setup_passcodes[0] to commission their own
        apps. No-op for commands that commission via --qr-code/--manual-code (those
        have no --discriminator, so nothing is added).
        """
        if re.search(r"--discriminator\b", cmd) and not re.search(r"--passcode\b", cmd):
            return f"{cmd.rstrip()} --passcode 20202021"
        return cmd

    def _substitute_pairing_code(self, py_cmd: str, dut_log: Path) -> str:
        """
        Tests that commission INSIDE the test (CGEN, DeviceBasicComposition, …)
        pass --qr-code / --manual-code. The Sheet's value is usually stale AND is
        invalidated once we override the DUT's discriminator — the payload encodes
        discriminator+passcode. So replace it with the DUT's ACTUAL code, which the
        app prints at startup ("SetupQRCode: [MT:…]" / "Manual pairing code: […]").
        """
        want_qr = "--qr-code" in py_cmd
        want_mc = "--manual-code" in py_cmd
        if not (want_qr or want_mc):
            return py_cmd
        qr = mc = None
        # The app prints the code shortly after its Matter stack finishes init,
        # but on a loaded RPi that can lag well past a few seconds. Poll a
        # generous, configurable window (was a fixed 10s — too short under load,
        # which left the <placeholder> in place → "argparse: --manual-code invalid").
        wait_s = self.cfg.get("test_execution", {}).get("pairing_code_wait", 30)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                dlog = dut_log.read_text(errors="replace")
            except OSError:
                dlog = ""
            # DUT log lines are wrapped in ANSI colour codes (e.g. "…]\x1b[0m") —
            # strip them so the payload isn't followed by escape bytes.
            dlog = re.sub(r"\x1b\[[0-9;]*m", "", dlog)
            if want_qr and not qr:
                m = re.search(r"SetupQRCode:\s*\[?(MT:[^\]\s]+)\]?", dlog)
                qr = m.group(1) if m else None
            if want_mc and not mc:
                m = re.search(r"Manual pairing code:\s*\[?([0-9][0-9\- ]*[0-9])\]?", dlog)
                mc = m.group(1) if m else None
            if (not want_qr or qr) and (not want_mc or mc):
                break
            time.sleep(0.5)
        if want_qr:
            if qr:
                py_cmd = re.sub(r"--qr-code\s+\S+", f"--qr-code {qr}", py_cmd, count=1)
                print(f"  [PAIR] Using DUT's actual QR code: {qr}")
            else:
                # Fallback: strip any <…> brackets off the Sheet value so it stays
                # a VALID arg (argparse rejects "<MT:…>"). Commissioning may still
                # fail, but with a clear reason — not a "Bad test command" exit 2.
                py_cmd = re.sub(r"(--qr-code\s+)<?([^<>\s]+)>?", r"\1\2", py_cmd, count=1)
                print(f"  [PAIR] ⚠️  --qr-code test but no SetupQRCode in DUT log after "
                      f"{wait_s}s — using the Sheet value (commissioning may fail).")
        if want_mc:
            if mc:
                code = re.sub(r"\D", "", mc)
                py_cmd = re.sub(r"--manual-code\s+\S+", f"--manual-code {code}", py_cmd, count=1)
                print(f"  [PAIR] Using DUT's actual manual code: {code}")
            else:
                # Same fallback: strip <…> so it's a valid (if possibly stale) code.
                py_cmd = re.sub(r"(--manual-code\s+)<?([^<>\s]+)>?", r"\1\2", py_cmd, count=1)
                print(f"  [PAIR] ⚠️  --manual-code test but no Manual pairing code in "
                      f"DUT log after {wait_s}s — using the Sheet value (commissioning may fail).")
        return py_cmd

    def _restart_dut(self, dut, dut_cmd, dut_log, mode, note):
        """Relaunch the DUT for a restart-flag-file / prompt request. Two modes:

        • 'restart'/'reboot'  → REBOOT: PRESERVE persisted data (fabrics, allocated
          streams). Required by persistence tests (TC-AVSM-2.18/2.19/2.20 step 13
          reboot → step 15 verify the stream survived). We strip the command's
          `rm -rf /tmp/chip_*` so the KVS is kept across the relaunch.
        • 'factory reset*'    → FACTORY RESET: WIPE the KVS (fresh, uncommissioned)
          so re-commissioning tests (TC-DA-1.1 step 4 re-PASE) work. Keeps the
          command's `rm -rf /tmp/chip_*`.
        """
        reboot = mode.lower().startswith(("restart", "reboot"))
        if reboot:
            # Drop the leading `rm -rf <path> &&` so persisted data survives.
            launch_cmd = re.sub(r"^\s*rm\s+-rf\s+\S+\s*&&\s*", "", dut_cmd)
            note(f"[RESET] '{mode}' → REBOOT (preserve persisted data) — relaunching")
        else:
            launch_cmd = dut_cmd     # keeps `rm -rf /tmp/chip_*` → wipes KVS
            note(f"[RESET] '{mode}' → FACTORY RESET (wipe KVS) — relaunching fresh")
        dut.stop()
        ok, err = dut.launch(launch_cmd, dut_log, append=True)
        note("[RESET] DUT back up" if ok else f"[RESET] DUT relaunch FAILED: {err}")

    def _run_python_prompted(self, cmd_parts, log_path: Path, header_lines: list,
                             dut: "DUTManager", dut_cmd: str, dut_log: Path,
                             restart_flag: str = None) -> tuple[int, bool]:
        """Run the python test with a live pipe so it runs unattended:

        1) restart-flag-file (SDK CI mechanism): a test that calls
           request_device_factory_reset()/reboot() WRITES `restart_flag` and polls
           for its removal. We detect the file, factory-reset the DUT, and delete
           it — exactly what the SDK's own test runner does (TC-DA-1.1 step 3).
        2) stdin prompts (wait_for_user_input fallback / other operator prompts):
           a 'factory reset' prompt also triggers a reset; any other prompt is
           auto-confirmed with Enter.

        Returns (returncode, timed_out).
        """
        lf = open(log_path, "w")
        for ln in header_lines:
            lf.write(ln + "\n")
        lf.write("[CI] " + "-" * 70 + "\n\n")
        lf.flush()
        log_lock = threading.Lock()

        proc = subprocess.Popen(
            cmd_parts, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=str(self.scripts_dir), preexec_fn=os.setsid,
            env={**os.environ,
                 "PATH": f"{self.venv_python.parent}:{os.environ.get('PATH','')}"},
        )

        q: "queue.Queue" = queue.Queue()

        def _reader():
            for line in proc.stdout:
                with log_lock:
                    lf.write(line)
                    lf.flush()
                q.put(line)
            q.put(None)                                  # EOF sentinel

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        def _note(msg):
            with log_lock:
                lf.write(f"\n[CI] {msg}\n")
                lf.flush()
            print(f"  {msg}")

        deadline = time.time() + self.timeout
        timed_out = False
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                timed_out = True
                break

            # (1) restart-flag-file: the test wrote it and is polling for removal.
            # Reset the DUT, then delete the flag to unblock the test (which then
            # continues once the freshly-reset DUT is back up).
            if restart_flag and os.path.exists(restart_flag):
                try:
                    mode = Path(restart_flag).read_text(errors="replace").strip()
                except OSError:
                    mode = "factory reset"
                self._restart_dut(dut, dut_cmd, dut_log, mode or "factory reset", _note)
                try:
                    os.remove(restart_flag)
                except OSError:
                    pass
                continue

            try:
                line = q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                break                                    # test process finished
            # (2) stdin prompt line: ">>> <msg> (press enter to confirm)".
            if "press enter to confirm" in line:
                low = line.lower()
                if "factory reset" in low:
                    self._restart_dut(dut, dut_cmd, dut_log, "factory reset", _note)
                elif "reboot" in low or "restart" in low:
                    self._restart_dut(dut, dut_cmd, dut_log, "restart", _note)
                else:
                    _note("[PROMPT] Operator prompt auto-confirmed (Enter)")
                try:
                    proc.stdin.write("\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass

        if timed_out:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            with log_lock:
                lf.write(f"\n\n[CI] TIMEOUT after {self.timeout}s\n")
                lf.flush()
        rc = proc.wait()
        reader.join(timeout=5)
        lf.close()
        return rc, timed_out

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

        # Apply the test's SDK CI-header args (--enable-key to the DUT, simulate_*/
        # PIXIT typed args + resolved app paths to the python cmd) so operator/
        # event-trigger/joint-fabric tests run unattended with correct values.
        dut_cmd, py_cmd = self._apply_ci_test_args(dut_cmd, py_cmd)

        # Ensure --PICS is present for EVERY test (not just app-pipe ones) so the
        # configured PICS source — which carries PICS_SDK_CI_ONLY — is active and
        # is_pics_sdk_ci_only is True. Without it, CI-simulated tests (JFDS,
        # SMOKECO, …) fall into their real-DUT branch (external app +
        # dut_rpc_server_port + setup_passcodes) and fail. Leaves an existing
        # --PICS or --PICS placeholder in the Sheet command untouched.
        py_cmd = self._ensure_pics(py_cmd)

        # Self-orchestrating tests (e.g. Joint Fabric JFDS/JFADMIN) launch their
        # OWN helper apps and pass their paths via --string-arg; the DUT command
        # has no `./app` for us to launch (and launching one would collide). Detect
        # that and skip our DUT launch + discriminator override.
        has_dut_app = bool(re.search(r"\./\S+", dut_cmd))
        safe = re.sub(r"[^A-Za-z0-9_]", "_", tc_id)

        # App-pipe: for tests that drive DUT state via write_to_app_pipe, inject a
        # MATCHING --app-pipe into BOTH the DUT app and the python command (SDK CI
        # pattern), and ensure --PICS so PICS_SDK_CI_ONLY makes the test take the
        # pipe path instead of prompting an operator. Pipe lives under /tmp/chip_*
        # so the DUT's own `rm -rf /tmp/chip_*` + our cleanup wipe it.
        app_pipe = None
        if has_dut_app and self._uses_app_pipe(py_cmd):
            app_pipe = f"/tmp/chip_apppipe_{safe}"
            dut_cmd  = set_cmd_flag(dut_cmd, "--app-pipe", app_pipe)
            py_cmd   = set_cmd_flag(py_cmd, "--app-pipe", app_pipe)
            # (--PICS already ensured above for every test)
            print(f"  [PIPE] app-pipe driven (CI-simulated DUT state) → {app_pipe}")

        # Factory-reset / reboot orchestration — the SDK's CI mechanism. Passing
        # --restart-flag-file makes request_device_factory_reset()/reboot() (e.g.
        # TC-DA-1.1 step 3) WRITE a flag file and poll for its removal, instead of
        # prompting on stdin. Our interactive runner monitors the flag, factory-
        # resets the DUT (stop → wipe KVS → relaunch fresh in commissioning mode),
        # then deletes it to unblock the test. The path is OUTSIDE /tmp/chip_* so
        # the DUT's own `rm -rf /tmp/chip_*` can't delete it early (which would
        # unblock the test before the freshly-reset DUT is back up). Harmless when
        # a test never resets — the flag is simply never created.
        restart_flag = None
        if has_dut_app:
            restart_flag = f"/tmp/matterci_restart_{safe}"
            if "--restart-flag-file" not in py_cmd:
                py_cmd = f"{py_cmd.rstrip()} --restart-flag-file {restart_flag}"
            subprocess.run(f"rm -f '{restart_flag}' 2>/dev/null || true", shell=True)

        self._clean_storage()
        start = time.time()

        if not has_dut_app:
            # No DUT to launch — the test manages its own apps. Just clear stale
            # chip state so those apps start fresh.
            print("  [DUT] No DUT app in command — self-orchestrating test; "
                  "skipping DUT launch (the test launches its own apps).")
            subprocess.run("rm -rf /tmp/chip_* 2>/dev/null || true", shell=True)
        else:
            launched, launch_err = dut.launch(dut_cmd, dut_log)
            if not launched:
                elapsed = round(time.time() - start, 2)
                c = {"stragglers_before": dut.last_straggler_count} if dut.last_straggler_count else {}
                return ERROR, c, launch_err, elapsed

        # The DUT app creates the FIFO only AFTER its Matter stack finishes init,
        # which can exceed the fixed startup wait on a slow RPi / heavy app. The
        # python runner REQUIRES --app-pipe to exist the moment it parses args, so
        # actively wait for the FIFO instead of racing it (that intermittent race
        # is why some pipe tests passed and others hit "pipe does NOT exist").
        if app_pipe:
            wait_s = self.cfg["test_execution"].get("app_pipe_wait", 25)
            deadline = time.time() + wait_s
            while not os.path.exists(app_pipe) and time.time() < deadline:
                if dut._proc is None or dut._proc.poll() is not None:
                    break   # DUT died — stop waiting
                time.sleep(0.5)
            if not os.path.exists(app_pipe):
                elapsed = round(time.time() - start, 2)
                reason = (f"App-pipe {app_pipe} was not created by the DUT within "
                          f"{wait_s}s — the launched app may not support --app-pipe "
                          f"or crashed at startup (wrong DUT app for this test?). "
                          f"The python runner requires the pipe to exist at launch.")
                print(f"  [PIPE] ❌ {reason}")
                dut.stop()
                self._clean_storage()
                return ERROR, {"app_pipe": True}, reason, elapsed
            print(f"  [PIPE] {app_pipe} ready "
                  f"({round(time.time() - start, 1)}s after launch).")

        # For in-test commissioning (--qr-code / --manual-code), swap in the DUT's
        # ACTUAL pairing payload from its startup log (the Sheet value is stale /
        # invalidated by our discriminator override).
        if has_dut_app:
            py_cmd = self._substitute_pairing_code(py_cmd, dut_log)

        cmd_parts = self._build_python_cmd(py_cmd)
        # The FINAL commands actually executed (after discriminator / app-pipe /
        # PICS / --enable-key / CI-arg injection) — logged in full and saved to
        # the result, since these differ from the raw Sheet commands.
        executed_dut = dut.last_full_cmd if has_dut_app else f"(no DUT app) {dut_cmd}"
        executed_py  = " ".join(str(p) for p in cmd_parts)
        print(f"  [TEST] Running (full): {executed_py}")

        # Header lines recorded at the TOP of the Ctrl Log so the exact executed
        # commands are visible when you open it from the report.
        header = [f"[CI] Executed DUT command    : {executed_dut}",
                  f"[CI] Executed Python command : {executed_py}"]
        try:
            # DUT tests run through the interactive runner: it tees output and
            # enforces the timeout exactly like subprocess.run, but ALSO answers
            # operator prompts (wait_for_user_input) at RUNTIME and factory-resets
            # the DUT when a test asks (e.g. TC-DA-1.1 step 3). We detect prompts
            # from the "(press enter to confirm)" marker rather than scanning the
            # source (unreliable — some tests call the prompt via a shared helper).
            # Safe as the default: no python_testing test reads stdin except via
            # wait_for_user_input, which always prints that marker first. No-DUT
            # self-orchestrating tests keep the plain path (nothing to reset).
            if has_dut_app:
                rc, timed_out = self._run_python_prompted(
                    cmd_parts, log_path, header, dut, dut_cmd, dut_log, restart_flag)
                if timed_out:
                    status, counts, reason = ERROR, {}, f"Test timed out after {self.timeout}s"
                else:
                    log_text = log_path.read_text(errors="replace")
                    status, counts, reason = parse_result(
                        log_text, exit_code=rc, pass_threshold=self.pass_threshold)
            else:
                with open(log_path, "w") as lf:
                    for ln in header:
                        lf.write(ln + "\n")
                    lf.write("[CI] " + "-" * 70 + "\n\n")
                    lf.flush()
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
                status, counts, reason = parse_result(
                    log_text, exit_code=proc.returncode,
                    pass_threshold=self.pass_threshold)

        except subprocess.TimeoutExpired:
            status, counts, reason = ERROR, {}, f"Test timed out after {self.timeout}s"
            with open(log_path, "a") as lf:
                lf.write(f"\n\n[CI] TIMEOUT after {self.timeout}s\n")

        except Exception as exc:
            status, counts, reason = ERROR, {}, f"Runner exception: {exc}"

        finally:
            dut.stop()
            self._clean_storage()
            if restart_flag:
                subprocess.run(f"rm -f '{restart_flag}' 2>/dev/null || true", shell=True)

        # Record whether leftover DUT(s) had to be killed before this attempt —
        # surfaced in the report/summary so kill-races are visible. (Only when we
        # actually launched a DUT — otherwise the count is stale from a prior test.)
        if has_dut_app and dut.last_straggler_count:
            counts = dict(counts or {})
            counts["stragglers_before"] = dut.last_straggler_count

        # Mark pipe-driven runs so results are clearly CI-simulated (not physical).
        if app_pipe:
            counts = dict(counts or {})
            counts["app_pipe"] = True

        # Carry the FINAL executed commands into the result (promoted to top-level
        # keys by _result) so they're visible in test_results.json / the report.
        counts = dict(counts or {})
        counts["executed_dut_command"]    = executed_dut
        counts["executed_python_command"] = executed_py

        # If the DUT CRASHED mid-test, the controller only sees a Timeout (0x32).
        # Surface the real cause from the DUT log so it's actionable — most often
        # the wrong DUT app for this test's app-pipe commands (e.g. all-clusters
        # VerifyOrDie/core-dumps on RVC's "Reset" — RVC tests need chip-rvc-app).
        if status in (ERROR, FAIL) and has_dut_app:
            try:
                dlog = dut_log.read_text(errors="replace") if dut_log.exists() else ""
            except OSError:
                dlog = ""
            crash = None
            if ("Named pipe command not supported" in dlog
                    or "VerifyOrDie failure" in dlog):
                m = re.search(r"Unhandled command '([^']+)'", dlog)
                cmd = m.group(1) if m else "?"
                crash = (f"DUT CRASHED on unsupported app-pipe command '{cmd}' "
                         f"(app aborted/core-dumped) — the launched DUT app does not "
                         f"implement this test's pipe commands. Use the app the test "
                         f"expects (RVC tests need chip-rvc-app, not all-clusters).")
            elif re.search(r"core dumped|Aborted|Segmentation fault|terminate called",
                           dlog):
                crash = "DUT crashed (core dump/abort) mid-test — see the DUT log."
            if crash:
                status = ERROR
                reason = crash + (f" | {reason}" if reason else "")

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
            # Show only the numeric counts on the progress line — the full
            # executed commands are already printed above (the "(full)" lines)
            # and saved to the result JSON, so don't repeat them here.
            shown = {k: v for k, v in (counts or {}).items()
                     if k not in ("executed_dut_command", "executed_python_command")}
            print(f"  [{status}] {tc_id} — {elapsed}s  {shown}{reason_short}")

            # Determine if we should retry. Besides outright commissioning
            # failures, retry transient SESSION/PASE errors during setup — these
            # come from the DUT not being ready / stale mDNS on a rapid restart
            # (the test passes on a fresh relaunch, which _run_attempt does). A
            # manual re-run works for exactly this reason.
            SESSION_RETRY_MARKERS = (
                "Commissioning", "Not connected", "Incorrect state",
                "secure session", "Secure Pairing", "PASE",
                "setup_class", "ChipStackError",
                "0x00000048", "0x00000003",
            )
            is_commissioning_error = (
                status == ERROR and reason and
                any(m in reason for m in SESSION_RETRY_MARKERS)
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
        if (counts or {}).get("app_pipe"):
            retry_note = "[CI-simulated via app-pipe] "
        if commissioning_attempts > 0:
            retry_note += f"(session/commissioning retried {commissioning_attempts}x) "
        if step_retry_done:
            retry_note += "(step failure retried 1x) "
        if retry_note:
            reason = retry_note.strip() + (" | " + reason if reason else "")

        # Use the last log file as final log
        if attempt > 1:
            final_log = log_path.parent / f"{log_path.stem}_attempt{attempt}.log"
            if not final_log.exists():
                final_log = log_path

        return self._result(tc, status, counts, elapsed, final_log, note=reason)

    def _result(self, tc, status, counts, elapsed, log_path, note=""):
        # Move the executed commands out of counts to top-level result keys, so
        # counts stays purely numeric (Steps column) and the JSON clearly shows
        # both the raw Sheet command and the FINAL executed command.
        counts = dict(counts or {})
        exec_dut = counts.pop("executed_dut_command", tc["dut_command"])
        exec_py  = counts.pop("executed_python_command", tc["python_command"])
        return {
            "test_case_id":            tc["test_case_id"],
            "cluster":                 tc.get("cluster", ""),
            "dut_command":             tc["dut_command"],          # raw (from Sheet)
            "python_command":          tc["python_command"],       # raw (from Sheet)
            "executed_dut_command":    exec_dut,                   # actually launched
            "executed_python_command": exec_py,                    # actually run
            "status":                  status,
            "counts":                  counts,
            "elapsed_s":               elapsed,
            "log_file":                str(log_path),
            "note":                    note,
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


def generate_report(results: list[dict], cfg: dict = None,
                    report_path=None, build_info=None) -> Path:
    # report_path / build_info can be passed explicitly to regenerate a report
    # from an EXISTING run's test_results.json (see regenerate_report.py) without
    # a live run. Falls back to the config / logs/build-info.json for a live run.
    if report_path is None:
        report_path = PROJECT_ROOT / cfg["test_execution"]["report_path"]
    report_path = Path(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    bi          = build_info if build_info is not None else read_build_info()
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

    # ---- Status → pill CSS class ----
    PILL_CLASS = {
        PASS: "pill-pass", PASS_WARN: "pill-passw", FAIL: "pill-fail",
        RERUN: "pill-rerun", ERROR: "pill-error", CANCEL: "pill-cancel",
    }

    def badge(status):
        return (f'<span class="pill {PILL_CLASS.get(status, "pill-error")}">'
                f'<span class="dot"></span>{status}</span>')

    # ---- Step-count SVG icons ----
    CHECK = ('<svg class="ic" viewBox="0 0 24 24" width="13" height="13" fill="none" '
             'stroke="#059669" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">'
             '<polyline points="20 6 9 17 4 12"/></svg>')
    XMARK = ('<svg class="ic" viewBox="0 0 24 24" width="12" height="12" fill="none" '
             'stroke="#DC2626" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">'
             '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>')

    def status_reason(r):
        """Reason string for the report. FAIL/RERUN/ERROR use parse_result's note."""
        status = r["status"]
        note   = r.get("note", "")
        if status == PASS:
            return note
        if status == PASS_WARN:
            return note
        if status == CANCEL:
            return "Cancelled by user before this test started"
        if note:
            return note
        counts = r.get("counts", {})
        if status == FAIL:
            f = counts.get("failed", 0); e = counts.get("error", 0)
            parts = []
            if f: parts.append(f"{f} step(s) failed")
            if e: parts.append(f"{e} error(s)")
            return ", ".join(parts) if parts else "Test failed"
        if status == RERUN:
            s = counts.get("skipped", 0); ex = counts.get("executed", 0)
            return f"All {s}/{ex} steps skipped — re-run to investigate"
        if status == ERROR:
            return "Test script did not produce a result — may have crashed or timed out"
        return ""

    def steps_cell(counts, status):
        st_total = counts.get("step_total", 0)
        if st_total > 0:
            st_skip = counts.get("step_skipped", 0)
            st_fail = counts.get("step_failed", 1 if status == FAIL else 0)
            st_pass = counts.get("step_passed", max(st_total - st_skip - st_fail, 0))
            total_v = st_total
        elif "executed" in counts:
            st_pass = counts.get("passed", 0); st_fail = counts.get("failed", 0)
            st_skip = counts.get("skipped", 0); total_v = counts.get("executed", 0)
        else:
            return ""
        skip_html = (f'<span class="sg skip">⏭{st_skip}</span>' if st_skip else "")
        return (f'<span class="steps mono"><span class="sg">{CHECK}{st_pass}</span>'
                f'<span class="sg">{XMARK}{st_fail}</span>{skip_html}'
                f'<span class="sg sig">Σ{total_v}</span></span>')

    # ---- Build table rows ----
    rows_html = ""
    for r in results:
        tc_id   = r["test_case_id"]
        cluster = extract_cluster(tc_id, r.get("cluster", ""))
        status  = r["status"]
        counts  = r.get("counts", {})
        elapsed = r["elapsed_s"]
        log_file = Path(r.get("log_file", ""))
        dut_log  = log_file.parent / f"{tc_id}_dut.log" if log_file.name else None

        if log_file.name:
            tcid_html = (f'<a class="tcid-link mono" href="test_runs/{log_file.name}" '
                         f'target="_blank">{tc_id}</a>')
        else:
            tcid_html = f'<span class="tcid-link mono">{tc_id}</span>'

        log_links = ""
        if log_file.name:
            log_links += (f'<a href="test_runs/{log_file.name}" target="_blank" '
                          f'class="log-link ctrl-log">Ctrl Log</a>')
        if dut_log and dut_log.name:
            log_links += (f'<a href="test_runs/{dut_log.name}" target="_blank" '
                          f'class="log-link dut-log">DUT Log</a>')

        reason = status_reason(r)
        reason_cell = f'<span class="reason">{reason}</span>' if reason else ""

        rows_html += f"""
        <tr class="tc-row row-{status.lower()}" data-cluster="{cluster}" data-status="{status}" data-time="{elapsed}" data-tcid="{tc_id}">
          <td>{tcid_html}<div class="cluster-sub">{cluster}</div></td>
          <td>{badge(status)}</td>
          <td>{steps_cell(counts, status)}</td>
          <td class="mono time">{elapsed}s</td>
          <td class="log-cell">{log_links}</td>
          <td class="reason-cell">{reason_cell}</td>
        </tr>"""

    # ---- Stat tiles (single row of 7) ----
    _TILES = [
        ("t-total", "Total", total, "ALL",
         "Total test cases in this run. Click to clear the status filter."),
        ("t-pass", "Passed", passed, "PASS",
         "All steps executed and passed — clean result."),
        ("t-passw", "Pass*", pass_warn, "PASS*",
         "Partial — some steps skipped (PICS/feature-gated or an unmet precondition) "
         "but enough passed to accept. See the Ctrl Log for each skip reason."),
        ("t-fail", "Failed", failed, "FAIL",
         "One or more test steps failed. Check the Reason column and the Ctrl Log."),
        ("t-rerun", "Rerun", rerun, "RERUN",
         "All steps skipped — PICS/feature-gated, unsupported feature, or another "
         "issue. Check the Ctrl Log for the skip reasons."),
        ("t-err", "Error", errors, "ERROR",
         "Script crashed, timed out, or commissioning failed before steps ran. "
         "Check the Ctrl Log for the traceback."),
        ("t-cancel", "Cancelled", cancelled, "CANCEL",
         "Run was cancelled (SIGTERM / GitHub Actions cancel). These TCs did not execute."),
    ]
    stat_tiles = "".join(
        f'<div class="tile {cls}" data-filter="{filt}" onclick="setStatus(\'{filt}\')">'
        f'<div class="num mono">{val}</div><div class="lbl">{lbl}</div>'
        f'<div class="tip">{tip}</div></div>'
        for cls, lbl, val, filt, tip in _TILES
    )

    # ---- Cluster multi-select checkboxes ----
    cluster_checkboxes = "".join(
        f'<label class="ms-item"><input type="checkbox" class="cluster-cb" value="{c}" '
        f'checked onchange="onClusterChange()"><span>{c}</span></label>'
        for c in clusters
    )

    # ---- Footer status ----
    if failed == 0 and errors == 0 and rerun == 0 and cancelled == 0:
        footer_status = "All tests passed"; foot_dot = "#22c55e"
    elif failed == 0 and errors == 0:
        footer_status = f"{passed + pass_warn}/{total} passed"; foot_dot = "#f59e0b"
    else:
        footer_status = f"{failed} failed · {errors} error(s)"; foot_dot = "#ef4444"
    built_txt = bi_date if bi_date else "—"

    _TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Matter CI — Test Report</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
      background: #F0F2F7; color: #1f2937; -webkit-font-smoothing: antialiased;
    }
    .mono { font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace; }

    /* ---- Header ---- */
    .header {
      background: #0E1120; color: #fff; padding: 20px 32px;
      display: flex; justify-content: space-between; align-items: center; gap: 20px; flex-wrap: wrap;
    }
    .header-title { display: flex; align-items: center; gap: 12px; font-size: 20px; font-weight: 700; letter-spacing: -.2px; }
    .header-meta { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12px; color: #9ba3b4; margin-top: 9px; }
    .chip { font-family: 'JetBrains Mono', ui-monospace, monospace; background: #1b2030; border: 1px solid #2a3143; color: #c7cfdd; padding: 2px 9px; border-radius: 6px; font-size: 12px; }
    .branch { background: rgba(34,197,94,.14); color: #4ade80; border: 1px solid rgba(74,222,128,.32); padding: 2px 11px; border-radius: 20px; font-size: 12px; font-weight: 600; }
    .header-meta .built { color: #6b7280; }
    .header-right { text-align: right; }
    .header-right .lbl { font-size: 10px; text-transform: uppercase; letter-spacing: 1.2px; color: #6b7280; }
    .header-right .val { font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 13px; color: #c7cfdd; margin-top: 3px; }

    /* ---- Stat tiles (always one row of 7) ---- */
    .tiles { display: grid; grid-template-columns: repeat(7, 1fr); gap: 12px; padding: 20px 32px; }
    .tile {
      position: relative; border-radius: 12px; padding: 15px 18px; cursor: pointer; border: 1px solid rgba(255,255,255,.05);
      transition: transform .12s ease, box-shadow .12s ease; user-select: none;
    }
    .tile:hover { transform: translateY(-2px); box-shadow: 0 8px 20px rgba(16,24,40,.16); }
    .tile.active { outline: 2px solid rgba(255,255,255,.55); outline-offset: 1px; }
    .tile .num { font-family: 'JetBrains Mono', ui-monospace, monospace; font-size: 32px; font-weight: 700; line-height: 1; }
    .tile .lbl { font-size: 10px; text-transform: uppercase; letter-spacing: 1.1px; margin-top: 8px; color: rgba(255,255,255,.55); font-weight: 600; }
    .tile .tip {
      position: absolute; top: calc(100% + 8px); left: 12px; right: 12px; z-index: 60;
      background: #0E1120; color: #e5e7eb; border: 1px solid #2a3143; border-radius: 8px;
      padding: 9px 11px; font-size: 11px; line-height: 1.5; font-weight: 400; text-transform: none;
      letter-spacing: 0; box-shadow: 0 10px 24px rgba(16,24,40,.28);
      opacity: 0; pointer-events: none; transform: translateY(-3px); transition: opacity .16s, transform .16s;
    }
    .tile .tip::after { content: ""; position: absolute; bottom: 100%; left: 24px; border: 6px solid transparent; border-bottom-color: #0E1120; }
    .tile:hover .tip { opacity: 1; transform: translateY(0); }
    .t-total  { background: #1B2131; } .t-total .num  { color: #CBD5E1; }
    .t-pass   { background: #0F2A1D; } .t-pass .num   { color: #34D399; }
    .t-passw  { background: #12291F; } .t-passw .num  { color: #6EE7B7; }
    .t-fail   { background: #2B1517; } .t-fail .num   { color: #F87171; }
    .t-rerun  { background: #2B1E0F; } .t-rerun .num  { color: #FBBF24; }
    .t-err    { background: #1B222E; } .t-err .num    { color: #94A3B8; }
    .t-cancel { background: #241531; } .t-cancel .num { color: #C084FC; }

    /* ---- Filters ---- */
    .filters {
      background: #fff; border: .5px solid #DDE0EC; border-radius: 12px; margin: 0 32px 20px;
      padding: 12px 16px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    }
    .filters .flabel { font-size: 12px; font-weight: 600; color: #6b7280; }
    .filters select {
      padding: 8px 12px; border: 1px solid #DDE0EC; border-radius: 8px; font-size: 13px;
      background: #fff; color: #374151; outline: none; cursor: pointer;
    }
    .filters select:focus { border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,.12); }
    .search { flex: 1; max-width: 280px; position: relative; }
    .search input { width: 100%; padding: 8px 30px 8px 12px; border: 1px solid #DDE0EC; border-radius: 8px; font-size: 13px; outline: none; }
    .search input:focus { border-color: #4f46e5; box-shadow: 0 0 0 3px rgba(79,70,229,.12); }
    .search .clr { position: absolute; right: 9px; top: 50%; transform: translateY(-50%); cursor: pointer; color: #9ca3af; font-size: 14px; display: none; }
    .search .clr.show { display: block; }
    .btn-clear { padding: 8px 14px; border: 1px solid #DDE0EC; border-radius: 8px; background: #fff; color: #374151; font-size: 13px; font-weight: 600; cursor: pointer; transition: all .15s; }
    .btn-clear:hover { background: #EEF2FF; border-color: #4f46e5; color: #4f46e5; }
    .count { margin-left: auto; font-size: 12px; color: #6b7280; font-weight: 600; }

    /* cluster multi-select */
    .ms { position: relative; }
    .ms-toggle {
      padding: 8px 12px; border: 1px solid #DDE0EC; border-radius: 8px; font-size: 13px;
      background: #fff; color: #374151; cursor: pointer; min-width: 300px; text-align: left;
      display: flex; justify-content: space-between; align-items: center; gap: 8px;
    }
    .ms-toggle:hover { border-color: #c7cdda; }
    .ms-toggle .car { color: #9ca3af; font-size: 10px; }
    .ms-panel {
      position: absolute; top: calc(100% + 5px); left: 0; z-index: 50; background: #fff;
      border: 1px solid #DDE0EC; border-radius: 10px; box-shadow: 0 10px 24px rgba(16,24,40,.14);
      padding: 8px; min-width: 300px; max-height: 320px; overflow: auto;
    }
    .ms-head { display: flex; justify-content: space-between; padding: 4px 8px 8px; border-bottom: 1px solid #eef0f6; margin-bottom: 6px; }
    .ms-head button { background: none; border: none; color: #4f46e5; font-size: 12px; font-weight: 600; cursor: pointer; }
    .ms-item, .ms-all { display: flex; align-items: center; gap: 8px; padding: 6px 8px; border-radius: 6px; font-size: 13px; cursor: pointer; }
    .ms-item:hover { background: #f4f5fb; }
    .ms-item input { accent-color: #4f46e5; width: 15px; height: 15px; }

    /* ---- Table ---- */
    .table-card { background: #fff; border: .5px solid #DDE0EC; border-radius: 12px; margin: 0 32px 20px; overflow: hidden; }
    table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    thead th {
      background: #0E1120; color: #8b93a7; text-transform: uppercase; font-size: 10px; letter-spacing: 1.1px;
      font-weight: 600; padding: 12px 16px; text-align: left; white-space: nowrap;
    }
    th.sortable { cursor: pointer; user-select: none; }
    th.sortable:hover { color: #cbd5e1; }
    th .arrow { opacity: .5; margin-left: 4px; }
    td { padding: 12px 16px; border-bottom: .5px solid #EEF0F6; font-size: 13px; vertical-align: top; overflow-wrap: anywhere; word-break: break-word; }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr.tc-row:hover { background: #F7F8FC; }
    tr.tc-row { transition: background .12s; }

    .tcid-link { color: #4f46e5; font-weight: 600; text-decoration: none; font-size: 13px; }
    .tcid-link:hover { text-decoration: underline; }
    .cluster-sub { color: #9ca3af; font-size: 11px; margin-top: 4px; }
    .time { color: #4b5563; }

    .pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 11px; border-radius: 20px; font-size: 12px; font-weight: 600; border: 1px solid; white-space: nowrap; }
    .pill .dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
    .pill-pass   { background: #ECFDF5; color: #065F46; border-color: #A7F3D0; } .pill-pass .dot   { background: #10B981; }
    .pill-passw  { background: #F0FDF4; color: #166534; border-color: #BBF7D0; } .pill-passw .dot  { background: #22C55E; }
    .pill-fail   { background: #FEF2F2; color: #991B1B; border-color: #FECACA; } .pill-fail .dot   { background: #EF4444; }
    .pill-rerun  { background: #FFF7ED; color: #C2410C; border-color: #FED7AA; } .pill-rerun .dot  { background: #F97316; }
    .pill-error  { background: #F8FAFC; color: #334155; border-color: #E2E8F0; } .pill-error .dot  { background: #64748B; }
    .pill-cancel { background: #FAF5FF; color: #6B21A8; border-color: #E9D5FF; } .pill-cancel .dot { background: #A855F7; }

    .steps { display: inline-flex; align-items: center; gap: 11px; font-size: 12.5px; color: #374151; }
    .sg { display: inline-flex; align-items: center; gap: 3px; }
    .sg.sig { color: #6b7280; } .sg.skip { color: #9ca3af; }
    .ic { flex-shrink: 0; }

    .log-link { display: inline-flex; align-items: center; padding: 4px 11px; border-radius: 7px; font-size: 12px; font-weight: 600; text-decoration: none; margin-right: 6px; border: 1px solid; transition: filter .12s; }
    .ctrl-log { background: #EFF6FF; color: #1D4ED8; border-color: #BFDBFE; }
    .dut-log  { background: #FFF7ED; color: #C2410C; border-color: #FED7AA; }
    .log-link:hover { filter: brightness(.96); }

    .reason { font-size: 12.5px; color: #4b5563; line-height: 1.45; }
    tr.row-fail .reason { color: #991B1B; }
    tr.row-rerun .reason { color: #C2410C; }
    tr.row-error .reason { color: #475569; }

    .hidden { display: none; }
    .no-results { text-align: center; padding: 44px; color: #9ca3af; font-size: 14px; }

    /* ---- Footer ---- */
    .footer {
      background: #fff; border-top: .5px solid #DDE0EC; padding: 16px 32px;
      display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap;
      font-size: 12px; color: #6b7280;
    }
    .footer .foot-left { display: inline-flex; align-items: center; gap: 8px; }
    .footer .foot-dot { width: 8px; height: 8px; border-radius: 50%; background: __FOOT_DOT__; display: inline-block; }

    @media (max-width: 900px) {
      .tiles { grid-template-columns: repeat(7, minmax(90px, 1fr)); overflow-x: auto; }
    }
  </style>
</head>
<body>
  <header class="header">
    <div class="header-left">
      <div class="header-title">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#34d17f" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 3h6"/><path d="M10 3v6l-5.2 9.4A1.5 1.5 0 0 0 6.1 21h11.8a1.5 1.5 0 0 0 1.3-2.6L14 9V3"/><path d="M7.5 14h9"/>
        </svg>
        Matter CI — Test Report
      </div>
      <div class="header-meta">
        <span class="chip">__COMMIT__</span>
        <span class="branch">__BRANCH__</span>
        <span class="built">built __BUILT__</span>
      </div>
    </div>
    <div class="header-right">
      <div class="lbl">Generated</div>
      <div class="val">__RUN_TIME__</div>
    </div>
  </header>

  <section class="tiles">
    __TILES__
  </section>

  <section class="filters">
    <span class="flabel">Cluster:</span>
    <div class="ms" id="clusterMs">
      <button type="button" class="ms-toggle" id="msToggle" onclick="toggleMs(event)">
        <span id="msLabel">All clusters</span><span class="car">▼</span>
      </button>
      <div class="ms-panel hidden" id="msPanel">
        <div class="ms-head">
          <button type="button" onclick="setAllClusters(true)">Select all</button>
          <button type="button" onclick="setAllClusters(false)">Clear</button>
        </div>
        __CLUSTER_CB__
      </div>
    </div>

    <span class="flabel">Status:</span>
    <select id="statusFilter" onchange="applyFilters()">
      <option value="ALL">All statuses</option>
      <option value="PASS">PASS</option>
      <option value="PASS*">PASS* (partial)</option>
      <option value="FAIL">FAIL</option>
      <option value="RERUN">RERUN</option>
      <option value="ERROR">ERROR</option>
      <option value="CANCEL">CANCELLED</option>
    </select>

    <div class="search">
      <input type="text" id="searchFilter" placeholder="Search TC ID / text…" oninput="applyFilters()">
      <span class="clr" id="searchClr" onclick="clearSearch()">✕</span>
    </div>

    <button class="btn-clear" onclick="clearFilters()">↺ Clear filters</button>
    <span class="count" id="result-count"></span>
  </section>

  <section class="table-card">
    <table id="resultsTable">
      <colgroup>
        <col style="width:18%"><col style="width:10%"><col style="width:14%">
        <col style="width:8%"><col style="width:15%"><col style="width:35%">
      </colgroup>
      <thead>
        <tr>
          <th class="sortable" onclick="sortTable(0,'tcid')">TC ID <span class="arrow">⇅</span></th>
          <th class="sortable" onclick="sortTable(1,'status')">Status <span class="arrow">⇅</span></th>
          <th>Steps</th>
          <th class="sortable" onclick="sortTable(3,'num')">Time <span class="arrow">⇅</span></th>
          <th>Logs</th>
          <th>Reason / Notes</th>
        </tr>
      </thead>
      <tbody id="tableBody">
        __ROWS__
      </tbody>
    </table>
    <div class="no-results hidden" id="noResults">No test cases match the current filters.</div>
  </section>

  <footer class="footer">
    <span class="foot-left"><span class="foot-dot"></span>Matter CI · Test execution report · __FOOTER_STATUS__</span>
    <span class="mono">__COMMIT__</span>
  </footer>

  <script>
    var TOTAL = __TOTAL__;
    var sortState = { col: -1, dir: 'asc' };

    function allClusterBoxes() { return Array.from(document.querySelectorAll('.cluster-cb')); }
    function checkedClusters() { return allClusterBoxes().filter(c => c.checked).map(c => c.value); }
    function updateClusterLabel() {
      var boxes = allClusterBoxes(), sel = checkedClusters(), el = document.getElementById('msLabel');
      if (sel.length === boxes.length) el.textContent = 'All clusters';
      else if (sel.length === 0) el.textContent = 'None selected';
      else el.textContent = sel.length + ' of ' + boxes.length + ' clusters';
    }
    function onClusterChange() { updateClusterLabel(); applyFilters(); }
    function setAllClusters(v) { allClusterBoxes().forEach(c => c.checked = v); onClusterChange(); }
    function toggleMs(e) { e.stopPropagation(); document.getElementById('msPanel').classList.toggle('hidden'); }
    document.addEventListener('click', function(e) {
      var ms = document.getElementById('clusterMs');
      if (ms && !ms.contains(e.target)) document.getElementById('msPanel').classList.add('hidden');
    });

    function applyFilters() {
      var status = document.getElementById('statusFilter').value;
      var search = document.getElementById('searchFilter').value.toLowerCase();
      var sel = checkedClusters(), allC = sel.length === allClusterBoxes().length;
      var selSet = {}; sel.forEach(c => selSet[c] = true);
      var visible = 0;
      document.querySelectorAll('.tc-row').forEach(function(row) {
        var ok = (allC || selSet[row.dataset.cluster] === true)
              && (status === 'ALL' || row.dataset.status === status)
              && (search === '' || row.textContent.toLowerCase().indexOf(search) !== -1);
        row.classList.toggle('hidden', !ok);
        if (ok) visible++;
      });
      document.getElementById('result-count').textContent = 'Showing ' + visible + ' of ' + TOTAL + ' test cases';
      document.getElementById('noResults').classList.toggle('hidden', visible > 0);
      document.getElementById('searchClr').classList.toggle('show', search !== '');
      document.querySelectorAll('.tile').forEach(function(t) {
        t.classList.toggle('active', t.dataset.filter === status && status !== 'ALL');
      });
    }
    function setStatus(s) { document.getElementById('statusFilter').value = s; applyFilters(); }
    function clearSearch() { document.getElementById('searchFilter').value = ''; applyFilters(); }
    function clearFilters() {
      document.getElementById('statusFilter').value = 'ALL';
      document.getElementById('searchFilter').value = '';
      allClusterBoxes().forEach(c => c.checked = true);
      updateClusterLabel(); applyFilters();
    }

    function sortTable(col, type) {
      var tbody = document.getElementById('tableBody');
      var rows = Array.from(tbody.querySelectorAll('tr.tc-row'));
      var dir = (sortState.col === col && sortState.dir === 'asc') ? 'desc' : 'asc';
      sortState = { col: col, dir: dir };
      var sev = { FAIL:0, ERROR:1, RERUN:2, 'PASS*':3, CANCEL:4, PASS:5 };
      rows.sort(function(a, b) {
        var av, bv;
        if (type === 'num') { av = parseFloat(a.dataset.time) || 0; bv = parseFloat(b.dataset.time) || 0; }
        else if (type === 'status') { av = sev[a.dataset.status] ?? 9; bv = sev[b.dataset.status] ?? 9; }
        else { av = (a.dataset.tcid || '').toLowerCase(); bv = (b.dataset.tcid || '').toLowerCase(); }
        if (av < bv) return dir === 'asc' ? -1 : 1;
        if (av > bv) return dir === 'asc' ? 1 : -1;
        return 0;
      });
      rows.forEach(function(r) { tbody.appendChild(r); });
      document.querySelectorAll('th.sortable').forEach(function(th) {
        var a = th.querySelector('.arrow'); if (a) a.textContent = '⇅';
      });
      var ths = document.querySelectorAll('th');
      if (ths[col]) { var ar = ths[col].querySelector('.arrow'); if (ar) ar.textContent = dir === 'asc' ? '▲' : '▼'; }
    }

    updateClusterLabel();
    applyFilters();
  </script>
</body>
</html>"""
    html = (_TEMPLATE
            .replace("__FOOT_DOT__", foot_dot)
            .replace("__COMMIT__", str(bi_commit))
            .replace("__BRANCH__", str(bi_branch))
            .replace("__BUILT__", str(built_txt))
            .replace("__RUN_TIME__", str(run_time))
            .replace("__FOOTER_STATUS__", footer_status)
            .replace("__TILES__", stat_tiles)
            .replace("__CLUSTER_CB__", cluster_checkboxes)
            .replace("__TOTAL__", str(total))
            .replace("__ROWS__", rows_html))

    report_path.write_text(html)
    print(f"\n[REPORT] Written to: {report_path}")
    return report_path


# =============================================================================
# Main
# =============================================================================
def preflight_ldd_check(cfg: dict, commands: list[dict]) -> list[dict]:
    """
    Before running any TC, `ldd` every DISTINCT DUT binary that the run will
    launch and report missing RUNTIME shared libraries ONCE, up-front — instead
    of a confusing per-TC "rc=127 / error while loading shared libraries". Common
    for the camera app (needs ffmpeg/gstreamer runtime libs on the RPi).
    Returns [{binary, missing:[...]}] and writes logs/preflight.json for the job
    summary. Never aborts — apps with all libs still run.
    """
    sdk_dir = Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))
    # Map every enabled app + chip-tool binary name → its path on the RPi.
    name_to_path = {}
    for app in resolve_pipeline_apps(sdk_dir, cfg):
        if app.get("enabled"):
            name_to_path[app["binary_name"]] = sdk_dir / app["build_dir"] / app["binary_name"]
    ct = cfg.get("chip_tool", {})
    if ct.get("binary_name"):
        name_to_path[ct["binary_name"]] = sdk_dir / ct["build_dir"] / ct["binary_name"]

    # Only the binaries actually referenced by the TCs we're about to run.
    needed = set()
    for tc in commands:
        m = re.search(r"\./([^\s]+)", tc.get("dut_command", ""))
        if m:
            needed.add(m.group(1))

    problems, checked = [], 0
    for bin_name in sorted(needed):
        path = name_to_path.get(bin_name)
        if not path or not path.exists():
            continue   # a truly missing binary is reported per-TC by _find_binary
        checked += 1
        r = subprocess.run(f"ldd '{path}' 2>&1 | grep 'not found' || true",
                           shell=True, capture_output=True, text=True)
        missing = sorted({ln.strip().split()[0] for ln in r.stdout.splitlines() if ln.strip()})
        if missing:
            problems.append({"binary": bin_name, "missing": missing})

    print("\n" + "=" * 70)
    print(f"[PREFLIGHT] Checked shared libraries for {checked} DUT binary(ies).")
    if problems:
        print(f"[PREFLIGHT] ⚠️  {len(problems)} binary(ies) MISSING runtime libraries "
              f"— their TCs will fail to launch until installed on the RPi:")
        for p in problems:
            print(f"   ❌ {p['binary']}: {', '.join(p['missing'])}")
        print("[PREFLIGHT] Fix: install the app's runtime libs on the RPi "
              "(camera → ffmpeg/gstreamer; see Matter_CI/apt-packages.txt).")
    else:
        print("[PREFLIGHT] ✅ All DUT binaries resolve their shared libraries.")
    print("=" * 70)

    try:
        (PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)
        (PROJECT_ROOT / "logs" / "preflight.json").write_text(json.dumps(problems, indent=2))
    except OSError:
        pass
    return problems


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

    # One-shot shared-library preflight so missing runtime libs are reported
    # up-front (in the run log + job summary), not as a per-TC rc=127.
    preflight_ldd_check(cfg, commands)

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
