"""
Every rule from the brief lives here as a small, independently readable
function. `validate_tab1_row()` and `cross_check()` wire them together.

Each row-level issue is a dict: {"severity": "error"|"warning", "check": str,
"message": str}. Severity "error" means the sheet is factually inconsistent;
"warning" is used for the couple of rules that are more like style/lint
checks (arg spacing) so they don't drown out real mismatches.
"""

import re
from typing import Dict, List

import config
from sheet_utils import cell, normalize, strip_notes

# ---------------------------------------------------------------------------
# Regex building blocks
# ---------------------------------------------------------------------------

# TC-<CLUSTER>-<digits>(.<digits>)*  e.g. TC-ACE-1.4, TC-WEBRTC-1.10,
# TC-SC-TC-2.1 (the cluster abbreviation itself can contain hyphens - the
# only hard requirement is hyphens/dots throughout, never underscores, and
# a numeric version suffix at the end).
ID_HYPHEN_RE = re.compile(r"^TC(?:-[A-Za-z0-9]+)*-\d+(?:\.\d+)*$")

# The bracketed id at the start of the Test Case Name column, e.g.
# "[TC-ACE-1.4] Targets[DUT-Commissionee]" -> "TC-ACE-1.4"
BRACKET_ID_RE = re.compile(r"^\[([^\]]+)\]")

# Long ("--foo") and short ("-f") style CLI arguments, matched only where a
# dash actually starts a token (start of string or right after whitespace) -
# this is what keeps "-n" in "on-network" or "-t" in "run-tests" from being
# mistaken for a flag.
ARG_TOKEN_RE = re.compile(r"(?:(?<=\s)|^)(--[A-Za-z][\w-]*|-[A-Za-z](?![\w-]))")

# Column D uses a JSON-ish "key": "value" style with no leading dashes at
# all, e.g. "int-arg": "minFrameRate:15" or "endpoint":"1".
D_KEY_RE = re.compile(r'"([A-Za-z][\w-]*)"\s*:')

# When column F's real command uses one of these flags, it's a generic
# shared script (e.g. TC_DeviceConformance.py, TC_DeviceBasicComposition.py)
# that pulls its configuration from elsewhere rather than from literal
# product-specific CLI args - so column D's declared arguments aren't
# expected to appear in F at all for these rows.
SHARED_SCRIPT_MARKERS = {"manual-code"}


# A `-p` / `--pics-config-folder` style flag used as a whole token.
def _flag_present(text: str, names: set) -> bool:
    for match in ARG_TOKEN_RE.finditer(text):
        token = match.group(0).lstrip("-").lower()
        if token in names:
            return True
    return False


def to_underscore_id(hyphen_id: str) -> str:
    """TC-ACE-1.4 -> TC_ACE_1_4"""
    return re.sub(r"[.\-]", "_", hyphen_id)


# ---------------------------------------------------------------------------
# Individual checks (each returns 0 or more issue dicts)
# ---------------------------------------------------------------------------

def check_no_empty_columns(row_cells: Dict[str, str]) -> List[dict]:
    issues = []
    for key, value in row_cells.items():
        if not value:
            issues.append({
                "severity": "error",
                "check": "Empty cell",
                "message": f"Column {key.upper()} is empty.",
            })
    return issues


def check_id_formats_and_consistency(row_cells: Dict[str, str], known_master_ids: set = frozenset()) -> List[dict]:
    issues = []
    name_col, id_col = row_cells["B"], row_cells["C"]

    # The regex catches the common mistake (underscores instead of hyphens),
    # but the master tab is the real source of truth for what a legitimate
    # ID looks like - if this exact ID exists there, don't second-guess it.
    if not ID_HYPHEN_RE.match(id_col) and id_col not in known_master_ids:
        issues.append({
            "severity": "error",
            "check": "Test Case ID format",
            "message": (
                f"Column C ('{id_col}') should look like TC-XXXX-1.1 "
                "(hyphens, not underscores)."
            ),
        })

    bracket_match = BRACKET_ID_RE.match(name_col)
    bracket_id = bracket_match.group(1) if bracket_match else None
    if not bracket_id:
        issues.append({
            "severity": "error",
            "check": "Test Case ID format",
            "message": f"Column B ('{name_col}') should start with '[TC-XXXX-1.1]'.",
        })
    elif bracket_id != id_col:
        issues.append({
            "severity": "error",
            "check": "Test Case ID mismatch",
            "message": f"Column B id '{bracket_id}' does not match Column C id '{id_col}'.",
        })

    return issues


