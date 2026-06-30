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
import re
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
# Step 2 — Expand brace-notation shorthand into individual target names
# =============================================================================
def expand_braces(s: str) -> list[str]:
    """
    Expands bash-style brace notation into individual strings.
    e.g. "linux-arm64-{a,b,c}[-x][-y]" with all bracket groups OPTIONAL
    (each [-x] either present or absent) and all {a,b,c} groups REQUIRED
    (exactly one alternative chosen).

    Returns the base combinations WITHOUT optional modifiers expanded
    (modifiers are returned separately) — we only need the base app names
    for our purposes, not the full combinatorial explosion (which would
    be in the tens of thousands).
    """
    # Step 1: extract and replace {a,b,c} groups (required, pick one)
    brace_groups = re.findall(r'\{([^}]+)\}', s)
    # Step 2: extract [optional] groups separately — we don't expand these,
    # just strip them, since we only care about base app names for config
    base = re.sub(r'\[[^\]]*\]', '', s)  # strip all [optional] modifiers
    base = re.sub(r'\{[^}]+\}', '\0', base)  # placeholder for brace group

    if not brace_groups:
        return [base] if base else []

    # Replace the {…} placeholder with each alternative
    results = []
    for group in brace_groups:
        alternatives = group.split(',')
        for alt in alternatives:
            expanded = re.sub(r'\{[^}]+\}', alt, s, count=1)
            expanded = re.sub(r'\[[^\]]*\]', '', expanded)  # strip modifiers
            results.append(expanded)
    return results


def get_modifiers(s: str) -> list[str]:
    """Extracts the list of optional [-modifier] suffixes available."""
    return re.findall(r'\[-([^\]]+)\]', s)


# =============================================================================
# Step 3 — Filter + expand targets relevant to our pipeline
# =============================================================================
def filter_targets(targets: list[dict], platform_filter: str = "") -> list[dict]:
    """
    Filters the full SDK target list down to what's relevant for our
    RPi-based Matter CI pipeline — Linux host builds only by default.
    Expands brace-notation shorthand into individual app target names.
    """
    expanded = []
    for t in targets:
        shorthand = t.get("shorthand", "") or t.get("name", "")
        prefix = platform_filter if platform_filter else "linux"
        if not shorthand.startswith(prefix):
            continue

        modifiers = get_modifiers(shorthand)
        for individual in expand_braces(shorthand):
            if not individual:
                continue
            expanded.append({
                "shorthand": individual,
                "raw": shorthand,
                "modifiers": modifiers,
                "name": t.get("name", individual),
            })
    return expanded


# =============================================================================
# Step 3 — Pretty print for human review
# =============================================================================
def print_targets(targets: list[dict]):
    print()
    print("=" * 78)
    print(f"  Available app targets ({len(targets)} matched, expanded from SDK shorthand)")
    print("=" * 78)
    seen = set()
    for t in sorted(targets, key=lambda x: x.get("shorthand", "")):
        shorthand = t.get("shorthand", "?")
        if shorthand in seen:
            continue
        seen.add(shorthand)
        print(f"  {shorthand}")

    # Show available modifiers once (same set applies to all linux-arm64 targets)
    all_modifiers = set()
    for t in targets:
        all_modifiers.update(t.get("modifiers", []))
    if all_modifiers:
        print()
        print(f"  Available optional modifiers ({len(all_modifiers)}):")
        for m in sorted(all_modifiers):
            print(f"    [-{m}]")
        print()
        print("  Modifiers can be appended to any target above, e.g.:")
        print("    linux-arm64-chip-tool-ipv6only-platform-mdns")
        print("    linux-arm64-all-clusters-no-ble-platform-mdns")
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
# Step 4.5 — Resolve real source_dir + binary_name from BUILD.gn
# =============================================================================
def resolve_app_path(sdk_dir: Path, app_guess: str) -> Path | None:
    """
    Finds the real examples/<dir> path for a given SDK target shorthand
    name. The shorthand (e.g. "network-manager") often does NOT match the
    actual examples/ folder name (e.g. "network-manager-app") — so we
    search rather than assume.

    Tries, in order:
      1. examples/<app_guess>/linux/         (exact match)
      2. examples/<app_guess>-app/linux/     (with -app suffix)
      3. Fuzzy: any examples/*<app_guess>*/linux/ containing a BUILD.gn
    """
    examples_dir = sdk_dir / "examples"
    if not examples_dir.exists():
        return None

    candidates = [
        examples_dir / app_guess / "linux",
        examples_dir / f"{app_guess}-app" / "linux",
    ]
    for c in candidates:
        if (c / "BUILD.gn").exists():
            return c

    # Fuzzy fallback — search for any folder containing app_guess
    for d in examples_dir.glob(f"*{app_guess}*"):
        linux_dir = d / "linux"
        if (linux_dir / "BUILD.gn").exists():
            return linux_dir

    return None


