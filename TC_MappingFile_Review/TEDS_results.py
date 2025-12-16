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
SECTION_LOW_PASS = "---- Pass Count < 3 ----"
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


def apply_yellow_for_sections(sheet):
    try:
        data = sheet.get_all_values()
        for i, row in enumerate(data, start=1):
            if row and row[0].strip() in [SECTION_NOT_EXECUTED, SECTION_LOW_PASS, SECTION_PASSED]:
                sheet.format(f"A{i}:E{i}", {
                    "backgroundColor": {"red": 1, "green": 1, "blue": 0.6},
                    "textFormat": {"bold": True},
                    "horizontalAlignment": "CENTER",
                })
    except Exception as e:
        print(f"⚠️ Warning: Could not apply yellow highlighting: {e}")


def read_summary_data(sheet):
    try:
        data = sheet.get_all_values()
        summary = {}
        for row in data[1:]:
            if len(row) >= 5 and row[0] and row[1].isdigit():
                summary[row[0]] = {

                    "Pass": int(row[1]),
                    "Fail": int(row[2]),
                    "NotTested": int(row[3]),
                    "Total": int(row[4])
                }
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


def apply_delta_colors(sheet):
    try:
        data = sheet.get_all_values()
        for i, row in enumerate(data[1:], start=2):
            if len(row) < 10:
                continue
            status = row[9].strip()
            color = None
            if status == "Updated":
                color = {"red": 0.8, "green": 1, "blue": 0.8}
            elif status == "Reduced":
                color = {"red": 1, "green": 1, "blue": 0.6}
            elif status == "Changed":
                color = {"red": 0.9, "green": 0.9, "blue": 0.9}
            if color:
                sheet.format(f"A{i}:J{i}", {"backgroundColor": color})
    except Exception as e:
        print(f"⚠️ Warning: Could not apply delta colors: {e}")


# === MAIN EXECUTION ===
try:
    # Initialize Sheets
    try:
        summary_ws = spreadsheet.worksheet(SUMMARY_SHEET_NAME)
        old_summary_data = read_summary_data(summary_ws)
        clear_backgrounds_except_header(summary_ws)
    except gspread.exceptions.WorksheetNotFound:
        summary_ws = spreadsheet.add_worksheet(title=SUMMARY_SHEET_NAME, rows=2000, cols=6)
        old_summary_data = {}

    try:
        delta_ws = spreadsheet.worksheet(DELTA_SHEET_NAME)
        clear_backgrounds_except_header(delta_ws)
    except gspread.exceptions.WorksheetNotFound:
        delta_ws = spreadsheet.add_worksheet(title=DELTA_SHEET_NAME, rows=2000, cols=10)

    # Read test data
    tc_list_ws = spreadsheet.worksheet(MASTER_TC_SHEET)
    all_test_cases = tc_list_ws.col_values(1)[1:]

    results_ws = spreadsheet.worksheet(SOURCE_SHEET_NAME)
    rows = results_ws.get_all_values()[1:]  # Skip header

    # Process unique results
    unique_results = set()
    filtered_rows = []
    duplicates_removed = 0

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

    print(f"🧹 Removed {duplicates_removed} duplicate results (same company/DUT/test/result).")

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
            summary_data[tcid]["Fail"] += len(results["Fail"])
        if not_tested:
            summary_data[tcid]["NotTested"] += not_tested

    # Build final summary
    output_data = [["Test Case Name", "Pass Count", "Fail Count", "Not Tested Count", "Total", "Total Pass+Fail"]]
    never_executed, low_pass, others = [], [], []

    for tc in all_test_cases:
        counts = summary_data.get(tc, {"Pass": 0, "Fail": 0, "NotTested": 0})
        total_pf = counts["Pass"] + counts["Fail"]
        total_all = counts["Pass"] + counts["Fail"] + counts["NotTested"]

        # Columns:
        # [Test Case, Total, Total Pass+Fail, Pass, Fail, Not Tested]
        row = [tc, counts["Pass"], counts["Fail"], counts["NotTested"], total_all, total_pf]

        if counts["Pass"] == 0:
            # If no pass at all (even if fail or not tested), mark as "Not Executed Yet"
            never_executed.append(row)
        elif counts["Pass"] < 3:
            # Has passes, but less than 3
            low_pass.append(row)
        else:
            others.append(row)

    output_data += [[SECTION_NOT_EXECUTED]]
    output_data += never_executed or [["All test cases executed at least once"]]
    output_data += [[""], [SECTION_LOW_PASS]]
    output_data += low_pass or [["No test cases with Pass Count < 3"]]
    output_data += [[""], [SECTION_PASSED]]
    output_data += others or [["No remaining test cases"]]

    # Update summary sheet
    summary_ws.clear()
    summary_ws.update(range_name="A1", values=output_data)
    apply_yellow_for_sections(summary_ws)

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
