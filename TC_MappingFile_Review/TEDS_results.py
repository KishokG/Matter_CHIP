import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict

# === CONFIGURATION ===
SERVICE_ACCOUNT_FILE = "credentials.json"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1kRAZQ8JJmbD6b0w3Mw9SUwXF2RPZO_i_NBq2HxGUI30/edit?gid=0#gid=0"
SOURCE_SHEET_NAME = "1.5_SVE_Results"
MASTER_TC_SHEET = "SVE_TC_List"
SUMMARY_SHEET_NAME = "Summary"
DELTA_SHEET_NAME = "Summary Changes"

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
                        # No startRowIndex/endRowIndex = covers entire sheet
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
                            "endColumnIndex": 8
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
                        "startRowIndex": i,       # 0-based
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


def get_certification_status(pass_count, runs_required):
    """
    Determine certification status based on pass count and required runs.
    - runs_required == 3: need >= 3 passes for Certifiable, else Provisional
    - runs_required == 1: need >= 1 pass for Certifiable, else New Changes - Provisional
    - Other values: treat same as runs_required == 3 (safe default)
    """
    try:
        runs = int(runs_required)
    except (ValueError, TypeError):
        runs = 3  # Default to 3 if not parseable

    if runs == 1:
        return "Certifiable" if pass_count >= 1 else "New Changes - Provisional"
    else:
        # Covers runs_required == 3 and any other value
        return "Certifiable" if pass_count >= runs else "Provisional"


def read_summary_data(sheet):
    try:
        data = sheet.get_all_values()
        summary = {}
        if not data:
            return summary
        header = data[0]
        # Find column indices dynamically
        try:
            pass_idx = header.index("Pass Count")
            fail_idx = header.index("Fail Count")
            nt_idx = header.index("Not Tested Count")
            total_idx = header.index("Total")
        except ValueError:
            # Fallback to positional (old format)
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
            if n["Pass"] > o["Pass"] or n["Fail"] > o["Fail"] or n["NotTested"] != o["NotTested"]:
                status = "Updated"
            elif n["Pass"] < o["Pass"] or n["Fail"] < o["Fail"]:
                status = "Reduced"
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
                color = {"red": 0.8, "green": 1.0, "blue": 0.8}
            elif status == "Reduced":
                color = {"red": 1.0, "green": 1.0, "blue": 0.6}
            elif status == "Changed":
                color = {"red": 0.9, "green": 0.9, "blue": 0.9}
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
        
def apply_pass_count_colors(sheet, spreadsheet):
    """Color Column B (Pass Count) - single batch API call."""
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
                        "startRowIndex": i,       # 0-based
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


# === MAIN EXECUTION ===
try:
    # Initialize Sheets
    try:
        summary_ws = spreadsheet.worksheet(SUMMARY_SHEET_NAME)
        old_summary_data = read_summary_data(summary_ws)
        clear_backgrounds_except_header(summary_ws)
    except gspread.exceptions.WorksheetNotFound:
        summary_ws = spreadsheet.add_worksheet(title=SUMMARY_SHEET_NAME, rows=2000, cols=8)
        old_summary_data = {}

    try:
        delta_ws = spreadsheet.worksheet(DELTA_SHEET_NAME)
        clear_backgrounds_except_header(delta_ws)
    except gspread.exceptions.WorksheetNotFound:
        delta_ws = spreadsheet.add_worksheet(title=DELTA_SHEET_NAME, rows=2000, cols=10)

    # Read test data from MASTER_TC_SHEET
    # Column A = TC ID, Column B = Number of runs required
    tc_list_ws = spreadsheet.worksheet(MASTER_TC_SHEET)
    tc_raw = tc_list_ws.get_all_values()

    all_test_cases = []
    runs_required_map = {}  # { tcid: runs_required }

    for row in tc_raw[1:]:  # Skip header
        if not row:
            continue
        tcid = row[0].strip() if len(row) > 0 else ""
        runs_req = row[1].strip() if len(row) > 1 else "3"  # Default to 3 if missing
        if tcid:
            all_test_cases.append(tcid)
            runs_required_map[tcid] = runs_req if runs_req else "3"

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
        dut = row[COL_DUT_ID - 1].strip()
        tcid = row[COL_MATTER_CASE - 1].strip()
        result = row[COL_TEST_RESULT - 1].strip().capitalize()

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
    company_tc_results = defaultdict(lambda: {"Pass": set(), "Fail": set(), "NotTested": 0})
    for company, dut, tcid, result in filtered_rows:
        if result == "Pass":
            company_tc_results[(company, tcid)]["Pass"].add(dut)
        elif result == "Fail":
            company_tc_results[(company, tcid)]["Fail"].add(dut)
        elif result.lower().startswith("not"):
            company_tc_results[(company, tcid)]["NotTested"] += 1

    # Summarize results per test case
    summary_data = defaultdict(lambda: {"Pass": 0, "Fail": 0, "NotTested": 0})
    for (company, tcid), results in company_tc_results.items():
        passes = len(results["Pass"]) > 0
        fails = len(results["Fail"]) > 0
        not_tested = results["NotTested"]

        if passes and fails:
            summary_data[tcid]["Pass"] += 1
            summary_data[tcid]["Fail"] += 1
        elif passes:
            summary_data[tcid]["Pass"] += 1
        elif fails:
            summary_data[tcid]["Fail"] += 1  # Fixed: count by company, not DUT count
        if not_tested:
            summary_data[tcid]["NotTested"] += not_tested

    # Build final summary
    # Columns: Test Case Name | Pass Count | Fail Count | Not Tested Count | Total | Total Pass+Fail | Runs Required | Certification Status
    output_data = [[
        "Test Case Name", "Pass Count", "Fail Count", "Not Tested Count",
        "Total", "Total Pass+Fail", "Number of runs required", "Certification Status"
    ]]
    never_executed, low_pass, others = [], [], []

    for tc in all_test_cases:
        counts = summary_data.get(tc, {"Pass": 0, "Fail": 0, "NotTested": 0})
        total_pf = counts["Pass"] + counts["Fail"]
        total_all = counts["Pass"] + counts["Fail"] + counts["NotTested"]
        runs_req = runs_required_map.get(tc, "3")
        cert_status = get_certification_status(counts["Pass"], runs_req)

        row = [
            tc,
            counts["Pass"],
            counts["Fail"],
            counts["NotTested"],
            total_all,
            total_pf,
            runs_req,
            cert_status
        ]

        # ✅ Fixed categorization: only "Not Executed" if no Pass AND no Fail
        if counts["Pass"] == 0 and counts["Fail"] == 0:
            never_executed.append(row)
        elif counts["Pass"] < int(runs_req) if runs_req.isdigit() else counts["Pass"] < 3:
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
    apply_delta_colors(delta_ws, spreadsheet)
    apply_certification_colors(summary_ws, spreadsheet)
    apply_pass_count_colors(summary_ws, spreadsheet)

    print("✅ Certification Status column populated successfully.")

    # Delta comparison
    new_summary_data = read_summary_data(summary_ws)
    filtered_cases = [r[0] for r in never_executed + low_pass]
    delta_data = compare_deltas(old_summary_data, new_summary_data, filtered_cases)

    delta_ws.clear()
    if len(delta_data) > 1:
        delta_ws.update(range_name="A1", values=delta_data)
        apply_delta_colors(delta_ws)
    else:
        delta_ws.update(range_name="A1",
                        values=[["No changes found among Not Executed and Low Pass test cases."]])

    print("✅ Summary and Delta sheets updated successfully!")

except Exception as e:
    print(f"❌ Unexpected error: {e}")
    raise
