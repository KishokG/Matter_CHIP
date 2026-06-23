#!/usr/bin/env python3
"""
validate_config.py — Validates build_config.yaml before any build starts.
Runs on the GitHub Actions runner (not on RPi) as an early-exit gate.

Usage: python3 scripts/validate_config.py config/build_config.yaml
"""

import sys
import yaml
from pathlib import Path


def error(msg):
    print(f"❌  {msg}", file=sys.stderr)

def ok(msg):
    print(f"✅  {msg}")

def warn(msg):
    print(f"⚠️   {msg}")


def validate(config_path: str) -> bool:
    path = Path(config_path)
    if not path.exists():
        error(f"Config file not found: {config_path}")
        return False

    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
    except yaml.YAMLError as e:
        error(f"YAML parse error: {e}")
        return False

    passed = True

    # ── Top-level sections ────────────────────────────────────────────────────
    for key in ["sdk", "apps", "chip_tool", "python_controller", "rpi"]:
        if key not in cfg:
            error(f"Missing required section: '{key}'")
            passed = False

    if not passed:
        return False

    # ── SDK ───────────────────────────────────────────────────────────────────
    if not cfg["sdk"].get("repo"):
        error("sdk.repo must not be empty")
        passed = False
    if not cfg["sdk"].get("branch"):
        error("sdk.branch must not be empty")
        passed = False

    sha = cfg["sdk"].get("sha", "")
    if sha and not all(c in "0123456789abcdefABCDEF" for c in sha):
        error(f"sdk.sha looks invalid (expected hex string): '{sha}'")
        passed = False
    elif sha:
        ok(f"SDK pinned to SHA: {sha}")
    else:
        ok(f"SDK branch: {cfg['sdk']['branch']} (floating HEAD)")

    # ── Apps ──────────────────────────────────────────────────────────────────
    if not isinstance(cfg["apps"], list):
        error("'apps' must be a list")
        passed = False
    else:
        enabled_apps = []
        for i, app in enumerate(cfg["apps"]):
            for key in ["name", "enabled", "source_dir", "build_dir", "binary_name"]:
                if key not in app:
                    error(f"apps[{i}] '{app.get('name','?')}' missing key: '{key}'")
                    passed = False
            if app.get("enabled"):
                enabled_apps.append(app["name"])
        if enabled_apps:
            ok(f"Enabled apps: {enabled_apps}")
        else:
            warn("No reference apps enabled")

    # ── chip-tool ─────────────────────────────────────────────────────────────
    if cfg["chip_tool"].get("enabled"):
        for key in ["source_dir", "build_dir", "binary_name"]:
            if not cfg["chip_tool"].get(key):
                error(f"chip_tool.{key} required when chip_tool.enabled=true")
                passed = False
        ok(f"chip-tool: enabled  (build_dir: {cfg['chip_tool']['build_dir']})")
    else:
        warn("chip-tool: disabled")

    # ── Python controller ─────────────────────────────────────────────────────
    if cfg["python_controller"].get("enabled"):
        if not cfg["python_controller"].get("install_venv_name"):
            error("python_controller.install_venv_name required when enabled")
            passed = False
        ok(f"python-controller: enabled  (venv: {cfg['python_controller'].get('install_venv_name')})")
    else:
        warn("python-controller: disabled")

    # ── RPi ───────────────────────────────────────────────────────────────────
    if not cfg["rpi"].get("sdk_dir"):
        error("rpi.sdk_dir is required")
        passed = False
    else:
        ok(f"RPi SDK dir: {cfg['rpi']['sdk_dir']}")

    # ── At least one target enabled ───────────────────────────────────────────
    any_enabled = (
        any(a.get("enabled") for a in cfg["apps"])
        or cfg["chip_tool"].get("enabled")
        or cfg["python_controller"].get("enabled")
    )
    if not any_enabled:
        error("Nothing is enabled — enable at least one app, chip-tool, or python-controller")
        passed = False

    return passed


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 validate_config.py <config_path>")
        sys.exit(1)

    success = validate(sys.argv[1])
    print()
    if success:
        print("✅  Config validation passed.")
        sys.exit(0)
    else:
        print("❌  Config validation FAILED.", file=sys.stderr)
        sys.exit(1)
