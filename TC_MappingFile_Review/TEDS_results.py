import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict
from sve_html_report import generate_html_report

# === CONFIGURATION ===
SERVICE_ACCOUNT_FILE = "credentials.json"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1kRAZQ8JJmbD6b0w3Mw9SUwXF2RPZO_i_NBq2HxGUI30/edit?gid=0#gid=0"
SOURCE_SHEET_NAME = "1.6_SVE_Results"
MASTER_TC_SHEET = "SVE_TC_List"
SUMMARY_SHEET_NAME = "Summary"
DELTA_SHEET_NAME = "Summary Changes"

# === HTML SUMMARY CONFIGURATION
REPORT_TITLE    = "SVE Results Summary"
REPORT_SUBTITLE = "Matter 1.6"
REPORT_FILENAME = "sve_summary_report_29-04-2026.html"

# Column indices (1-based)
COL_COMPANY_ID = 3
COL_DUT_ID = 4
COL_MATTER_CASE = 8
COL_TEST_RESULT = 9

# Section headers
SECTION_NOT_EXECUTED = "---- Not Executed Yet----"
SECTION_LOW_PASS = "---- Pass Count < Required ----"
SECTION_PASSED = "---- Passed Rule of Three ----"

# === GOOGLE SHEETS AUTH ===
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

try:
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_url(SHEET_URL)
except Exception as auth_error:
    print(f"❌ Authentication failed: {auth_error}")
    exit(1)


# === HELPER FUNCTIONS ===
def col_to_letter(col_num):
    letter = ""
    while col_num > 0:
        col_num -= 1
        letter = chr(col_num % 26 + 65) + letter
        col_num //= 26
    return letter


def clear_backgrounds_except_header(sheet):
    try:
        sheet_data = sheet.get_all_values()
        if len(sheet_data) > 1:
            last_row = len(sheet_data)
            last_col = len(sheet_data[0])
            sheet.format(f"A2:{col_to_letter(last_col)}{last_row}", {
                "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                "textFormat": {"bold": False},
            })
            sheet.format(f"A2:A{last_row}", {"horizontalAlignment": "LEFT"})
            if last_col > 1:
                sheet.format(f"B2:{col_to_letter(last_col)}{last_row}",
                             {"horizontalAlignment": "RIGHT"})
    except Exception as e:
        print(f"⚠️ Warning: Could not clear backgrounds: {e}")


def apply_font_to_sheet(sheet, spreadsheet, font_family="Times New Roman"):
    """Apply font to the entire sheet in one batch call."""
    try:
        sheet_id = sheet._properties['sheetId']
        spreadsheet.batch_update({
            "requests": [{
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {
                                "fontFamily": font_family
                            }
                        }
                    },
                    "fields": "userEnteredFormat.textFormat.fontFamily"
                }
            }]
        })
        print(f"✅ Applied '{font_family}' font to entire sheet.")
    except Exception as e:
        print(f"⚠️ Warning: Could not apply font: {e}")


def apply_purple_for_sections(sheet, spreadsheet):
    try:
        data = sheet.get_all_values()
        sheet_id = sheet._properties['sheetId']
        requests = []

        for i, row in enumerate(data, start=0):  # 0-based for API
            if row and row[0].strip() in [SECTION_NOT_EXECUTED, SECTION_LOW_PASS, SECTION_PASSED]:
                requests.append({
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": i,
                            "endRowIndex": i + 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 12  # Extended to cover new Comments column
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.85, "green": 0.75, "blue": 0.95},
                                "textFormat": {"bold": True},
                                "horizontalAlignment": "CENTER"
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold,userEnteredFormat.horizontalAlignment"
                    }
                })

        if requests:
            spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"⚠️ Warning: Could not apply purple highlighting: {e}")


