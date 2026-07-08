#!/usr/bin/env python3
"""
discover_targets.py
====================
Resolves the pipeline's reference apps from the Matter SDK itself — no
hand-maintained source_dir/binary_name lists. It reads the SDK's own
build_examples.py target list + HostApp mapping + each app's BUILD.gn, and
turns build_config.yaml's `discovery.apps` (enabled flags + Test-Harness
modifiers) into the concrete build list every consumer uses.

READ-ONLY: it never modifies build_config.yaml or triggers a build.

Importable API (used by build_inside_container.sh, upload_artifacts.py,
run_tests.py, fetch_test_commands.py):
  resolve_pipeline_apps(sdk_dir, config) -> [{name, enabled, source_dir,
                                              build_dir, binary_name, extra_gn_args}]

CLI:
  # machine-readable resolved apps (stdout JSON) — used by the build container:
  python3 scripts/discover_targets.py --sdk-dir <SDK> --config <build_config.yaml> --emit-apps-json
  # regenerate the full discovery.apps: menu (paste into build_config.yaml):
  python3 scripts/discover_targets.py --sdk-dir <SDK> --emit-config-apps
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
    yaml = None  # only needed by load_config() for --emit-apps-json


# =============================================================================
# Step 1 — Query the SDK for its canonical target list
# =============================================================================
def fetch_targets(sdk_dir: Path, quiet: bool = False) -> list[dict]:
    """
    Runs `scripts/build/build_examples.py targets --format json` inside
    the SDK directory and returns the parsed list of target dicts.

    When quiet=True, all progress/diagnostic output goes to stderr so that
    stdout can carry machine-readable JSON only (used by --emit-apps-json).
    """
    # Progress lines go to stderr in quiet mode; errors always to stderr.
    out = sys.stderr if quiet else sys.stdout

    if not sdk_dir.exists():
        print(f"[ERROR] SDK directory not found: {sdk_dir}", file=sys.stderr)
        sys.exit(1)

    build_examples = sdk_dir / "scripts" / "build" / "build_examples.py"
    if not build_examples.exists():
        print(f"[ERROR] build_examples.py not found at: {build_examples}", file=sys.stderr)
        print(f"[ERROR] Is this a valid connectedhomeip checkout?", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] Querying SDK target list from: {sdk_dir}", file=out)
    print(f"[INFO] Running: source scripts/activate.sh && "
          f"scripts/build/build_examples.py targets --format json", file=out)

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
              "Has scripts/bootstrap.sh been run at least once?", file=sys.stderr)
        sys.exit(1)

    if result.returncode != 0:
        print(f"[ERROR] build_examples.py exited with code {result.returncode}", file=sys.stderr)
        print(f"[ERROR] stderr:\n{result.stderr[-2000:]}", file=sys.stderr)
        print(file=sys.stderr)
        print("[HINT] Common causes:", file=sys.stderr)
        print("  - scripts/activate.sh not bootstrapped yet "
              "(run scripts/bootstrap.sh once first)", file=sys.stderr)
        print("  - SDK checkout is incomplete (missing submodules)", file=sys.stderr)
        sys.exit(1)

    try:
        targets = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Could not parse JSON output: {e}", file=sys.stderr)
        print(f"[ERROR] Raw output (first 500 chars):\n{result.stdout[:500]}", file=sys.stderr)
        print(file=sys.stderr)
        print("[HINT] --format json requires a recent SDK checkout "
              "(merged via PR #25810). If your SDK predates this, "
              "upgrade or use the plain-text 'targets' command manually.", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] SDK reports {len(targets)} total buildable targets", file=out)
    return targets


# =============================================================================
# Step 4.5 — Resolve real source_dir + binary_name from BUILD.gn
# =============================================================================
def resolve_app_path(sdk_dir: Path, app_guess: str) -> Path | None:
    """
    Finds the real examples/<dir> path for a given SDK target shorthand
    name. The shorthand (e.g. "network-manager") often does NOT match the
    actual examples/ folder name (e.g. "network-manager-app") — so we
    search rather than assume.

    Two folder conventions exist in the SDK:
      A. Most device apps:  examples/<name>-app/linux/BUILD.gn
      B. Tool-type binaries: examples/<name>/BUILD.gn (NO linux/ subfolder)
         e.g. chip-tool, fabric-admin, fabric-sync, chip-cert

    Tries, in order:
      1. examples/<app_guess>/linux/BUILD.gn          (device app, exact name)
      2. examples/<app_guess>-app/linux/BUILD.gn       (device app, -app suffix)
      3. examples/<app_guess>/BUILD.gn                 (tool-type, no linux/)
      4. Fuzzy: any examples/*<app_guess>*/linux/BUILD.gn
      5. Fuzzy: any examples/*<app_guess>*/BUILD.gn (no linux/)
    """
    examples_dir = sdk_dir / "examples"
    if not examples_dir.exists():
        return None

    candidates = [
        examples_dir / app_guess / "linux",
        examples_dir / f"{app_guess}-app" / "linux",
        examples_dir / app_guess,                  # tool-type, no linux/
    ]
    for c in candidates:
        if (c / "BUILD.gn").exists():
            return c

    # Fuzzy fallback — search for any folder containing app_guess
    for d in examples_dir.glob(f"*{app_guess}*"):
        linux_dir = d / "linux"
        if (linux_dir / "BUILD.gn").exists():
            return linux_dir
        # Also check the dir itself (tool-type, no linux/)
        if (d / "BUILD.gn").exists():
            return d

    return None


def resolve_binary_name(linux_dir: Path) -> str | None:
    """
    Parses BUILD.gn for the real executable() target name.
    Looks for: executable("some-binary-name") { ... }

    Some BUILD.gn files declare MULTIPLE executable() blocks — e.g.
    a normal variant plus a "-fuzzing" or "-test" variant. We must
    prefer the standard/normal one, not just take the first regex match
    (which can incorrectly grab a fuzzing-only build target).
    """
    build_gn = linux_dir / "BUILD.gn"
    if not build_gn.exists():
        return None

    try:
        text = build_gn.read_text()
    except Exception:
        return None

    all_matches = re.findall(r'executable\("([^"]+)"\)', text)

    if not all_matches:
        # Some apps use output_name = "..." inside the executable block instead
        match = re.search(r'output_name\s*=\s*"([^"]+)"', text)
        return match.group(1) if match else None

    if len(all_matches) == 1:
        return all_matches[0]

    # Multiple executable() blocks found — filter out known variant suffixes
    # and prefer the "plain" one (no -fuzzing/-test/-asan/etc suffix)
    SKIP_SUFFIXES = ("-fuzzing", "-test", "-tests", "-asan", "-tsan", "-ubsan")
    plain_candidates = [
        m for m in all_matches
        if not any(m.endswith(suffix) for suffix in SKIP_SUFFIXES)
    ]

    if plain_candidates:
        # Prefer the shortest plain candidate (most likely the base name,
        # since variant names tend to be longer with extra suffixes)
        return min(plain_candidates, key=len)

    # All matches had variant suffixes — fall back to shortest overall
    return min(all_matches, key=len)


# =============================================================================
# Authoritative resolution via the SDK's own HostApp mapping
#
# This is the CANONICAL way to map an SDK target shorthand (e.g. "light")
# to its real source dir + output binary. The SDK's build system defines
# HostApp.ExamplePath() and HostApp.OutputNames() in
#   scripts/build/builders/host.py
# and build_examples.py tags each linux app part with its HostApp enum
# (parts[...].build_arguments.app = "HostApp.LIGHT"). We reuse that mapping
# directly instead of guessing folder names — which is fragile (e.g. the
# shorthand "light" must map to examples/lighting-app, NOT light-switch-app).
# =============================================================================

# Apps built via their own dedicated pipeline step (not as reference apps).
SEPARATELY_BUILT = {"chip-tool"}

# -----------------------------------------------------------------------------
# Matter Test Harness build parity
#
# The TH's DUT binaries are the SDK's chip-cert-bins outputs, built via
#   scripts/build/build_examples.py --target linux-<arch>-<app>-<modifiers>
# (see integrations/docker/images/chip-cert-bins/Dockerfile). We build the
# same apps NATIVELY on the RPi with gn_build_example.sh, so we translate each
# TH target modifier into its gn arg(s). This mapping is taken verbatim from
# the SDK:
#   scripts/build/build/targets.py   (AppendModifier(<name>, <kwarg>=...))
#   scripts/build/builders/host.py   (kwarg -> self.extra_gn_options)
# Keeping it here means one edit to add a modifier the SDK later introduces.
# -----------------------------------------------------------------------------
MODIFIER_GN_ARGS = {
    "ipv6only":       "chip_inet_config_enable_ipv4=false",
    "platform-mdns":  'chip_mdns="platform"',
    "nfc-commission": "chip_enable_nfc_based_commissioning=true",
    "nlfaultinject":  "chip_with_nlfaultinjection=true",
    "rpc":            'import("//with_pw_rpc.gni")',
    "clang":          "is_clang=true",
    # NOT a Test-Harness modifier — a local escape hatch for apps that fail
    # ONLY on an upstream -Werror warning (e.g. refrigerator's ignored
    # [[nodiscard]] CHIP_ERROR). Drops -Werror for that app so a benign warning
    # isn't fatal. Do NOT use on core cert apps — it hides real regressions.
    "no-werror":      "treat_warnings_as_errors=false",
}

# TH default for virtually every linux reference app (ipv6-only host).
DEFAULT_MODIFIERS = ["ipv6only"]

# Reference apps the Test Harness builds with NON-default modifiers, taken from
# chip-cert-bins (integrations/docker/images/chip-cert-bins/Dockerfile). Every
# other reference app uses DEFAULT_MODIFIERS.
CERT_BINS_MODIFIERS = {
    "fabric-bridge": ["rpc", "ipv6only"],   # linux-*-fabric-bridge-rpc-ipv6only
    "fabric-admin":  ["rpc", "ipv6only"],   # linux-*-fabric-admin-rpc-ipv6only
    "camera":        ["clang", "ipv6only"], # linux-arm64-camera-clang-ipv6only (RPi is arm64)
}

# HostApp enum members that are NOT deployable sample/DUT apps — tools, test
# harnesses, language-binding controllers, and meta targets (their ExamplePath
# is '../', 'placeholder/', 'minimal-mdns', 'shell/standalone', etc.). These are
# omitted from the generated app menu. (chip-tool is handled via SEPARATELY_BUILT.)
NON_APP_ENUMS = {
    "ADDRESS_RESOLVE", "TESTS", "PYTHON_BINDINGS", "CERT_TOOL",
    "SIMULATED_APP1", "SIMULATED_APP2", "MIN_MDNS", "SHELL",
    "RPC_CONSOLE", "EFR32_TEST_RUNNER", "CHIP_TOOL_DARWIN",
    "JAVA_MATTER_CONTROLLER", "KOTLIN_MATTER_CONTROLLER",
}

# Apps enabled by default when generating a fresh discovery.apps block —
# mirrors the pipeline's original curated set. Everything else is emitted
# enabled: false so the operator opts in explicitly.
DEFAULT_ENABLED = {
    "all-clusters", "light", "lock", "thermostat", "bridge",
    "air-purifier", "evse", "closure", "network-manager",
}


def modifiers_to_gn_args(modifiers) -> str:
    """Translate a list of TH target modifiers into a gn-args string."""
    args = []
    for m in modifiers:
        gn = MODIFIER_GN_ARGS.get(m)
        if gn is None:
            print(f"[WARN] Unknown TH modifier '{m}' — skipped (no gn-arg "
                  f"mapping in MODIFIER_GN_ARGS).", file=sys.stderr)
            continue
        args.append(gn)
    return " ".join(args)


def _valid_binary_name(n) -> bool:
    """A usable binary name — non-empty, no path sep, no unresolved gn template."""
    return bool(n) and "/" not in n and "$" not in n and "{" not in n


def _resolve_source_binary(name, enum_name, HostApp, sdk_dir):
    """
    Resolve (source_dir, binary_name, is_reference_app) for one app.

    - source_dir: from the authoritative HostApp.ExamplePath() (avoids the
      fragile fuzzy folder match, e.g. "light" -> lighting-app not light-switch);
      falls back to a fuzzy example-folder search if HostApp is unavailable.
    - binary_name: from the ACTUAL BUILD.gn executable() (what gn_build_example
      really produces), because HostApp.OutputNames() can drift from it on some
      SDK versions (e.g. dishwasher: OutputNames 'dishwasher-app' vs BUILD.gn
      'chip-dishwasher-app'). Falls back to OutputNames only when BUILD.gn can't
      be parsed to a clean name (e.g. simulated-app uses a '${var}' template).
      This keeps collect/upload/run_tests aligned with the real built file.

    Returns (None, None, False) if unresolvable.
    """
    source_dir = None
    output_name = None   # HostApp.OutputNames() — used as a fallback only

    if HostApp is not None and enum_name:
        try:
            app_enum = HostApp[enum_name]
            source_dir = f"examples/{app_enum.ExamplePath()}"
        except Exception:  # noqa: BLE001 — unknown enum → fall back below
            source_dir = None
        try:
            output_name = _first_binary(app_enum.OutputNames())
        except Exception:  # noqa: BLE001
            output_name = None

    # Fallback: locate the example folder by fuzzy search.
    if not source_dir:
        linux_dir = resolve_app_path(sdk_dir, name)
        if linux_dir is not None:
            source_dir = str(linux_dir.relative_to(sdk_dir))

    if not source_dir:
        return None, None, False

    src_abs = sdk_dir / source_dir
    if not src_abs.exists():
        return None, None, False

    # Prefer the real executable() name from BUILD.gn; fall back to OutputNames.
    gn_name = resolve_binary_name(src_abs)
    if _valid_binary_name(gn_name):
        binary_name = gn_name
    elif _valid_binary_name(output_name):
        binary_name = output_name
    else:
        return None, None, False

    is_ref = source_dir.endswith("/linux") or source_dir.endswith("/posix")
    return source_dir, binary_name, is_ref


def extract_app_parts(targets: list[dict]) -> list[dict]:
    """
    Finds the native 'linux' host target in the SDK target list and returns
    its application dimension as [{"name": <shorthand>, "hostapp": <ENUM>}].

    The linux target is described as linux-{arm64,x64}-{...apps...}; its
    'parts' is a list of dimension groups. We pick the group whose entries
    carry a `build_arguments.app` = "HostApp.XXX" (the app dimension). The
    arm64/x64 dimension is irrelevant here — source dirs and binary names
    are architecture-independent (native gn_build_example.sh builds).

    NOTE: several targets share name == "linux" (linux-fake-tests,
    linux-x64-efr32-test-runner, and the main linux-{arm64,x64}-{...} host
    target). We do NOT filter by target name (it has varied across SDK
    versions) — instead we scan ALL targets' part groups and return the
    LARGEST group whose entries carry a `build_arguments.app` of the form
    "HostApp.XXX". Only the linux host target uses HostApp.* enums, so this
    reliably isolates the linux reference-app list regardless of target name.
    """
    best: list[dict] = []
    for t in targets:
        for group in t.get("parts", []):
            if not isinstance(group, list):
                continue
            apps = []
            for part in group:
                ba = (part or {}).get("build_arguments", {}) or {}
                app_val = ba.get("app", "")
                if isinstance(app_val, str) and app_val.startswith("HostApp."):
                    apps.append({
                        "name": part.get("name", ""),
                        "hostapp": app_val.split(".", 1)[1],
                    })
            if len(apps) > len(best):
                best = apps
    return best


def import_hostapp(sdk_dir: Path):
    """
    Imports the SDK's HostApp enum so we can call ExamplePath()/OutputNames().
    Returns the HostApp class, or None if it can't be imported (in which case
    callers fall back to BUILD.gn resolution).
    """
    build_dir = sdk_dir / "scripts" / "build"
    if not (build_dir / "builders" / "host.py").exists():
        return None
    if str(build_dir) not in sys.path:
        sys.path.insert(0, str(build_dir))
    try:
        from builders.host import HostApp
        return HostApp
    except Exception as e:  # noqa: BLE001 — any import failure → fallback
        print(f"[WARN] Could not import HostApp from SDK ({e}); "
              f"falling back to BUILD.gn resolution.", file=sys.stderr)
        return None


def _first_binary(output_names) -> str | None:
    """First real executable from HostApp.OutputNames() (skip .map + dirs)."""
    for n in output_names:
        if n.endswith(".map") or "/" in n:
            continue
        return n
    return None


def resolve_pipeline_apps(sdk_dir, config: dict) -> list[dict]:
    """
    Return the list of reference apps the pipeline should build, resolved
    dynamically from the SDK's own HostApp mapping. This is the single source
    of truth shared by the build container (--emit-apps-json), upload_artifacts.py
    and run_tests.py. Each returned dict matches the schema those consumers expect:
        {name, enabled, source_dir, build_dir, binary_name, extra_gn_args}

    Config model — discovery.apps: an explicit list enumerating every app with
    a per-app enable flag + Test-Harness build modifiers:
        apps:
          - {name: all-clusters, enabled: true,  modifiers: [ipv6only]}
          - {name: fabric-bridge, enabled: false, modifiers: [rpc, ipv6only]}
    Only enabled entries build; extra_gn_args comes from modifiers (see
    MODIFIER_GN_ARGS) so builds match the Matter Test Harness.

    chip-tool is always excluded here — it is built by its own pipeline step.
    """
    sdk_dir = Path(sdk_dir)
    disc = (config or {}).get("discovery", {}) or {}

    apps_cfg = disc.get("apps")
    if not isinstance(apps_cfg, list):
        print(f"[WARN] discovery.apps is not a list (type="
              f"{type(apps_cfg).__name__}) — nothing to build. Add a discovery."
              f"apps: list to build_config.yaml.", file=sys.stderr)
        return []

    targets = fetch_targets(sdk_dir, quiet=True)
    app_parts = extract_app_parts(targets)
    HostApp = import_hostapp(sdk_dir)

    # Selection: enabled app name -> extra_gn_args (from its modifiers).
    wanted: dict[str, str] = {}
    for entry in apps_cfg:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name or name in SEPARATELY_BUILT or not entry.get("enabled", False):
            continue
        mods = entry.get("modifiers")
        if not isinstance(mods, list) or not mods:
            mods = list(DEFAULT_MODIFIERS)
        wanted[name] = modifiers_to_gn_args(mods)

    resolved = []
    for part in app_parts:
        name, enum_name = part["name"], part["hostapp"]
        if name not in wanted:
            continue

        source_dir, binary_name, _is_ref = _resolve_source_binary(
            name, enum_name, HostApp, sdk_dir)
        if not source_dir or not binary_name:
            print(f"[WARN] Skipping '{name}': could not resolve source/binary "
                  f"from SDK.", file=sys.stderr)
            continue

        # Guard against emitting a target whose source dir isn't in this
        # checkout (would otherwise fail the build with a wrong-path error).
        if not (sdk_dir / source_dir).exists():
            print(f"[WARN] Skipping '{name}': source dir not found in checkout "
                  f"({source_dir})", file=sys.stderr)
            continue

        resolved.append({
            "name": name,
            "enabled": True,
            "source_dir": source_dir,
            "build_dir": f"out/{name}",
            "binary_name": binary_name,
            "extra_gn_args": wanted[name],
        })

    resolved.sort(key=lambda a: a["name"])

    # One-line diagnostic summary (stderr → CI log via the build container) — makes a
    # 0-app result self-explanatory instead of silent.
    print(f"[INFO] discovery summary: enabled-in-config={len(wanted)}, "
          f"SDK-app-parts={len(app_parts)}, "
          f"HostApp={'imported' if HostApp else 'FALLBACK'}, "
          f"resolved={len(resolved)}", file=sys.stderr)

    # Warn about enabled names that couldn't be resolved.
    missing = sorted(set(wanted) - {a["name"] for a in resolved} - SEPARATELY_BUILT)
    if missing:
        print(f"[WARN] {len(missing)} enabled app(s) could not be resolved and "
              f"will NOT be built: {', '.join(missing)}", file=sys.stderr)

    # If nothing resolved, dump enough context to diagnose from the log alone.
    if not resolved:
        print(f"[WARN] 0 apps resolved from {len(apps_cfg)} discovery.apps "
              f"entries ({len(wanted)} enabled).", file=sys.stderr)
        sample = sorted(p["name"] for p in app_parts)
        print(f"[WARN] SDK exposed {len(app_parts)} host-app shorthand(s): "
              f"{sample if len(sample) <= 40 else sample[:40] + ['...']}",
              file=sys.stderr)

    return resolved


def load_config(config_path: Path) -> dict:
    """Load build_config.yaml (used by --emit-apps-json)."""
    if yaml is None:
        print("[ERROR] pyyaml not installed — run: "
              "pip3 install pyyaml --break-system-packages", file=sys.stderr)
        sys.exit(1)
    if not config_path.exists():
        print(f"[ERROR] Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


def generate_config_apps(sdk_dir):
    """
    Emit a ready-to-paste discovery.apps: YAML block listing EVERY discoverable
    linux reference app with an enabled flag and its Test-Harness build
    modifiers. Paste under `discovery:` in build_config.yaml and flip the
    enabled flags. Regenerate when the SDK adds/removes apps.
    """
    sdk_dir = Path(sdk_dir)
    targets = fetch_targets(sdk_dir, quiet=True)
    app_parts = extract_app_parts(targets)
    HostApp = import_hostapp(sdk_dir)

    rows = []
    for part in app_parts:
        name, enum_name = part["name"], part["hostapp"]
        if not name or name in SEPARATELY_BUILT or enum_name in NON_APP_ENUMS:
            continue
        source_dir, binary_name, is_ref = _resolve_source_binary(
            name, enum_name, HostApp, sdk_dir)
        if not source_dir or not binary_name:
            continue
        # Real example subfolder only (NON_APP_ENUMS already drops '../' meta paths).
        if not (sdk_dir / source_dir).exists():
            continue
        mods = CERT_BINS_MODIFIERS.get(name, list(DEFAULT_MODIFIERS))
        rows.append((name, name in DEFAULT_ENABLED, mods, binary_name))

    rows.sort(key=lambda r: (not r[1], r[0]))   # enabled first, then alpha
    name_w = max((len(r[0]) for r in rows), default=4)

    print("discovery:")
    print("  # Full reference-app menu — flip enabled: true/false per app.")
    print("  # modifiers mirror the Matter Test Harness (chip-cert-bins) targets.")
    print("  apps:")
    for name, enabled, mods, binary in rows:
        modstr = "[" + ", ".join(mods) + "]"
        pad = " " * (name_w - len(name))
        en = "true " if enabled else "false"
        print(f"    - {{ name: {name},{pad} enabled: {en}, "
              f"modifiers: {modstr} }}"
              f"{' ' * max(1, 22 - len(modstr))}# {binary}")
    print(f"[INFO] Emitted {len(rows)} reference app(s) "
          f"({sum(1 for r in rows if r[1])} enabled).", file=sys.stderr)


# =============================================================================
# Main
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-dir", required=True,
                        help="Path to connectedhomeip SDK checkout")
    parser.add_argument("--emit-apps-json", action="store_true",
                        help="Emit the resolved pipeline apps list as JSON to "
                             "stdout (machine-readable — consumed by the build). "
                             "Requires --config. All diagnostics go to stderr.")
    parser.add_argument("--emit-config-apps", action="store_true",
                        help="Print a ready-to-paste discovery.apps: YAML block "
                             "listing every reference app with enabled flags + "
                             "Test-Harness modifiers.")
    parser.add_argument("--config", metavar="CONFIG_PATH",
                        help="build_config.yaml to read discovery.apps "
                             "(enabled flags + modifiers) from (for --emit-apps-json)")
    args = parser.parse_args()

    sdk_dir = Path(args.sdk_dir).expanduser().resolve()

    # --emit-apps-json short-circuits: stdout must carry ONLY the JSON array.
    if args.emit_apps_json:
        if not args.config:
            print("[ERROR] --emit-apps-json requires --config <build_config.yaml>",
                  file=sys.stderr)
            sys.exit(1)
        cfg = load_config(Path(args.config).expanduser().resolve())
        apps = resolve_pipeline_apps(sdk_dir, cfg)
        print(json.dumps(apps, indent=2))
        print(f"[DONE] Emitted {len(apps)} resolved app(s).", file=sys.stderr)
        return

    if args.emit_config_apps:
        generate_config_apps(sdk_dir)
        return

    parser.error("choose a mode: --emit-apps-json (with --config) or --emit-config-apps")


if __name__ == "__main__":
    main()
