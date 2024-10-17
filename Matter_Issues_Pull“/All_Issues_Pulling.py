import requests
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd

# GitHub Settings
REPOSITORIES = [
    {"name": "project-chip/connectedhomeip", "sheet_name": "ConnectedHomeIP"},  # First repo
    {"name": "project-chip/certification-tool", "sheet_name": "Certificationtool_Issues"},  # Second repo
    {"name": "project-chip/matter-test-scripts", "sheet_name": "Script_Issues"},  # Third repo
    {"name": "CHIP-Specifications/chip-test-plans", "sheet_name": "TestPlan_Issues"},  # Third repo
    # Add more repositories here
]
GITHUB_TOKEN = "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"  # Replace with your GitHub token

# Google Sheets Settings
SPREADSHEET_ID = "1mx9GKwpmrUVmeAEY6Q6nq__8FWoj0CjVUa6IOwm-AVE"  # Replace with your Google Sheet ID

# Define the required scopes
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]


# Authenticate with Google Sheets API
def authenticate_google_sheets():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


# Fetch all GitHub Issues with Pagination, excluding pull requests
def fetch_github_issues(repo_name):
    issues = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo_name}/issues"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}"
        }
        params = {
            "state": "open",  # Fetch only open issues (optional, remove for all issues)
            "per_page": 100,  # Fetch 100 issues per page (maximum allowed by GitHub API)
            "page": page
        }
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            page_issues = response.json()
            if not page_issues:
                break  # Exit the loop when no more issues are returned

            # Filter out pull requests by checking for the "pull_request" key
            issues.extend([issue for issue in page_issues if "pull_request" not in issue])

            page += 1  # Move to the next page
        else:
            print(f"Failed to fetch issues for {repo_name}: {response.status_code}")
            break
    return issues


# Insert issues data into Google Sheets
def update_google_sheet(issues, sheet):
    # Extract relevant fields
    issue_data = [
        [
            issue["number"],
            issue["title"],
            issue["user"]["login"],
            issue["state"],
            issue["created_at"],
            issue["updated_at"],
            issue["url"]
        ]
        print()
        for issue in issues
    ]

    # Insert into Google Sheets
    sheet.clear()  # Clear the existing content
    sheet.update("A1", [["Issue Number", "Title", "Author", "State", "Created At", "Updated At", "Issue Link"]])  # Add headers
    sheet.update("A2", issue_data)  # Add issue data


def main():
    # Authenticate Google Sheets
    client = authenticate_google_sheets()

    for repo in REPOSITORIES:
        repo_name = repo["name"]
        sheet_name = repo["sheet_name"]

        # Fetch GitHub issues for the repository
        issues = fetch_github_issues(repo_name)

        if issues:
            # Get or create a sheet (tab) for the repository
            try:
                sheet = client.worksheet(sheet_name)  # If the sheet already exists
            except gspread.exceptions.WorksheetNotFound:
                sheet = client.add_worksheet(title=sheet_name, rows="1000", cols="20")  # Create new sheet if not found

            # Update Google Sheet with issues
            update_google_sheet(issues, sheet)
            print(f"Google Sheet tab '{sheet_name}' updated with {len(issues)} issues from {repo_name}!")
        else:
            print(f"No issues found or failed to fetch issues for {repo_name}.")


if __name__ == "__main__":
    main()