def apply_certification_colors(sheet, spreadsheet):
    """Apply green/orange/yellow to Certification Status column - single batch API call."""
    try:
        data = sheet.get_all_values()
        if not data:
            return
        header = data[0]
        try:
            cert_col_idx = header.index("Certification Status")
        except ValueError:
            print("⚠️ Warning: 'Certification Status' column not found for coloring.")
            return

        sheet_id = sheet._properties['sheetId']
        requests = []

        for i, row in enumerate(data[1:], start=1):
            if len(row) <= cert_col_idx:
                continue
            status = row[cert_col_idx].strip()

            if status == "Certifiable":
                color = {"red": 0.7, "green": 0.93, "blue": 0.7}
            elif status == "Provisional":
                color = {"red": 1.0, "green": 0.85, "blue": 0.6}
            elif status == "New Changes - Provisional":
                color = {"red": 1.0, "green": 1.0, "blue": 0.6}
            else:
                continue

            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": cert_col_idx,
                        "endColumnIndex": cert_col_idx + 1
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                            "horizontalAlignment": "CENTER"
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.horizontalAlignment"
                }
            })

        if requests:
            spreadsheet.batch_update({"requests": requests})
            print(f"✅ Applied certification colors to {len(requests)} rows in one batch.")

    except Exception as e:
        print(f"⚠️ Warning: Could not apply certification colors: {e}")


def get_certification_status(effective_pass_count, adjusted_runs_required):
    """
    Determine certification status based on effective pass count and adjusted runs required.
    - effective_pass_count  = actual passes + TH run count
    - adjusted_runs_required = runs_required - previous SVE runs (min 1)

    - adjusted_runs_required == 1: need >= 1 effective pass for Certifiable,
                                   else "New Changes - Provisional"
    - Other values: need >= adjusted_runs_required effective passes for Certifiable,
                    else "Provisional"
    """
    try:
        runs = int(adjusted_runs_required)
    except (ValueError, TypeError):
        runs = 3

    runs = max(runs, 1)  # Safety: never let adjusted runs go below 1

    if runs == 1:
        return "Certifiable" if effective_pass_count >= 1 else "New Changes - Provisional"
    else:
        return "Certifiable" if effective_pass_count >= runs else "Provisional"


def read_summary_data(sheet):
    try:
        data = sheet.get_all_values()
        summary = {}
        if not data:
            return summary
        header = data[0]
        try:
            pass_idx = header.index("Pass Count")
            fail_idx = header.index("Fail Count")
            nt_idx = header.index("Not Tested Count")
            total_idx = header.index("Total")
        except ValueError:
            pass_idx, fail_idx, nt_idx, total_idx = 1, 2, 3, 4

        for row in data[1:]:
            if len(row) > max(pass_idx, fail_idx, nt_idx, total_idx) and row[0]:
                try:
                    if row[pass_idx].isdigit():
                        summary[row[0]] = {
                            "Pass": int(row[pass_idx]),
                            "Fail": int(row[fail_idx]),
                            "NotTested": int(row[nt_idx]),
                            "Total": int(row[total_idx])
                        }
                except (ValueError, IndexError):
                    continue
        return summary
    except Exception as e:
        print(f"⚠️ Warning: Could not read summary data: {e}")
        return {}


def compare_deltas(old, new, filtered_cases):
    deltas = [["Test Case Name", "Old Pass", "New Pass", "Old Fail", "New Fail",
                "Old Not Tested", "New Not Tested", "Old Total", "New Total", "Status"]]
    for tc in filtered_cases:
        o = old.get(tc, {"Total": 0, "Pass": 0, "Fail": 0, "NotTested": 0})
        n = new.get(tc, {"Total": 0, "Pass": 0, "Fail": 0, "NotTested": 0})
        if o != n:
            pass_up   = n["Pass"] > o["Pass"]
            pass_down = n["Pass"] < o["Pass"]
            fail_up   = n["Fail"] > o["Fail"]
            fail_down = n["Fail"] < o["Fail"]
            nt_down   = n["NotTested"] < o["NotTested"]  # fewer Not Tested = more runs done = good
            nt_up     = n["NotTested"] > o["NotTested"]  # more Not Tested = bad

            if pass_up and fail_down:
                status = "Improved"        # mixed but positive — pass up, fail down
            elif pass_down and fail_up:
                status = "Regressed"       # mixed but negative — pass down, fail up
            elif pass_up or nt_down:
                status = "Updated"         # only genuinely positive changes
            elif pass_down or fail_down:
                status = "Reduced"
            elif fail_up or nt_up:
                status = "Changed"         # more Fail or more Not Tested = Changed, not Updated
            else:
                status = "Changed"

            deltas.append([
                tc, o["Pass"], n["Pass"],
                o["Fail"], n["Fail"],
                o["NotTested"], n["NotTested"],
                o["Total"], n["Total"], status
            ])
    return deltas


