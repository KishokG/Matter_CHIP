#!/usr/bin/env python3
"""
discover_targets.py
====================
Queries the Matter SDK's own build_examples.py for the canonical list of
buildable targets, instead of relying on hand-maintained entries in
build_config.yaml.

This is a READ-ONLY discovery tool — it does not modify build_config.yaml
or trigger any builds. It helps you:
  1. See all targets the SDK currently supports for a given platform
  2. Cross-check your existing build_config.yaml apps list against the SDK
  3. Generate a ready-to-paste YAML snippet for new/missing apps

How it works:
  Runs: scripts/build/build_examples.py targets --format json
  inside the SDK directory (after sourcing scripts/activate.sh), then
  filters/parses the JSON output.

Usage:
  python3 scripts/discover_targets.py --sdk-dir /home/ubuntu/connectedhomeip
  python3 scripts/discover_targets.py --sdk-dir ~/connectedhomeip --platform linux
  python3 scripts/discover_targets.py --sdk-dir ~/connectedhomeip --compare config/build_config.yaml
  python3 scripts/discover_targets.py --sdk-dir ~/connectedhomeip --generate-yaml
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent

try:
    import yaml
except ImportError:
    yaml = None  # only needed for --compare / --generate-yaml


# =============================================================================
# Step 1 — Query the SDK for its canonical target list
# =============================================================================
def fetch_targets(sdk_dir: Path) -> list[dict]:
    """
    Runs `scripts/build/build_examples.py targets --format json` inside
    the SDK directory and returns the parsed list of target dicts.
    """
    if not sdk_dir.exists():
        print(f"[ERROR] SDK directory not found: {sdk_dir}")
        sys.exit(1)

    build_examples = sdk_dir / "scripts" / "build" / "build_examples.py"
    if not build_examples.exists():
        print(f"[ERROR] build_examples.py not found at: {build_examples}")
        print(f"[ERROR] Is this a valid connectedhomeip checkout?")
        sys.exit(1)

    print(f"[INFO] Querying SDK target list from: {sdk_dir}")
    print(f"[INFO] Running: source scripts/activate.sh && "
          f"scripts/build/build_examples.py targets --format json")

    # Run inside a bash subshell so `source scripts/activate.sh` works,
    # exactly like the official TH Dockerfile does.
    cmd = (
        "source scripts/activate.sh >/dev/null 2>&1 && "
        "scripts/build/build_examples.py targets --format json"
    )

    try:
        result = subprocess.run(
            ["bash", "-c", cmd],
            cwd=str(sdk_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        print("[ERROR] Timed out querying targets (120s). "
              "Has scripts/bootstrap.sh been run at least once?")
        sys.exit(1)

    if result.returncode != 0:
        print(f"[ERROR] build_examples.py exited with code {result.returncode}")
        print(f"[ERROR] stderr:\n{result.stderr[-2000:]}")
        print()
        print("[HINT] Common causes:")
        print("  - scripts/activate.sh not bootstrapped yet "
              "(run scripts/bootstrap.sh once first)")
        print("  - SDK checkout is incomplete (missing submodules)")
        sys.exit(1)

    try:
        targets = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Could not parse JSON output: {e}")
        print(f"[ERROR] Raw output (first 500 chars):\n{result.stdout[:500]}")
        print()
        print("[HINT] --format json requires a recent SDK checkout "
              "(merged via PR #25810). If your SDK predates this, "
              "upgrade or use the plain-text 'targets' command manually.")
        sys.exit(1)

    print(f"[INFO] SDK reports {len(targets)} total buildable targets")
    return targets


# =============================================================================
# Step 2 — Filter targets relevant to our pipeline (Linux host builds)
# =============================================================================
def filter_targets(targets: list[dict], platform_filter: str = "") -> list[dict]:
    """
    Filters the full SDK target list down to what's relevant for our
    RPi-based Matter CI pipeline — Linux host builds only by default.
    """
    filtered = []
    for t in targets:
        shorthand = t.get("shorthand", "") or t.get("name", "")
        if platform_filter:
            if not shorthand.startswith(platform_filter):
                continue
        else:
            # Default: only linux-* targets (what we actually build on RPi)
            if not shorthand.startswith("linux"):
                continue
        filtered.append(t)
    return filtered


# =============================================================================
# Step 3 — Pretty print for human review
# =============================================================================
def print_targets(targets: list[dict]):
    print()
    print("=" * 78)
    print(f"  Available targets ({len(targets)} matched)")
    print("=" * 78)
    for t in sorted(targets, key=lambda x: x.get("shorthand", x.get("name", ""))):
        shorthand = t.get("shorthand", t.get("name", "?"))
        print(f"  {shorthand}")
    print("=" * 78)


# =============================================================================
# Step 4 — Compare against existing build_config.yaml
# =============================================================================
def compare_with_config(targets: list[dict], config_path: Path):
    if yaml is None:
        print("[ERROR] pyyaml not installed — run: pip3 install pyyaml --break-system-packages")
        sys.exit(1)

    if not config_path.exists():
        print(f"[ERROR] Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    configured_apps = {app["binary_name"] for app in cfg.get("apps", [])}
    configured_apps.add(cfg.get("chip_tool", {}).get("binary_name", ""))

    # Build a set of "known good" shorthand target names from SDK
    sdk_shorthands = {t.get("shorthand", t.get("name", "")) for t in targets}

    print()
    print("=" * 78)
    print("  Comparison: build_config.yaml  vs  SDK-reported targets")
    print("=" * 78)

    print(f"\n[CONFIGURED] {len(configured_apps)} app(s) in build_config.yaml:")
    for app in sorted(configured_apps):
        print(f"  - {app}")

    print(f"\n[SDK TARGETS] {len(sdk_shorthands)} linux-* target(s) available "
          f"in this SDK checkout:")
    for t in sorted(sdk_shorthands)[:30]:
        print(f"  - {t}")
    if len(sdk_shorthands) > 30:
        print(f"  ... and {len(sdk_shorthands) - 30} more "
              f"(run without --compare to see full list)")

    print()
    print("[NOTE] SDK target 'shorthand' names (e.g. linux-x64-chip-tool-...)")
    print("       don't map 1:1 to our binary_name entries — this comparison")
    print("       is for manual cross-reference, not automatic matching.")
    print("=" * 78)


# =============================================================================
# Step 5 — Generate YAML snippet for new apps not yet in config
# =============================================================================
def generate_yaml_snippet(targets: list[dict]):
    """
    Prints a ready-to-paste YAML snippet for apps/ entries based on
    discovered SDK example app directories — cross-referenced against
    the standard examples/ folder naming convention.
    """
    print()
    print("=" * 78)
    print("  Suggested build_config.yaml 'apps:' entries")
    print("  (Review and adjust source_dir/build_dir before pasting!)")
    print("=" * 78)
    print()

    # Known example app name patterns we can map to source_dir convention
    seen = set()
    for t in targets:
        shorthand = t.get("shorthand", t.get("name", ""))
        # crude heuristic: strip linux-x64- / linux-arm64- prefix
        for prefix in ("linux-x64-", "linux-arm64-"):
            if shorthand.startswith(prefix):
                app_guess = shorthand[len(prefix):]
                break
        else:
            continue

        # Strip common suffix modifiers
        for suffix in ("-ipv6only-platform-mdns", "-platform-mdns", "-mdns"):
            if app_guess.endswith(suffix):
                app_guess = app_guess[: -len(suffix)]

        if app_guess in seen or not app_guess:
            continue
        seen.add(app_guess)

        print(f'  - name: "{app_guess}"')
        print(f'    enabled: false   # review before enabling')
        print(f'    source_dir: "examples/{app_guess}/linux"   # VERIFY this path exists')
        print(f'    build_dir: "examples/{app_guess}/linux/out/{app_guess}"')
        print(f'    binary_name: "chip-{app_guess}"   # VERIFY actual binary name')
        print(f'    extra_gn_args: "chip_inet_config_enable_ipv4=false"')
        print()

    print("=" * 78)
    print(f"[INFO] Generated {len(seen)} candidate entries.")
    print("[INFO] These are HEURISTIC GUESSES based on target name parsing.")
    print("[INFO] Always verify source_dir/binary_name against the actual")
    print("[INFO] examples/ folder structure before adding to your config.")
    print("=" * 78)


# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", required=True,
                        help="Path to connectedhomeip SDK checkout")
    parser.add_argument("--platform", default="",
                        help="Filter by platform prefix (default: 'linux')")
    parser.add_argument("--compare", metavar="CONFIG_PATH",
                        help="Compare SDK targets against an existing "
                             "build_config.yaml")
    parser.add_argument("--generate-yaml", action="store_true",
                        help="Print a suggested YAML snippet for apps: section")
    parser.add_argument("--save-json", metavar="PATH",
                        help="Save raw SDK target list as JSON to this path")
    args = parser.parse_args()

    sdk_dir = Path(args.sdk_dir).expanduser().resolve()

    all_targets = fetch_targets(sdk_dir)

    if args.save_json:
        Path(args.save_json).write_text(json.dumps(all_targets, indent=2))
        print(f"[INFO] Saved raw target list: {args.save_json}")

    filtered = filter_targets(all_targets, args.platform)

    if args.compare:
        compare_with_config(filtered, Path(args.compare))
    elif args.generate_yaml:
        generate_yaml_snippet(filtered)
    else:
        print_targets(filtered)

    print()
    print("[DONE] This script is READ-ONLY — no files were modified, "
          "no build was triggered.")


if __name__ == "__main__":
    main()
