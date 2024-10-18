from datetime import datetime
import calendar
import time
import os
import github
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import yaml
import json
import requests
from google.oauth2.service_account import Credentials
import pandas as pd

# GitHub Settings
REPOSITORIES = [
    {"name": "project-chip/connectedhomeip", "sheet_name": "ConnectedHomeIP_Issues"},  # First repo
    {"name": "project-chip/certification-tool", "sheet_name": "Certificationtool_Issues"},  # Second repo
    {"name": "CHIP-Specifications/chip-certification-tool", "sheet_name": "Certificationtool_Issues"},  # Second repo
    {"name": "project-chip/matter-test-scripts", "sheet_name": "Script_Issues"},  # Third repo
    {"name": "CHIP-Specifications/chip-test-plans", "sheet_name": "TestPlan_Issues"},  # Third repo
    # Add more repositories here
]

github_token = os.environ.get("PERSONNEL_TOKEN")
service_account_json = os.environ.get("CREDENTIALS_JSON")

# Google Sheets Settings
SPREADSHEET_ID = "1mx9GKwpmrUVmeAEY6Q6nq__8FWoj0CjVUa6IOwm-AVE"  # Replace with your Google Sheet ID

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

# List of authors whose issues you want to pull
AUTHORS = ["Ashwinigrl", "KishokG", "Rajashreekalmane", "sumaky", "kvsmohan"]  # Replace with GitHub usernames of the authors

# Fetch all GitHub Issues created by specific authors, excluding pull requests
def fetch_github_issues(repo_name):
    issues = []
    for author in AUTHORS:
        page = 1
        while True:
            url = f"https://api.github.com/repos/{repo_name}/issues"
            headers = {
                "Authorization": f"token {github_token}"  # Use the correct GitHub token variable
            }
            params = {
                "state": "all",  # Fetch only open issues
                "creator": author,  # Filter by issue creator (author)
                "per_page": 100,  # Fetch 100 issues per page (maximum allowed by GitHub API)
                "page": page
            }
            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 200:
                page_issues = response.json()
                if not page_issues:
                    break  # Exit the loop when no more issues are returned

                # Include both issues and pull requests
                issues.extend(page_issues)

                # Filter out pull requests by checking for the "pull_request" key
                #issues.extend([issue for issue in page_issues if "pull_request" not in issue])

                page += 1  # Move to the next page
            else:
                print(f"Failed to fetch issues for {repo_name} by author {author}: {response.status_code}")
                break
    return issues

# Insert issues data into Google Sheets
def update_google_sheet(issues, sheet, repo_name):
    repo_short_name = repo_name.split('/')[-1]

    # Sort issues by issue number
    issues.sort(key=lambda x: x["number"])  # Sort by issue number (ID)
    issues.reverse()  # Reverse the list to have the last issue first

    # List of authors that should get "GRLQA"
    specific_authors = ["Ashwinigrl", "KishokG", "Rajashreekalmane"]  # Replace with the actual author usernames

    
    # Extract relevant fields
    issue_data = [
        [
            repo_short_name,  # Add repository name
            issue["number"],
            issue["state"],
            issue["title"],
            issue["user"]["login"],
            created_at := datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ"),  # Parse created date
            datetime.strptime(issue["updated_at"], "%Y-%m-%dT%H:%M:%SZ").date().isoformat(),  # Format updated date to string
            #issue["url"],
            f"https://github.com/{repo_name}/issues/{issue['number']}",  # Direct link to the GitHub issue
            created_at.year,  # Extract the created year
            created_at.strftime("%b"),  # Extract the month in 3-letter format,
            "GRLQA" if issue["user"]["login"] in specific_authors else "",  # Check if author is in specific_authors
            "GRLTEAM"
        ]
        for issue in issues
    ]

     # Convert created_at to a string format for the sheet
    for i in range(len(issue_data)):
        issue_data[i][5] = issue_data[i][5].strftime("%Y-%m-%d")  # Convert created date to string format

    # Clear the existing content
    if clear_sheet:
        sheet.clear()  # This clears the existing content from the sheet

     # Insert headers for the first time
        sheet.update("A1", [["Repository Name", "Issue Number", "State", "Title", "Author", "Created Date", "Closed Date", "Issue Link", "Year", "Month", "GRLQA", "Team"]])  # Add headers

    # Append the new issue data starting from the first available row
    existing_data = len(sheet.get_all_values()) + 1  # Get the number of existing rows
    sheet.update(f"A{existing_data + 1}", issue_data)  # Append issue data starting after the last row

    return issue_data  # Return the issue data for the consolidated sheet


def main():
    # Authenticate Google Sheets
    client = authenticate_google_sheets()

    all_issues_data = []  # List to hold all issues data for the consolidated sheet


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
                
             # Clear the sheet only for the first repository processed for Certificationtool_Issues
            clear_sheet = (repo_name == "project-chip/certification-tool")

            # Update Google Sheet with issues and collect issue data
            issues_data = update_google_sheet(issues, sheet, repo_name, clear_sheet=clear_sheet)  # Pass clear_sheet argument
            all_issues_data.extend(issues_data)  # Append to the consolidated list
            print(f"Google Sheet tab '{sheet_name}' updated with {len(issues)} issues from {repo_name}!")
        else:
            print(f"No issues found or failed to fetch issues for {repo_name}.")

     # Create or get the consolidated sheet
    try:
        consolidated_sheet = client.worksheet("All_Issues")  # If the sheet already exists
    except gspread.exceptions.WorksheetNotFound:
        consolidated_sheet = client.add_worksheet(title="All_Issues", rows="1000", cols="20")  # Create new sheet if not found

    # Update consolidated sheet with all issues
    consolidated_sheet.clear()  # Clear the existing content
    consolidated_sheet.update("A1", [["Repository Name", "Issue Number", "State", "Title", "Author", "Created Date", "Created Year", "Created Month", "Closed Date", "Issue Link", "GRLQA", "Team"]])  # Add headers
    consolidated_sheet.update("A2", all_issues_data)  # Add all issues data


if __name__ == "__main__":
    main()