def apply_delta_colors(sheet, spreadsheet):
    try:
        data = sheet.get_all_values()
        sheet_id = sheet._properties['sheetId']
        requests = []

        for i, row in enumerate(data[1:], start=1):  # 0-based for API
            if len(row) < 10:
                continue
            status = row[9].strip()
            if status == "Updated":
                color = {"red": 0.8, "green": 1.0, "blue": 0.8}    # green
            elif status == "Improved":
                color = {"red": 0.7, "green": 1.0, "blue": 0.85}   # teal-green
            elif status == "Regressed":
                color = {"red": 1.0, "green": 0.7, "blue": 0.7}    # red
            elif status == "Reduced":
                color = {"red": 1.0, "green": 1.0, "blue": 0.6}    # yellow
            elif status == "Changed":
                color = {"red": 0.9, "green": 0.9, "blue": 0.9}    # grey
            else:
                continue

            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 10
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor"
                }
            })

        if requests:
            spreadsheet.batch_update({"requests": requests})
    except Exception as e:
        print(f"⚠️ Warning: Could not apply delta colors: {e}")

def apply_delta_cell_colors(sheet, spreadsheet):
    """Highlight individual cells where old vs new value changed."""
    try:
        data = sheet.get_all_values()
        if not data:
            return
        header = data[0]
        sheet_id = sheet._properties['sheetId']
        requests = []

        # Column indices (0-based) for the pairs to compare
        # Header: Test Case Name | Old Pass | New Pass | Old Fail | New Fail |
        #         Old Not Tested | New Not Tested | Old Total | New Total | Status
        pairs = [
            (1, 2),   # Old Pass vs New Pass
            (3, 4),   # Old Fail vs New Fail
            (5, 6),   # Old Not Tested vs New Not Tested
            (7, 8),   # Old Total vs New Total
        ]
        highlight_color = {"red": 0.7, "green": 0.85, "blue": 1.0}  # light blue

        for i, row in enumerate(data[1:], start=1):
            if len(row) < 10:
                continue
            if row[9].strip() in ["", "No changes detected across all test cases.",
                                   "First run — no previous summary data to compare against."]:
                continue

            for old_idx, new_idx in pairs:
                try:
                    old_val = int(row[old_idx]) if row[old_idx].strip() else 0
                    new_val = int(row[new_idx]) if row[new_idx].strip() else 0
                except ValueError:
                    continue

                if old_val != new_val:
                    # Highlight the New value cell
                    requests.append({
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": i,
                                "endRowIndex": i + 1,
                                "startColumnIndex": new_idx,
                                "endColumnIndex": new_idx + 1
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": highlight_color,
                                    "textFormat": {"bold": True}
                                }
                            },
                            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold"
                        }
                    })

        if requests:
            spreadsheet.batch_update({"requests": requests})
            print(f"✅ Applied delta cell highlights to {len(requests)} changed cells.")

    except Exception as e:
        print(f"⚠️ Warning: Could not apply delta cell colors: {e}")

def apply_pass_count_colors(sheet, spreadsheet):
    """Color Pass Count column based on actual passes vs adjusted runs required."""
    try:
        data = sheet.get_all_values()
        if not data:
            return
        header = data[0]
        try:
            pass_idx = header.index("Pass Count")
            runs_idx = header.index("Number of runs required")
        except ValueError:
            print("⚠️ Warning: Required columns not found for pass count coloring.")
            return

        sheet_id = sheet._properties['sheetId']
        requests = []

        for i, row in enumerate(data[1:], start=1):
            if len(row) <= max(pass_idx, runs_idx):
                continue
            if row[0].strip() in [SECTION_NOT_EXECUTED, SECTION_LOW_PASS, SECTION_PASSED, ""]:
                continue

            try:
                pass_count = int(row[pass_idx])
                runs_required = int(row[runs_idx]) if row[runs_idx].isdigit() else 3
            except (ValueError, IndexError):
                continue

            if pass_count == 0:
                color = {"red": 1.0, "green": 0.8, "blue": 0.8}
            elif pass_count < runs_required:
                color = {"red": 1.0, "green": 1.0, "blue": 0.6}
            else:
                color = {"red": 0.7, "green": 0.93, "blue": 0.7}

            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": pass_idx,
                        "endColumnIndex": pass_idx + 1
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor"
                }
            })

        if requests:
            spreadsheet.batch_update({"requests": requests})
            print(f"✅ Applied pass count colors to {len(requests)} rows in one batch.")

    except Exception as e:
        print(f"⚠️ Warning: Could not apply pass count colors: {e}")
        