def check_id_in_docker_and_cli_commands(row_cells: Dict[str, str], known_master_ids: set = frozenset()) -> List[dict]:
    issues = []
    id_col = row_cells["C"]
    if not ID_HYPHEN_RE.match(id_col) and id_col not in known_master_ids:
        # Format is already flagged elsewhere; skip derived checks on a bad id.
        return issues

    underscore_id = to_underscore_id(id_col)
    # Anything after a "Note:" is guidance for humans (an alternate command
    # for another platform, a reminder about a special-case argument), not
    # part of the actual command, so it's ignored here and below.
    docker_cmd = strip_notes(row_cells["F"])
    cli_cmd = strip_notes(row_cells["G"])

    if underscore_id.lower() not in docker_cmd.lower():
        issues.append({
            "severity": "error",
            "check": "Test Case ID mismatch",
            "message": (
                f"Column F does not contain '{underscore_id}' anywhere "
                "(the script name or --tests/--tests-list value should include it)."
            ),
        })

    # A hyphenated id used as a literal .py filename is the specific mistake
    # called out in the brief (should be underscores in a filename).
    bad_filename = re.search(r"\bTC-[A-Za-z0-9]+(?:-\d+)+(?:\.\d+)*\.py\b", docker_cmd)
    if bad_filename:
        issues.append({
            "severity": "error",
            "check": "Test Case ID mismatch",
            "message": (
                f"Column F uses '{bad_filename.group(0)}' as a script name - "
                f"file names should use underscores, e.g. '{underscore_id}.py'."
            ),
        })

    if id_col.lower() not in cli_cmd.lower() and underscore_id.lower() not in cli_cmd.lower():
        issues.append({
            "severity": "error",
            "check": "Test Case ID mismatch",
            "message": (
                f"Column G does not contain '{id_col}' or '{underscore_id}'."
            ),
        })

    return issues


def check_arg_spacing(row_cells: Dict[str, str]) -> List[dict]:
    issues = []
    for col in ("F", "G"):
        text = strip_notes(row_cells[col])
        for match in ARG_TOKEN_RE.finditer(text):
            end = match.end()
            if end < len(text) and text[end] not in (" ", "=", "\t"):
                issues.append({
                    "severity": "warning",
                    "check": "Argument spacing",
                    "message": (
                        f"Column {col}: '{match.group(0)}' is not followed by a space "
                        f"(found '...{text[max(0, match.start()-5):end+10]}...')."
                    ),
                })
    return issues


def _arg_names(text: str) -> set:
    """Flag names used CLI-style in columns F/G, e.g. --int-arg -> 'int-arg'."""
    return {m.group(0).lstrip("-").lower() for m in ARG_TOKEN_RE.finditer(text)}


def _d_key_names(text: str) -> set:
    """Bare key names used JSON-style in column D, e.g. "int-arg": ... """
    return {m.group(1).lower() for m in D_KEY_RE.finditer(text)}


