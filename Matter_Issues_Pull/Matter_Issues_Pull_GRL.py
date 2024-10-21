import requests
import gspread
from google.oauth2.service_account import Credentials
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import calendar
import time
import os
import github
import yaml
import json
import pandas as pd

# GitHub Settings
REPOSITORIES = [
    {"name": "project-chip/connectedhomeip", "sheet_name": "ConnectedHomeIP_Issues"},  # First repo
    {"name": "project-chip/certification-tool", "sheet_name": "Certificationtool_Issues"},  # Second repo
    {"name": "CHIP-Specifications/chip-certification-tool", "sheet_name": "Chip-Certificationtool_Issues_Old_repo"},  # Add new repo here
    {"name": "project-chip/matter-test-scripts", "sheet_name": "Script_Issues"},  # Third repo
    {"name": "CHIP-Specifications/chip-test-scripts", "sheet_name": "Chip-Script_Issues_Old_repo"},  # Third repo
    {"name": "CHIP-Specifications/chip-test-plans", "sheet_name": "TestPlan_Issues"},  # Third repo
    # Add more repositories here
]

github_token = os.environ.get("PERSONNEL_TOKEN")
service_account_json = os.environ.get("CREDENTIALS_JSON")

# Google Sheets Settings
SPREADSHEET_ID = "1qHYCQqg17gd1gRF-CjjKlSi1NrOgEllJtk_1kayz_tc"  # Replace with your Google Sheet ID

# Define the required scopes
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

g = github.Github(github_token)
service_account_json_dict = json.loads(service_account_json)

# Authenticate with Google Sheets API
def authenticate_google_sheets():
    creds = Credentials.from_service_account_info(service_account_json_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)
    
# Load authors from YAML file
with open('Authors_ID.yaml', 'r') as file:
    authors_data = yaml.safe_load(file)

AUTHORS = authors_data['AUTHORS']  # Load the list of authors
specific_authors = authors_data['specific_authors']  # Load the list of specific authors


# Fetch all GitHub Issues and Pull Requests with Pagination
def fetch_github_issues(repo_name):
    issues = []
    for author in AUTHORS:
        page = 1
        while True:
            url = f"https://api.github.com/repos/{repo_name}/issues"
            headers = {
                "Authorization": f"token {github_token}"
            }
            params = {
                "state": "all",  # Fetch only open issues (optional, remove for all issues)
                "creator": author,  # Filter by issue creator (author)
                "per_page": 100,  # Fetch 100 issues per page (maximum allowed by GitHub API)
                "page": page
            }
            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 200:
                page_issues = response.json()
                if not page_issues:
                    break  # Exit the loop when no more issues are returned
                issues.extend(page_issues)
                page += 1  # Move to the next page
            else:
                print(f"Failed to fetch issues for {repo_name} by author {author}: {response.status_code}")
                break
    return issues


# Insert issues data into Google Sheets
def update_google_sheet(issues, sheet, repo_name, all_issues_data=None):
    repo_short_name = repo_name.split('/')[-1]

    # Sort issues by issue number
    issues.sort(key=lambda x: x["number"])  # Sort by issue number (ID)
    issues.reverse()  # Reverse the list to have the last issue first

    # Extract relevant fields with datetime conversion to string
    issue_data = [
        [
            repo_short_name,  # Add repository name
            issue["number"],
            issue["state"],
            issue["title"],
            issue["user"]["login"],
            (created_at := datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ")).strftime("%Y-%m-%d %H:%M:%S"),
            # Convert datetime to string
            datetime.strptime(issue["updated_at"], "%Y-%m-%dT%H:%M:%SZ").date().isoformat(),
            # Format updated date to string
            #f"https://github.com/{repo_name}/issues/{issue['number']}",  # Direct link to the GitHub issue
            f"https://github.com/{repo_name}/{'pull' if 'pull_request' in issue else 'issues'}/{issue['number']}",
            created_at.year,  # Extract the created year
            created_at.strftime("%b"),  # Extract the month in 3-letter format
            "GRLQA" if issue["user"]["login"] in specific_authors else "",  # Check if author is in specific_authors
            "GRLTEAM",
            "PR" if "pull_request" in issue else "Issue",  # Check if it's a pull request or issue
        ]
        for issue in issues
    ]
        # Insert into Google Sheets
    sheet.clear()  # Clear the existing content
    print("Cleared existing data's.")
    sheet.update(range_name="A1", values=[["Repository Name", "Issue Number", "State", "Title", "Author", "Created Date", "Closed Date", "Issue Link",
             "Year", "Month", "Ref_1", "Ref_2", "Type"]])  # Add headers
    sheet.update(range_name="A2", values=issue_data)  # Add issue data

    # Add the issue data to the global all_issues_data list (if provided)
    if all_issues_data is not None:
        all_issues_data.extend(issue_data)  # Append the current repo's issues

def main():
    # Authenticate Google Sheets
    client = authenticate_google_sheets()
    all_issues_data = []  # This will store all issues from all repositories

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
            update_google_sheet(issues, sheet, repo_name, all_issues_data)
            print(f"Google Sheet tab '{sheet_name}' updated with {len(issues)} issues from {repo_name}!")
        else:
            print(f"No issues found or failed to fetch issues for {repo_name}.")

    # Update the "All_repo_Issues" tab with combined data from all repositories
    if all_issues_data:
        all_issues_data_sorted = sorted(all_issues_data, key=lambda x: datetime.strptime(x[5], "%Y-%m-%d %H:%M:%S"), reverse=True)
        try:
            all_issues_sheet = client.worksheet("All_repo_Issues")  # If the sheet already exists
        except gspread.exceptions.WorksheetNotFound:
            all_issues_sheet = client.add_worksheet(title="All_repo_Issues", rows="3000",
                                                    cols="20")  # Create new sheet if not found

            # Clear and update the All_repo_Issues sheet with combined data
        all_issues_sheet.clear()  # Clear the existing content
        print("Cleared All_repo_Issues existing data's.")
        all_issues_sheet.update(range_name="A1", values=[
                ["Repository Name", "Issue Number", "State", "Title", "Author", "Created Date", "Closed Date", "Issue Link",
             "Year", "Month", "Ref_1", "Ref_2", "Type"]])  # Add headers
        all_issues_sheet.update(range_name="A2", values=all_issues_data_sorted)  # Add all issues data

        print(f"Google Sheet tab 'All_repo_Issues' updated with {len(all_issues_data_sorted)} total issues from all repositories, sorted by Created Date!")
    else:
        print("No issues found in any repository.")

if __name__ == "__main__":
    main()
