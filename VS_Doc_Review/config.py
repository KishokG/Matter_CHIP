"""
All the knobs you're likely to need live in this one file.
Nothing in the other modules should need editing for day-to-day use.
"""

# ---------------------------------------------------------------------------
# Google Sheet connection
# ---------------------------------------------------------------------------

# The long ID in the sheet's URL:
# https://docs.google.com/spreadsheets/d/<THIS PART>/edit
SPREADSHEET_ID = "1AvdavsRfpbPzGAkTxIYpXqBEB-0W6WHcK2wDsrupDew"

# Path to the service-account JSON key file (see README.md for how to create
# one and share the sheet with the service account's email address).
SERVICE_ACCOUNT_FILE = "credentials.json"

# ---------------------------------------------------------------------------
# Tab 1 - the tab with the per-test-case commands (point 1-8 in the brief)
# ---------------------------------------------------------------------------

TAB1_NAME = "Python Script Validation Procedure"

# Row numbers are 1-indexed, exactly like they appear in Google Sheets.
TAB1_HEADER_ROW = 7
TAB1_DATA_START_ROW = 8

# Column letters -> 0-indexed position, exactly as described in the brief.
TAB1_COLUMNS = {
    "cluster_name": "A",
    "test_case_name": "B",
    "test_case_id": "C",
    "ui_args": "D",
    "commissionable_cmd": "E",
    "docker_python_cmd": "F",
    "cli_cmd": "G",
}

# ---------------------------------------------------------------------------
# Tab 2 - the master/cert-repo tab used for cross-checking (point 9-10)
# ---------------------------------------------------------------------------

TAB2_NAME = "All_TCs_Available_In_cert_repo_details"

TAB2_HEADER_ROW = 1
TAB2_DATA_START_ROW = 2

TAB2_COLUMNS = {
    "cluster_name_a": "A",
    "cluster_name_b": "B",
    "test_case_name": "C",
    "test_case_id": "D",
    # Column whose value tells us how the test case is executed
    # (values seen: "UI-Automated", "UI-Python", ...). Any row whose value
    # here contains "python" is expected to also show up in TAB1.
    "execution_type": "F",
}

# Substring (case-insensitive) that marks a Tab-2 row as "should have a
# Python script entry in Tab 1".
PYTHON_EXECUTION_MARKER = "python"

# ---------------------------------------------------------------------------
# Validation rules
# ---------------------------------------------------------------------------

# Exact note that column D is allowed to contain instead of real arguments.
NO_ARGS_NOTE = "No arguments required the test case can be selected directly for execution"

# Argument names that are allowed to appear in column F (or G) without also
# being required in column D. --PICS is the one called out in the brief;
# add more here (lower-case, no leading dashes) if your sheet has other
# "always there" boilerplate arguments you don't want flagged.
ARGS_EXEMPT_FROM_D_F_CROSS_CHECK = {"pics"}

# Column F always carries the standard connection/setup flags needed to run
# the docker command (commissioning method, discriminator, passcode, storage
# path, ...) even though those are never product-specific "arguments for UI
# execution" and so never appear in column D. Left un-exempted, every row
# with any real column-D argument would report these as "missing from D"
# noise. Edit this set to match what your sheet always includes in F/G;
# leave it empty for a fully literal reading of the brief's rule.
STANDARD_EXECUTION_ARGS_EXEMPT_FROM_D_F_CROSS_CHECK = {
    "commissioning-method",
    "discriminator",
    "passcode",
    "storage-path",
    # Several test cases share one generic script (e.g. TC_AccessChecker.py)
    # and column F picks the specific test with --tests/--tests-list; the
    # Test Harness handles this itself, so it's never something column D
    # needs to also list.
    "tests",
    "tests-list",
}

# Accepted spellings for "the PICS folder" flag in column G, checked when
# column F uses --PICS.
PICS_FLAG_ALIASES_IN_G = {"p", "pics-config-folder"}

# Accepted spellings for "use this config file" in column G, checked when
# column D has real arguments (i.e. does not contain NO_ARGS_NOTE).
CONFIG_FLAG_ALIASES_IN_G = {"c", "config"}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

# Every run is written as its own timestamped file in this folder, so you
# keep a full history instead of overwriting the previous run.
REPORTS_DIR = "reports"
REPORT_FILENAME_PREFIX = "validation_report"
REPORT_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"

# Also refresh reports/latest.html with a copy of the newest run, so there's
# always a stable link to "whatever ran most recently" alongside the history.
KEEP_LATEST_COPY = True

REPORT_TEMPLATE_FILE = "report_template.html"
