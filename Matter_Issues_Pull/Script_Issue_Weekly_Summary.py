import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

# GitHub Settings
REPOSITORIES = [
    {"name": "project-chip/matter-test-scripts"},  # Example repo
    # Add more repositories here
]

github_token = os.environ.get("PERSONNEL_TOKEN")
service_account_json = os.environ.get("CREDENTIALS_JSON")

# Google Sheets Settings
SPREADSHEET_ID = "1t7OeL2miAcecYoVBOJv4v7nJmQtsKqhi8NVxD7UEqqw"  # Replace with your Google Sheet ID

# Define the required scopes
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

g = github.Github(github_token)
service_account_json_dict = json.loads(service_account_json)

# Authenticate with Google Sheets API
def authenticate_google_sheets():
    creds = Credentials.from_service_account_info(service_account_json_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


# Fetch all GitHub Issues and Pull Requests with Pagination
def fetch_github_issues(repo_name):
    issues = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{repo_name}/issues"
        headers = {
            "Authorization": f"token {github_token}"
        }
        params = {
            "state": "all",  # Fetch all issues (open and closed)
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
            print(f"Failed to fetch issues for {repo_name}: {response.status_code}")
            break
    return issues


# Insert issues data into Google Sheets
def update_google_sheet(issues, sheet, category):
    headers = [
        ["Repository Name", "Issue Number", "State", " Issue Title", "Author/Raised By", "Issue url", "Type"]]

    # Prepare Google Sheets update data
    def prepare_issue_data(issues):
        return [
            [
                issue["repo_name"],
                issue["number"],
                issue["state"],
                issue["title"],
                issue["user"]["login"],
                issue["url"],
                "PR" if "pull_request" in issue else "Issue"
            ]
            for issue in issues
        ]

    # Update the Google Sheets tab for the specific category
    sheet.clear()  # Clear the existing content
    print(f"Cleared existing data for {category} issues.")

    sheet.update(range_name="A1", values=headers)
    if issues:
        sheet.update(range_name="A2", values=prepare_issue_data(issues))
        print(f"Updated {len(issues)} {category} issues in Google Sheets!")
    else:
        print(f"No {category} issues to update.")


# Filter issues based on the date range
def filter_issues(issues):
    today = datetime.utcnow()
    last_week = today - timedelta(days=7)

    new_issues = []
    closed_issues = []
    open_issues = []

    for issue in issues:
        created_at = datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ")
        closed_at = issue.get("closed_at")
        closed_at_dt = datetime.strptime(closed_at, "%Y-%m-%dT%H:%M:%SZ") if closed_at else None

        issue_data = {
            "repo_name": issue["repository_url"].split("/")[-1],
            "number": issue["number"],
            "state": issue["state"],
            "title": issue["title"],
            "user": issue["user"],
            "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
            "closed_at": closed_at_dt.strftime("%Y-%m-%d") if closed_at_dt else "",
            "url": f"https://github.com/{issue['repository_url'].split('/')[-1]}/issues/{issue['number']}",
            "created_year": created_at.year,
            "created_month": created_at.strftime("%b"),
        }

        if created_at >= last_week:
            new_issues.append(issue_data)
        elif closed_at_dt and closed_at_dt >= last_week:
            closed_issues.append(issue_data)
        elif issue["state"] == "open":
            open_issues.append(issue_data)

    return new_issues, closed_issues, open_issues


def main():
    client = authenticate_google_sheets()

    for repo in REPOSITORIES:
        repo_name = repo["name"]

        # Fetch GitHub issues for the repository
        issues = fetch_github_issues(repo_name)

        if issues:
            new_issues, closed_issues, open_issues = filter_issues(issues)

            # For Newly Raised Issues Tab
            try:
                new_issues_sheet = client.worksheet("New_Issues_In_Last_7Days")  # If the sheet already exists
            except gspread.exceptions.WorksheetNotFound:
                new_issues_sheet = client.add_worksheet(title="New_Issues_In_Last_7Days", rows="1000",
                                                        cols="20")  # Create new sheet if not found
            update_google_sheet(new_issues, new_issues_sheet, "Newly Raised")

            # For Closed Issues Tab
            try:
                closed_issues_sheet = client.worksheet("Closed_Issues_In_Last_7Days")  # If the sheet already exists
            except gspread.exceptions.WorksheetNotFound:
                closed_issues_sheet = client.add_worksheet(title="Closed_Issues_In_Last_7Days", rows="1000",
                                                           cols="20")  # Create new sheet if not found
            update_google_sheet(closed_issues, closed_issues_sheet, "Closed")

            # For Open Issues Tab
            try:
                open_issues_sheet = client.worksheet("Open_Issues_In_Last_7Days")  # If the sheet already exists
            except gspread.exceptions.WorksheetNotFound:
                open_issues_sheet = client.add_worksheet(title="Open_Issues_In_Last_7Days", rows="1000",
                                                         cols="20")  # Create new sheet if not found
            update_google_sheet(open_issues, open_issues_sheet, "Open")

            print(f"Google Sheets updated with Newly Raised, Closed, and Open issues for {repo_name}!")
        else:
            print(f"No issues found or failed to fetch issues for {repo_name}.")


if __name__ == "__main__":
    main()
