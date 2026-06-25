#!/usr/bin/env python3
"""
collect_build_info.py — Runs on the RPi after a build.
Prints binary sizes, paths, and any build error conclusions.

Usage: python3 scripts/collect_build_info.py config/build_config.yaml
"""

import os, sys, subprocess, yaml, json
from pathlib import Path
from datetime import datetime


def size_mb(path: Path) -> str:
    try:
        return f"{path.stat().st_size / 1_048_576:.1f} MB"
    except FileNotFoundError:
        return "NOT FOUND"


def git_info(sdk_dir: Path) -> tuple:
    def run(cmd):
        try:
            return subprocess.run(cmd, cwd=sdk_dir, capture_output=True,
                                  text=True).stdout.strip()
        except Exception:
            return "unknown"
    return run(["git", "rev-parse", "HEAD"]), run(["git", "rev-parse", "--abbrev-ref", "HEAD"])


def load_error_log(log_dir: Path, app_name: str) -> str:
    """Load last 10 lines of build error log if it exists."""
    err_log = log_dir / f"{app_name}_build_error.log"
    if not err_log.exists():
        return ""
    lines = err_log.read_text(errors="replace").strip().splitlines()
    return "\n      ".join(lines[-10:]) if lines else ""


def main():
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config/build_config.yaml")
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    sdk_dir  = Path(os.environ.get("MATTER_SDK_DIR", cfg["rpi"]["sdk_dir"]))
    log_dir  = Path(__file__).parent.parent / "logs" / "build_logs"
    commit, branch = git_info(sdk_dir)

    print("=" * 62)
    print("  Matter CI — Build Summary")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 62)
    print(f"  SDK Dir    : {sdk_dir}")
    print(f"  Branch     : {branch}")
    print(f"  Commit     : {commit}")
    print()

    all_ok = True
    failed_apps = []

    # Reference apps
    print("── Reference Apps " + "─" * 44)
    for app in cfg.get("apps", []):
        if not app.get("enabled"):
            print(f"  ⏭   {app['name']:<32} disabled")
            continue
        binary = sdk_dir / app["build_dir"] / app["binary_name"]
        if binary.exists():
            print(f"  ✅  {app['name']:<32} {size_mb(binary):>8}   {binary}")
        else:
            print(f"  ❌  {app['name']:<32} {'MISSING':>8}")
            # Show error conclusion
            err_log = load_error_log(log_dir, app['name'])
            if err_log:
                print(f"      Error details:")
                print(f"      {err_log}")
            all_ok = False
            failed_apps.append(app['name'])

    print()

    # chip-tool
    print("── chip-tool " + "─" * 49)
    if cfg["chip_tool"].get("enabled"):
        b = sdk_dir / cfg["chip_tool"]["build_dir"] / cfg["chip_tool"]["binary_name"]
        if b.exists():
            print(f"  ✅  {'chip-tool':<32} {size_mb(b):>8}   {b}")
        else:
            print(f"  ❌  {'chip-tool':<32} {'MISSING':>8}")
            err_log = load_error_log(log_dir, "chip-tool")
            if err_log:
                print(f"      Error details:")
                print(f"      {err_log}")
            all_ok = False
            failed_apps.append("chip-tool")
    else:
        print("  ⏭   chip-tool   disabled")

    print()

    # Python controller
    print("── Python Controller " + "─" * 41)
    if cfg["python_controller"].get("enabled"):
        venv = sdk_dir / cfg["python_controller"]["install_venv_name"]
        if venv.exists():
            print(f"  ✅  venv → {venv}")
            python_bin = venv / "bin" / "python3"
            try:
                ver = subprocess.run(
                    [str(python_bin), "-c", "import chip; print(chip.__version__)"],
                    capture_output=True, text=True
                ).stdout.strip()
                print(f"  ✅  chip package version: {ver or 'installed (version unknown)'}")
            except Exception:
                pass
        else:
            print(f"  ❌  venv MISSING: {venv}")
            err_log = load_error_log(log_dir, "python-controller")
            if err_log:
                print(f"      Error details:")
                print(f"      {err_log}")
            all_ok = False
            failed_apps.append("python-controller")
    else:
        print("  ⏭   python-controller   disabled")

    print()
    print("=" * 62)
    if all_ok:
        print("  ✅  All enabled targets built successfully!")
    else:
        print(f"  ❌  {len(failed_apps)} target(s) failed: {', '.join(failed_apps)}")
        print("  ⚠️   Tests will run only for successfully built apps.")
    print("=" * 62)

    # Exit 0 even with partial failures — tests will handle it
    sys.exit(0)


if __name__ == "__main__":
    main()
