import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
import pandas as pd

# GitHub Settings
REPOSITORIES = [
    {"name": "CHIP-Specifications/chip-test-plans"},  # Example repo
    # Add more repositories here
]

github_token = "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"  # Replace with your GitHub token

# Google Sheets Settings
SPREADSHEET_ID = "16H9QlG3SiwAlMbkz7sWbI30f4_PtpBNUrQIuk6QXrCc"  # Replace with your Google Sheet ID

# Define the required scopes
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

#g = github.Github(github_token)
#service_account_json_dict = json.loads(service_account_json)

# Authenticate with Google Sheets API
def authenticate_google_sheets():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

# Fetch all merged GitHub Pull Requests with Pagination
def fetch_github_merged_pull_requests(repo_name):
    prs = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo_name}/pulls"
        headers = {
            "Authorization": f"token {github_token}"
        }
        params = {
            "state": "closed",  # We need closed PRs to check for merged ones
            "per_page": 100,  # Fetch 100 pull requests per page (maximum allowed by GitHub API)
            "page": page
        }
        response = requests.get(url, headers=headers, params=params)
        if response.status_code == 200:
            page_prs = response.json()

            # Filter only merged pull requests
            for pr in page_prs:
                if pr.get("merged_at"):
                    prs.append(pr)

            if not page_prs:
                break  # Exit the loop when no more pull requests are returned
            page += 1  # Move to the next page
        else:
            print(f"Failed to fetch pull requests for {repo_name}: {response.status_code}")
            break
    return prs

# Insert PR data into Google Sheets
def update_google_sheet(data, sheet, repo_name):
    repo_short_name = repo_name.split('/')[-1]
    headers = [
        ["Repository Name", "PR Number", "Title", "Author", "PR URL", "Merged Date"]
    ]

    # Sort data by PR number in descending order (most recent first)
    data.sort(key=lambda x: x["number"], reverse=True)

    data_to_insert = [
        [
            repo_short_name,  # Repository name
            item["number"],
            item["title"],
            item["user"]["login"],
            f"https://github.com/{repo_name}/pull/{item['number']}",
            item["merged_at"].split("T")[0] if item.get("merged_at") else "N/A",  # Get only the merged date (YYYY-MM-DD)
        ]
        for item in data
    ]

    # Clear the existing content and update with the new data
    sheet.clear()  # Clear the existing content
    print(f"Cleared existing data for Merged PRs.")

    sheet.update(range_name="A1", values=headers)
    if data_to_insert:
        sheet.update(range_name="A2", values=data_to_insert)
        print(f"Updated {len(data)} Merged PRs in Google Sheets!")
    else:
        print(f"No Merged PRs to update.")

# Filter merged PRs based on the user-specified date range
def filter_merged_prs_by_date(prs, start_date, end_date):
    merged_prs = []

    for pr in prs:
        merged_at_dt = datetime.strptime(pr["merged_at"], "%Y-%m-%dT%H:%M:%SZ") if pr.get("merged_at") else None

        if merged_at_dt and start_date <= merged_at_dt.date() <= end_date:
            merged_prs.append(pr)

    return merged_prs

# Get user input for start and end dates
def get_user_date_input():
    start_date_input = input("Enter the start date (YYYY-MM-DD): ")
    end_date_input = input("Enter the end date (YYYY-MM-DD): ")

    try:
        start_date = datetime.strptime(start_date_input, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_date_input, "%Y-%m-%d").date()

        if start_date > end_date:
            raise ValueError("Start date cannot be after end date!")

        return start_date, end_date
    except ValueError as ve:
        print(f"Invalid date input: {ve}")
        return None, None

def main():
    start_date, end_date = get_user_date_input()
    if not start_date or not end_date:
        return

    client = authenticate_google_sheets()

    for repo in REPOSITORIES:
        repo_name = repo["name"]

        # Fetch GitHub merged pull requests for the repository
        pull_requests = fetch_github_merged_pull_requests(repo_name)

        if pull_requests:
            merged_prs = filter_merged_prs_by_date(pull_requests, start_date, end_date)

            # Update the "Merged PRs" sheet
            try:
                merged_prs_sheet = client.worksheet("Merged_Pull_Requests_From_Date_To_Date")
            except gspread.exceptions.WorksheetNotFound:
                merged_prs_sheet = client.add_worksheet(title="Merged_Pull_Requests_From_Date_To_Date", rows="1000", cols="20")
            update_google_sheet(merged_prs, merged_prs_sheet, repo_name)

            print(f"Google Sheets updated with Merged PRs for {repo_name} between {start_date} and {end_date}!")
        else:
            print(f"No merged pull requests found for {repo_name} between {start_date} and {end_date}.")

if __name__ == "__main__":
    main()