def apply_final_runs_colors(sheet, spreadsheet):
    """Color Final # runs required column with a gradient based on remaining runs needed."""
    try:
        data = sheet.get_all_values()
        if not data:
            return
        header = data[0]
        try:
            col_idx = header.index("Final # runs required")
        except ValueError:
            print("⚠️ Warning: 'Final # runs required' column not found for coloring.")
            return

        sheet_id = sheet._properties['sheetId']
        requests = []

        color_map = {
            0: {"red": 0.7,  "green": 0.93, "blue": 0.7},   # strong green  — done
            1: {"red": 0.85, "green": 0.97, "blue": 0.85},  # light green   — 1 more needed
            2: {"red": 0.94, "green": 0.97, "blue": 0.75},  # yellow-green  — 2 more needed
            3: {"red": 1.0,  "green": 1.0,  "blue": 0.6},   # light yellow  — 3 more needed
        }
        color_high = {"red": 1.0, "green": 0.85, "blue": 0.6}  # light orange — 4+ more needed

        for i, row in enumerate(data[1:], start=1):
            if len(row) <= col_idx:
                continue
            if row[0].strip() in [SECTION_NOT_EXECUTED, SECTION_LOW_PASS, SECTION_PASSED, ""]:
                continue
            cell_val = row[col_idx].strip()
            if not cell_val.lstrip("-").isdigit():
                continue

            val = int(cell_val)
            color = color_map.get(val, color_high)

            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": i,
                        "endRowIndex": i + 1,
                        "startColumnIndex": col_idx,
                        "endColumnIndex": col_idx + 1
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                            "horizontalAlignment": "CENTER"
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.horizontalAlignment"
                }
            })

        if requests:
            spreadsheet.batch_update({"requests": requests})
            print(f"✅ Applied final runs required colors to {len(requests)} rows in one batch.")

    except Exception as e:
        print(f"⚠️ Warning: Could not apply final runs required colors: {e}")


def safe_int(value, default=0):
    """Safely convert a string to int, returning default if blank or invalid."""
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