def check_args_match_between_d_and_f(row_cells: Dict[str, str]) -> List[dict]:
    issues = []
    d_text = row_cells["D"]

    if normalize(d_text) == normalize(config.NO_ARGS_NOTE):
        return issues  # nothing to cross-check when D explicitly says "no args"

    exempt = {a.lower() for a in config.ARGS_EXEMPT_FROM_D_F_CROSS_CHECK}
    exempt |= {a.lower() for a in config.STANDARD_EXECUTION_ARGS_EXEMPT_FROM_D_F_CROSS_CHECK}

    d_args = _d_key_names(d_text) - exempt
    f_primary_text = strip_notes(row_cells["F"])

    # A generic shared script (identified by --manual-code) doesn't need
    # column D's declared args to show up in F at all - it configures itself
    # some other way, so the "D but not in F" direction doesn't apply here.
    # (This must NOT touch d_args itself - it's still needed below to check
    # the other direction, i.e. whether F's real args are declared in D.)
    skip_d_not_in_f_check = _flag_present(f_primary_text, SHARED_SCRIPT_MARKERS)

    # An arg D declares only needs to be accounted for SOMEWHERE in F - even
    # inside a "Note:"/"Example:" aside about when to add it - so this
    # direction searches the full, untruncated column F text.
    f_args_anywhere = _arg_names(row_cells["F"]) - exempt
    # But an arg F's actual command line uses has to be genuinely declared
    # in D - a mention inside F's own Note/Example doesn't count, so this
    # direction only looks at the real command (Note-stripped).
    f_args_primary = _arg_names(f_primary_text) - exempt

    if not skip_d_not_in_f_check:
        for missing in sorted(d_args - f_args_anywhere):
            issues.append({
                "severity": "error",
                "check": "Argument mismatch (D vs F)",
                "message": f"'--{missing}' is in Column D but not in Column F.",
            })
    for missing in sorted(f_args_primary - d_args):
        issues.append({
            "severity": "error",
            "check": "Argument mismatch (D vs F)",
            "message": f"'--{missing}' is in Column F but not in Column D.",
        })
    return issues


def check_pics_flag_in_g(row_cells: Dict[str, str]) -> List[dict]:
    issues = []
    if _flag_present(strip_notes(row_cells["F"]), {"pics"}):
        if not _flag_present(strip_notes(row_cells["G"]), set(config.PICS_FLAG_ALIASES_IN_G)):
            aliases = " or ".join(f"--{a}" if len(a) > 1 else f"-{a}" for a in config.PICS_FLAG_ALIASES_IN_G)
            issues.append({
                "severity": "error",
                "check": "Missing PICS flag in CLI command",
                "message": f"Column F uses --PICS but Column G has no {aliases}.",
            })
    return issues


def check_default_config_flag_in_g(row_cells: Dict[str, str]) -> List[dict]:
    issues = []
    d_text = row_cells["D"]
    if normalize(d_text) == normalize(config.NO_ARGS_NOTE):
        return issues

    pattern = re.compile(
        r"(--config|-c)\s+\S+\.json",
        re.IGNORECASE,
    )
    if not pattern.search(strip_notes(row_cells["G"])):
        issues.append({
            "severity": "error",
            "check": "Missing config flag in CLI command",
            "message": (
                "Column D has real arguments, so Column G should include "
                "'-c <file>.json' or '--config <file>.json'."
            ),
        })
    return issues


def check_arg_spacing_in_d(row_cells: Dict[str, str]) -> List[dict]:
    """Column D uses a different, quoted-JSON-ish style; only checked lightly
    (real duplicate-space-style issues there are still worth surfacing)."""
    return []  # Brief scopes the spacing rule to F/G only; kept as a hook.


ROW_CHECKS_NEEDING_MASTER_IDS = {
    check_id_formats_and_consistency,
    check_id_in_docker_and_cli_commands,
}

ROW_CHECKS = [
    check_no_empty_columns,
    check_id_formats_and_consistency,
    check_id_in_docker_and_cli_commands,
    check_arg_spacing,
    check_args_match_between_d_and_f,
    check_pics_flag_in_g,
    check_default_config_flag_in_g,
]