def resolve_binary_name(linux_dir: Path) -> str | None:
    """
    Parses BUILD.gn for the real executable() target name.
    Looks for: executable("some-binary-name") { ... }
    Returns the first match, or None if not found.
    """
    build_gn = linux_dir / "BUILD.gn"
    if not build_gn.exists():
        return None

    try:
        text = build_gn.read_text()
    except Exception:
        return None

    match = re.search(r'executable\("([^"]+)"\)', text)
    if match:
        return match.group(1)

    # Some apps use output_name = "..." inside the executable block instead
    match = re.search(r'output_name\s*=\s*"([^"]+)"', text)
    if match:
        return match.group(1)

    return None


def resolve_apps(sdk_dir: Path, app_guesses: list[str]) -> list[dict]:
    """
    For each candidate app name, attempts to resolve the REAL source_dir
    and binary_name by inspecting the actual SDK checkout, instead of
    guessing based on naming convention.
    """
    resolved = []
    for app_guess in app_guesses:
        linux_dir = resolve_app_path(sdk_dir, app_guess)
        if linux_dir is None:
            resolved.append({
                "name": app_guess,
                "found": False,
                "source_dir": None,
                "binary_name": None,
            })
            continue

        binary_name = resolve_binary_name(linux_dir)
        rel_source_dir = str(linux_dir.relative_to(sdk_dir))

        resolved.append({
            "name": app_guess,
            "found": True,
            "source_dir": rel_source_dir,
            "binary_name": binary_name,
        })
    return resolved


# =============================================================================
# Step 5 — Generate YAML snippet for new apps not yet in config
# =============================================================================
# Targets that aren't really "apps" — utility/test/platform targets we
# don't want suggested as buildable reference apps for the CI pipeline
SKIP_PATTERNS = (
    "test", "fuzz", "tests", "minmdns", "rpc-console", "shell",
    "java-matter-controller", "kotlin-matter-controller",
    "python-bindings", "simulated-app", "address-resolve-tool",
)

def generate_yaml_snippet(targets: list[dict], sdk_dir: Path):
    """
    Prints a ready-to-paste YAML snippet for apps/ entries, using REAL
    source_dir and binary_name values resolved by inspecting the actual
    SDK checkout (examples/ folders + BUILD.gn executable() targets) —
    not guessed from naming convention.
    """
    print()
    print("=" * 78)
    print("  Suggested build_config.yaml 'apps:' entries")
    print("  (source_dir + binary_name resolved from actual SDK checkout)")
    print("=" * 78)
    print()

    seen = set()
    app_guesses = []
    for t in sorted(targets, key=lambda x: x.get("shorthand", "")):
        shorthand = t.get("shorthand", "")
        for prefix in ("linux-x64-", "linux-arm64-"):
            if shorthand.startswith(prefix):
                app_guess = shorthand[len(prefix):]
                break
        else:
            continue
        if app_guess in seen or not app_guess:
            continue
        if any(skip in app_guess for skip in SKIP_PATTERNS):
            continue
        seen.add(app_guess)
        app_guesses.append(app_guess)

    print(f"[INFO] Resolving {len(app_guesses)} app paths against SDK checkout...")
    resolved = resolve_apps(sdk_dir, app_guesses)

    found_count = 0
    not_found = []
    for r in resolved:
        if not r["found"]:
            not_found.append(r["name"])
            continue

        found_count += 1
        binary = r["binary_name"] or f'UNKNOWN   # could not parse BUILD.gn — check manually'
        binary_line = (f'"{r["binary_name"]}"' if r["binary_name"]
                       else '"UNKNOWN"   # could not parse BUILD.gn — check manually')

        print(f'  - name: "{r["name"]}"')
        print(f'    enabled: false   # review before enabling')
        print(f'    source_dir: "{r["source_dir"]}"')
        print(f'    build_dir: "{r["source_dir"]}/out/{r["name"]}"')
        print(f'    binary_name: {binary_line}')
        print(f'    extra_gn_args: "chip_inet_config_enable_ipv4=false"')
        print()

    print("=" * 78)
    print(f"[INFO] Resolved {found_count}/{len(app_guesses)} apps with verified paths + binary names.")
    if not_found:
        print(f"[WARN] Could not locate examples/ folder for {len(not_found)} target(s):")
        for n in not_found:
            print(f"         - {n}")
        print("[WARN] These may be virtual/meta targets, renamed, or require")
        print("[WARN] a different folder structure. Skipped from output above.")
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
        generate_yaml_snippet(filtered, sdk_dir)
    else:
        print_targets(filtered)

    print()
    print("[DONE] This script is READ-ONLY — no files were modified, "
          "no build was triggered.")


if __name__ == "__main__":
    main()
