#!/usr/bin/env python3
"""
yaml_pics.py — convert our PICS XML → the flat KEY=1/0 file the YAML runner needs.

The Matter YAML test runner (scripts/tests/chipyaml + matter.yamltests.PICSChecker)
accepts ONLY a flat text file of `PICS.CODE=1` / `=0` lines. Our certification PICS
are authored as per-cluster XML (`<picsItem><itemNumber>CCTRL.S.A0000</itemNumber>
<support>true</support></picsItem>`). PICSChecker rejects `true`/`false` outright, so
we convert at RUNTIME into a throwaway flat file and pass it as `--pics-file`. Our XML
stays the single source of truth; the SDK is never patched.

Usable as a library (`convert(...)`) from run_tests.py, or standalone:
    python3 yaml_pics.py <xml_dir_or_file> -o /tmp/tc_pics.txt
"""
import sys
import argparse
import xml.etree.ElementTree as ET
from pathlib import Path


def _iter_pics_items(xml_file: Path):
    """Yield (item_number, supported_bool) for every <picsItem> in one XML file.

    Tolerant of namespaces and malformed files — a single bad file must never
    sink the whole run (we warn and skip it)."""
    try:
        root = ET.parse(xml_file).getroot()
    except (ET.ParseError, OSError) as e:
        print(f"  [PICS] ⚠️  skipping {xml_file.name}: {e}")
        return

    # Namespace-agnostic: match on the local tag name (strip any '{ns}' prefix).
    def local(tag):
        return tag.rsplit("}", 1)[-1]

    for item in root.iter():
        if local(item.tag) != "picsItem":
            continue
        number = support = None
        for child in item:
            lt = local(child.tag)
            if lt == "itemNumber":
                number = (child.text or "").strip()
            elif lt == "support":
                support = (child.text or "").strip().lower()
        if not number:
            continue
        yield number, (support in ("true", "1", "yes"))


def convert(xml_source, out_path, extra: dict | None = None) -> Path:
    """
    Convert PICS XML (a directory of per-cluster files, or a single file) into a
    flat `CODE=1/0` file at out_path. `extra` injects/overrides literal PICS
    (e.g. {"PICS_SDK_CI_ONLY": 0}) after the XML is read. Returns out_path.

    If the same itemNumber appears more than once, "supported anywhere" wins (1) —
    a PICS file lists capabilities, so a code enabled on any side stays enabled.
    """
    xml_source = Path(xml_source)
    out_path   = Path(out_path)

    if xml_source.is_dir():
        files = sorted(xml_source.rglob("*.xml"))
    elif xml_source.is_file():
        files = [xml_source]
    else:
        raise FileNotFoundError(f"PICS XML source not found: {xml_source}")
    if not files:
        raise FileNotFoundError(f"No .xml PICS files under: {xml_source}")

    pics: dict[str, bool] = {}
    for f in files:
        for number, ok in _iter_pics_items(f):
            pics[number] = pics.get(number, False) or ok   # OR across occurrences

    for k, v in (extra or {}).items():
        pics[str(k)] = bool(int(v)) if str(v) in ("0", "1") else bool(v)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as fh:
        fh.write("# Auto-generated from PICS XML by yaml_pics.py — do not edit.\n")
        fh.write(f"# source: {xml_source}\n")
        for code in sorted(pics):
            fh.write(f"{code}={1 if pics[code] else 0}\n")

    enabled = sum(1 for v in pics.values() if v)
    print(f"  [PICS] {len(pics)} items ({enabled} enabled) → {out_path}")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="Convert PICS XML → flat CODE=1/0 file.")
    ap.add_argument("source", help="PICS XML directory or single .xml file")
    ap.add_argument("-o", "--out", required=True, help="output flat PICS file path")
    args = ap.parse_args()
    try:
        convert(args.source, args.out)
    except (FileNotFoundError, ValueError) as e:
        print(f"[PICS] ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
