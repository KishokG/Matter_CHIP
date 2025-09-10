import json
import re
import yaml
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Load config from YAML
with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

# Extract values from config
credentials_file = config["credentials_file"]
sheet_url = config["sheet_url"]
worksheet_name = config["worksheet_name"]
json_file = config["json_file"]
output_file = config["output_file"]

# Load JSON test case IDs
with open(json_file, "r") as f:
    data = json.load(f)

json_tcs = set(data.keys())

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_file, scope)
client = gspread.authorize(creds)

# Open sheet and worksheet
sheet = client.open_by_url(sheet_url)
worksheet = sheet.worksheet(worksheet_name)

# Get all rows (skip header row) and take only Column D (index 3, since Python is 0-based)
rows = worksheet.get_all_values()
txt_tcs = {row[3].strip() for row in rows[1:] if len(row) > 3 and row[3].strip()}

# Compare sets
missing_in_json = sorted(txt_tcs - json_tcs)
extra_in_json = sorted(json_tcs - txt_tcs)

# Track issues
cert_status_issues = []
cert_mismatch_issues = []
pics_invalid_issues = []

# Forbidden characters for PICS entries (underscore _ allowed)
forbidden_chars = r"[,&(){}\-\]]"

inside_pics = False
current_tc_id = None
lines = []

# Read all lines so we can look ahead
with open(json_file, "r") as f:
    lines = f.readlines()

for num, line in enumerate(lines, start=1):
    # Detect test case IDs (lines ending with ": {")
    tc_match = re.match(r'\s*"([^"]+)":\s*{', line)
    if tc_match:
        current_tc_id = tc_match.group(1)

    # Check for empty CertificationStatus
    if '"CertificationStatus": ""' in line:
        cert_status_issues.append((num, current_tc_id))

    # Check for CertificationStatus value and validate next line
    status_match = re.search(r'"CertificationStatus":\s*"([^"]+)"', line)
    if status_match:
        status_val = status_match.group(1)
        expected_cert = None
        if status_val == "Executable":
            expected_cert = '"cert": "true"'
        elif status_val in ("Blocked", "Provisional"):
            expected_cert = '"cert": "false"'

        if expected_cert:
            # Look ahead at next line
            if num < len(lines):
                next_line = lines[num].strip().rstrip(",")
                if expected_cert not in next_line:
                    cert_mismatch_issues.append(
                        (num + 1, current_tc_id, status_val, expected_cert, next_line)
                    )

    # Detect start/end of PICS block
    if '"PICS": [' in line:
        inside_pics = True
    elif inside_pics and '],' in line:
        inside_pics = False

    # If inside PICS block, check for forbidden characters
    if inside_pics:
        pics_match = re.search(r'"([^"]+)"', line)
        if pics_match:
            value = pics_match.group(1)
            if re.search(forbidden_chars, value):
                pics_invalid_issues.append((num, value, current_tc_id))

# Write results to log file
with open(output_file, "w") as log:
    log.write(f"Total test cases available in JSON: {len(json_tcs)}\n")
    log.write(f"Total test cases available in Google Sheet: {len(txt_tcs)}\n")
    log.write(f"Missing in JSON (present in Sheet only): {len(missing_in_json)}\n")
    log.write(f"Extra unwanted test cases in JSON (not in Sheet): {len(extra_in_json)}\n")

    if missing_in_json:
        log.write("\n--- Missing in JSON ---\n")
        for tc in missing_in_json:
            log.write(tc + "\n")

    if extra_in_json:
        log.write("\n--- Extra in JSON (with line numbers + content) ---\n")
        with open(json_file, "r") as f:
            for num, line in enumerate(f, start=1):
                for key in extra_in_json:
                    if f'"{key}"' in line:
                        log.write(f"Line {num}: {line.strip()}\n")

    if cert_status_issues:
        log.write("\n--- CertificationStatus Issues (Empty) ---\n")
        for line_num, tc_id in cert_status_issues:
            log.write(f"Line {line_num}: CertificationStatus is empty in test case {tc_id}\n")

    if cert_mismatch_issues:
        log.write("\n--- CertificationStatus vs cert Mismatches ---\n")
        for line_num, tc_id, status_val, expected_cert, found_line in cert_mismatch_issues:
            log.write(
                f"Line {line_num}: In test case {tc_id}, CertificationStatus='{status_val}' "
                f"and expected cert value is {expected_cert}, but found: {found_line}\n"
            )

    if pics_invalid_issues:
        log.write("\n--- PICS Invalid Character Issues ---\n")
        for line_num, value, tc_id in pics_invalid_issues:
            log.write(f"Line {line_num}: Invalid PICS entry '{value}' in test case {tc_id}\n")

print(f"✅ Test case mapping file review & summary log saved to {output_file}")
