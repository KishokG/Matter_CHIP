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
    for key in ["sdk", "discovery", "chip_tool", "python_controller", "rpi"]:
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

    # ── Discovery (dynamic reference apps) ──────────────────────────────────────
    # Reference apps are resolved from the SDK at build time by
    # discover_targets.py. Here we validate only the SHAPE of the discovery
    # block — the SDK isn't present on this (ubuntu-latest) runner, so the
    # shorthand names and modifiers are validated against the SDK during build.
    KNOWN_MODIFIERS = {"ipv6only", "platform-mdns", "nfc-commission",
                       "nlfaultinject", "rpc", "clang", "no-werror"}
    disc = cfg["discovery"]
    if not isinstance(disc, dict) or not isinstance(disc.get("apps"), list):
        error("'discovery' must be a mapping with an 'apps' list "
              "(see build_config.yaml)")
        passed = False
    else:
        # discovery.apps — explicit per-app list with enabled + modifiers.
        enabled_apps = []
        for i, app in enumerate(disc["apps"]):
            if not isinstance(app, dict) or not app.get("name"):
                error(f"discovery.apps[{i}] must be a mapping with a 'name'")
                passed = False
                continue
            mods = app.get("modifiers", [])
            if mods is not None and not isinstance(mods, list):
                error(f"discovery.apps[{i}] '{app['name']}' modifiers must be a list")
                passed = False
            for m in (mods or []):
                if m not in KNOWN_MODIFIERS:
                    warn(f"discovery.apps '{app['name']}' has unknown modifier "
                         f"'{m}' — it will be ignored at build time")
            if app.get("enabled"):
                enabled_apps.append(app["name"])
        if enabled_apps:
            ok(f"Discovery: {len(enabled_apps)}/{len(disc['apps'])} app(s) enabled: {enabled_apps}")
        else:
            warn("No apps enabled in discovery.apps — only chip-tool / python "
                 "controller will build")

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
        isinstance(cfg.get("discovery"), dict)
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