# === MAIN EXECUTION ===
try:
    # Initialize Sheets
    try:
        summary_ws = spreadsheet.worksheet(SUMMARY_SHEET_NAME)
        old_summary_data = read_summary_data(summary_ws)
        clear_backgrounds_except_header(summary_ws)
    except gspread.exceptions.WorksheetNotFound:
        summary_ws = spreadsheet.add_worksheet(title=SUMMARY_SHEET_NAME, rows=2000, cols=10)
        old_summary_data = {}

    try:
        delta_ws = spreadsheet.worksheet(DELTA_SHEET_NAME)
        clear_backgrounds_except_header(delta_ws)
    except gspread.exceptions.WorksheetNotFound:
        delta_ws = spreadsheet.add_worksheet(title=DELTA_SHEET_NAME, rows=2000, cols=10)

    # ── Read test data from MASTER_TC_SHEET ──────────────────────────────────
    # Expected columns in SVE_TC_List:
    #   A: TC ID
    #   B: Number of runs required
    #   C: Can TH run be counted?          (0/1/blank; blank = 0)
    #   D: Number of runs in previous SVE  (integer, 0+; blank = 0)
    #   E: New/Legacy                      (text value passed through as-is)
    # ─────────────────────────────────────────────────────────────────────────
    tc_list_ws = spreadsheet.worksheet(MASTER_TC_SHEET)
    tc_raw = tc_list_ws.get_all_values()

    all_test_cases = []
    runs_required_map = {}   # { tcid: runs_required (str) }
    th_run_map = {}          # { tcid: 0 or 1 }          — "Can TH run be counted?"
    prev_sve_map = {}        # { tcid: int }              — "Number of runs in previous SVE"
    new_legacy_map = {}      # { tcid: str }              — "New/Legacy"

    for row in tc_raw[1:]:  # Skip header
        if not row:
            continue
        tcid       = row[0].strip() if len(row) > 0 else ""
        runs_req   = row[1].strip() if len(row) > 1 else "3"
        th_run     = row[2].strip() if len(row) > 2 else "0"
        prev_sve   = row[3].strip() if len(row) > 3 else "0"
        new_legacy = row[4].strip() if len(row) > 4 else ""

        if not tcid:
            continue

        all_test_cases.append(tcid)
        runs_required_map[tcid] = runs_req if runs_req else "3"
        th_run_map[tcid]        = 1 if safe_int(th_run, default=0) >= 1 else 0
        prev_sve_map[tcid]      = safe_int(prev_sve, default=0)
        new_legacy_map[tcid]    = new_legacy

    print(f"📋 Loaded {len(all_test_cases)} test cases from master list.")

    results_ws = spreadsheet.worksheet(SOURCE_SHEET_NAME)
    rows = results_ws.get_all_values()[1:]  # Skip header

    # Process unique results
    unique_results = set()
    filtered_rows = []
    duplicates_removed = 0
    duplicate_details = []

    for row in rows:
        if len(row) < max(COL_MATTER_CASE, COL_TEST_RESULT):
            continue
        company = row[COL_COMPANY_ID - 1].strip()
        dut     = row[COL_DUT_ID - 1].strip()
        tcid    = row[COL_MATTER_CASE - 1].strip()
        result  = row[COL_TEST_RESULT - 1].strip().capitalize()

        if not (company and dut and tcid):
            continue

        if result not in ["Pass", "Fail", "Not tested", "Not Tested"]:
            continue

        key = (company, dut, tcid, result)
        if key not in unique_results:
            unique_results.add(key)
            filtered_rows.append((company, dut, tcid, result))
        else:
            duplicates_removed += 1
            duplicate_details.append((company, dut, tcid, result))

    print(f"🧹 Removed {duplicates_removed} duplicate results (same company/DUT/test/result).")
    if duplicate_details:
        print("   Duplicate entries removed:")
        for company, dut, tcid, result in duplicate_details:
            print(f"   ⚠️  [{result}] {tcid} | Company: {company} | DUT: {dut}")
    print()

    # Dedup logic per company
    company_tc_results = defaultdict(lambda: {"Pass": set(), "Fail": set(), "NotTested": set()})
    for company, dut, tcid, result in filtered_rows:
        if result == "Pass":
            company_tc_results[(company, tcid)]["Pass"].add(dut)
        elif result == "Fail":
            company_tc_results[(company, tcid)]["Fail"].add(dut)
        elif result.lower().startswith("not"):
            company_tc_results[(company, tcid)]["NotTested"].add(dut)

    # Summarize results per test case
    summary_data = defaultdict(lambda: {"Pass": 0, "Fail": 0, "NotTested": 0})
    for (company, tcid), results in company_tc_results.items():
        passes    = len(results["Pass"]) > 0
        fails     = len(results["Fail"]) > 0
        not_tested = len(results["NotTested"]) > 0

        if passes and fails:
            summary_data[tcid]["Pass"] += 1
            summary_data[tcid]["Fail"] += 1
        elif passes:
            summary_data[tcid]["Pass"] += 1
        elif fails:
            summary_data[tcid]["Fail"] += 1
        if not_tested:
            summary_data[tcid]["NotTested"] += 1

    # ── Build final summary ───────────────────────────────────────────────────
    # Columns:
    #   Test Case Name | Pass Count | TH Run Count | Fail Count | Not Tested Count | Total |
    #   Total Pass+Fail | Number of runs required | Final # runs required | Certification Status | New/Legacy | Comments
    # ─────────────────────────────────────────────────────────────────────────
    output_data = [[
        "Test Case Name", "Pass Count", "Can TH run be counted?", "Fail Count", "Not Tested Count",
        "Total", "Total Pass+Fail", "Number of runs required",
        "Final # runs required", "Certification Status", "New/Legacy", "Comments"
    ]]

    never_executed, low_pass, others = [], [], []

    for tc in all_test_cases:
        counts    = summary_data.get(tc, {"Pass": 0, "Fail": 0, "NotTested": 0})
        total_pf  = counts["Pass"] + counts["Fail"]
        total_all = counts["Pass"] + counts["Fail"] + counts["NotTested"]

        # Raw values from master list
        runs_req  = safe_int(runs_required_map.get(tc, "3"), default=3)
        th_run    = th_run_map.get(tc, 0)    # extra passes from TH run
        prev_sve  = prev_sve_map.get(tc, 0)  # runs carried from previous SVE

        # Derived values
        adjusted_runs_req  = max(1, runs_req - prev_sve)      # reduce runs needed
        effective_pass     = counts["Pass"] + th_run          # boost actual passes
        final_runs_req     = max(0, adjusted_runs_req - effective_pass)  # remaining runs still needed

        cert_status = get_certification_status(effective_pass, adjusted_runs_req)

        # Comments column
        if prev_sve > 0:
            comment = f"Had {prev_sve} run{'s' if prev_sve > 1 else ''} in Previous SVE"
        else:
            comment = ""

        row = [
            tc,
            counts["Pass"],
            th_run,              # TH Run Count — shown separately
            counts["Fail"],
            counts["NotTested"],
            total_all,
            total_pf,
            adjusted_runs_req,   # Show the adjusted (reduced) runs required
            final_runs_req,      # Final # runs required = adjusted_runs_req - (Pass Count + TH Run)
            cert_status,
            new_legacy_map.get(tc, ""),  # New/Legacy
            comment
        ]

        # Categorise rows into sections
        if counts["Pass"] == 0 and counts["Fail"] == 0:
            never_executed.append(row)
        elif effective_pass < adjusted_runs_req:
            low_pass.append(row)
        else:
            others.append(row)

    output_data += [[SECTION_NOT_EXECUTED]]
    output_data += never_executed or [["All test cases executed at least once"]]
    output_data += [[""], [SECTION_LOW_PASS]]
    output_data += low_pass or [["No test cases below required pass count"]]
    output_data += [[""], [SECTION_PASSED]]
    output_data += others or [["No remaining test cases"]]

    # Update summary sheet
    summary_ws.clear()
    summary_ws.update(range_name="A1", values=output_data)
    apply_font_to_sheet(summary_ws, spreadsheet)
    apply_purple_for_sections(summary_ws, spreadsheet)
    apply_certification_colors(summary_ws, spreadsheet)
    apply_pass_count_colors(summary_ws, spreadsheet)
    apply_final_runs_colors(summary_ws, spreadsheet)

    # Center align New/Legacy and Comments columns
    try:
        header = output_data[0]
        for col_name in ["New/Legacy", "Comments"]:
            if col_name in header:
                col_letter = col_to_letter(header.index(col_name) + 1)
                last_row = len(output_data)
                summary_ws.format(f"{col_letter}2:{col_letter}{last_row}",
                                  {"horizontalAlignment": "CENTER"})
        print("✅ Center aligned New/Legacy and Comments columns.")
    except Exception as e:
        print(f"⚠️ Warning: Could not center align columns: {e}")

    print("✅ Certification Status and Comments columns populated successfully.")

    # Delta comparison — track ALL test cases so movements into Passed section are also captured
    new_summary_data = read_summary_data(summary_ws)
    filtered_cases   = all_test_cases
    delta_data = compare_deltas(old_summary_data, new_summary_data, filtered_cases)

    delta_ws.clear()
    if not old_summary_data:
        # Fix 4: First run — no previous summary existed to compare against
        delta_ws.update(range_name="A1",
                        values=[["First run — no previous summary data to compare against."]])
    elif len(delta_data) > 1:
        delta_ws.update(range_name="A1", values=delta_data)
        apply_delta_colors(delta_ws, spreadsheet)
        apply_delta_cell_colors(delta_ws, spreadsheet)
    else:
        delta_ws.update(range_name="A1",
                        values=[["No changes detected across all test cases."]])
                        
    generate_html_report(
        output_data=output_data,
        title=REPORT_TITLE,
        subtitle=REPORT_SUBTITLE,
        filename=REPORT_FILENAME
    )

    print("✅ Summary and Delta sheets updated successfully!")

except Exception as e:
    print(f"❌ Unexpected error: {e}")
    raise