def build_row_cells(raw_row: List[str]) -> Dict[str, str]:
    cols = config.TAB1_COLUMNS
    return {
        "A": cell(raw_row, cols["cluster_name"]),
        "B": cell(raw_row, cols["test_case_name"]),
        "C": cell(raw_row, cols["test_case_id"]),
        "D": cell(raw_row, cols["ui_args"]),
        "E": cell(raw_row, cols["commissionable_cmd"]),
        "F": cell(raw_row, cols["docker_python_cmd"]),
        "G": cell(raw_row, cols["cli_cmd"]),
    }


def validate_tab1_row(raw_row: List[str], known_master_ids: set = frozenset()) -> dict:
    row_cells = build_row_cells(raw_row)
    issues: List[dict] = []
    for check_fn in ROW_CHECKS:
        if check_fn in ROW_CHECKS_NEEDING_MASTER_IDS:
            issues.extend(check_fn(row_cells, known_master_ids))
        else:
            issues.extend(check_fn(row_cells))
    return {
        "test_case_id": row_cells["C"] or "(no id)",
        "cluster": row_cells["A"] or "(no cluster)",
        "cells": row_cells,
        "issues": issues,
    }


# ---------------------------------------------------------------------------
# Tab 1 <-> Tab 2 cross-check
# ---------------------------------------------------------------------------

def _tab2_index(tab2_data_rows: List[List[str]]) -> Dict[str, dict]:
    cols = config.TAB2_COLUMNS
    index = {}
    for raw_row in tab2_data_rows:
        tc_id = cell(raw_row, cols["test_case_id"])
        if not tc_id:
            continue
        index[tc_id] = {
            "cluster_a": cell(raw_row, cols["cluster_name_a"]),
            "cluster_b": cell(raw_row, cols["cluster_name_b"]),
            "test_case_name": cell(raw_row, cols["test_case_name"]),
            "execution_type": cell(raw_row, cols["execution_type"]),
        }
    return index


def get_tab2_ids(tab2_data_rows: List[List[str]]) -> set:
    """The set of Test Case IDs that exist in the master tab, used as a
    fallback source of truth for ID-format checks (see check_id_formats_and_consistency)."""
    return set(_tab2_index(tab2_data_rows).keys())


def cross_check(tab1_rows: List[dict], tab2_data_rows: List[List[str]]) -> dict:
    tab2_index = _tab2_index(tab2_data_rows)
    tab1_ids = {r["test_case_id"] for r in tab1_rows if r["test_case_id"] != "(no id)"}

    extra_in_tab1 = []
    cluster_mismatches = []

    for row in tab1_rows:
        tc_id = row["test_case_id"]
        if tc_id == "(no id)":
            continue
        master = tab2_index.get(tc_id)
        if master is None:
            extra_in_tab1.append({
                "test_case_id": tc_id,
                "cluster": row["cluster"],
                "reason": f"'{tc_id}' does not exist in {config.TAB2_NAME}.",
            })
            continue

        cluster_a = normalize(master["cluster_a"])
        cluster_b = normalize(master["cluster_b"])
        tab1_cluster = normalize(row["cluster"])
        if tab1_cluster not in (cluster_a, cluster_b):
            cluster_mismatches.append({
                "test_case_id": tc_id,
                "tab1_cluster": row["cluster"],
                "tab2_cluster_a": master["cluster_a"],
                "tab2_cluster_b": master["cluster_b"],
            })

    missing_python_tests = []
    for tc_id, master in tab2_index.items():
        if config.PYTHON_EXECUTION_MARKER in master["execution_type"].lower():
            if tc_id not in tab1_ids:
                missing_python_tests.append({
                    "test_case_id": tc_id,
                    "cluster": master["cluster_a"] or master["cluster_b"],
                    "test_case_name": master["test_case_name"],
                })

    return {
        "extra_in_tab1": extra_in_tab1,
        "cluster_mismatches": cluster_mismatches,
        "missing_python_tests": missing_python_tests,
    }
