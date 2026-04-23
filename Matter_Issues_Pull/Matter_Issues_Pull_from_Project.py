import requests
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import os
import json

# -------------------------------
# SETTINGS
# -------------------------------

github_token = os.environ.get("PERSONNEL_TOKEN")
service_account_json = os.environ.get("CREDENTIALS_JSON")

if not github_token:
    raise ValueError("PERSONNEL_TOKEN is missing")

if not service_account_json:
    raise ValueError("CREDENTIALS_JSON is missing")

service_account_json_dict = json.loads(service_account_json)
PROJECT_ORG = "project-chip"
PROJECT_NUMBER = 142
PROJECT_SHEET_NAME = "Project_142_Issues"

SPREADSHEET_ID = "171Wwn6JCx_zWum9oLiEO_4GCDVKiYsN2cAcr6ubsp5c"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# -------------------------------
# FILTER CONFIGURATION
# -------------------------------

FILTER_CONFIG = {
    "1.6_Issues_Tracking": {
        "field": "Fix Required For",
        "value": "Needs to be fixed for 1.6 release"
    },
    "1.7_Issues_Tracking": {
        "field": "Fix Required For",
        "value": "Needs to be fixed for 1.7 release"
    },
    "1.6_TE2_Issues": {
        "field": "Found In",
        "value": "1.6 TE2"
    },
    "1.6_SVE_Issues": {
        "field": "Found In",
        "value": "1.6 SVE"
    }
}

# -------------------------------
# Google Sheets Authentication
# -------------------------------

def authenticate_google_sheets():
    creds = Credentials.from_service_account_info(service_account_json_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)

# -------------------------------
# Fetch Project Items
# -------------------------------

def fetch_project_issues(org, project_number):

    project_items = []

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "application/json"
    }

    query = """
    query($org: String!, $number: Int!, $cursor: String) {
      organization(login: $org) {
        projectV2(number: $number) {
          items(first: 100, after: $cursor) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              fieldValues(first: 20) {
                nodes {
                  ... on ProjectV2ItemFieldSingleSelectValue {
                    name
                    field { ... on ProjectV2SingleSelectField { name } }
                  }
                  ... on ProjectV2ItemFieldTextValue {
                    text
                    field { ... on ProjectV2FieldCommon { name } }
                  }
                }
              }
              content {
                ... on Issue {
                  number
                  title
                  url
                  state
                  createdAt
                  updatedAt
                  repository { nameWithOwner }
                  author { login }
                }
                ... on PullRequest {
                  number
                  title
                  url
                  state
                  createdAt
                  updatedAt
                  repository { nameWithOwner }
                  author { login }
                }
              }
            }
          }
        }
      }
    }
    """

    cursor = None

    while True:

        variables = {"org": org, "number": project_number, "cursor": cursor}

        response = requests.post(
            "https://api.github.com/graphql",
            json={"query": query, "variables": variables},
            headers=headers
        )

        if response.status_code != 200:
            print("GraphQL error:", response.text)
            break

        data = response.json()
        items = data["data"]["organization"]["projectV2"]["items"]["nodes"]

        for item in items:

            content = item["content"]
            if not content:
                continue

            field_dict = {}

            for field in item["fieldValues"]["nodes"]:
                try:
                    field_name = field["field"]["name"]
                    value = field.get("name") or field.get("text")
                    field_dict[field_name] = value
                except:
                    continue

            project_items.append({
                "repo": content["repository"]["nameWithOwner"],
                "number": content["number"],
                "title": content["title"],
                "state": content["state"],
                "url": content["url"],
                "author": content["author"]["login"] if content["author"] else "",
                "createdAt": content["createdAt"],
                "updatedAt": content["updatedAt"],
                "fields": field_dict
            })

        page_info = data["data"]["organization"]["projectV2"]["items"]["pageInfo"]

        if page_info["hasNextPage"]:
            cursor = page_info["endCursor"]
        else:
            break

    return project_items

# -------------------------------
# Update Google Sheet
# -------------------------------

def update_sheet(sheet, headers, rows):

    sheet.clear()

    sheet.update("A1", [headers])

    if rows:
        sheet.update("A2", rows)

# -------------------------------
# MAIN
# -------------------------------

def main():

    client = authenticate_google_sheets()

    project_items = fetch_project_issues(PROJECT_ORG, PROJECT_NUMBER)

    all_rows = []

    filtered_rows = {sheet: [] for sheet in FILTER_CONFIG}

    for item in project_items:

        created_at = datetime.strptime(item["createdAt"], "%Y-%m-%dT%H:%M:%SZ")

        fields = item["fields"]

        repo_name = item["repo"].split("/")[-1]

        row = [
            repo_name,
            item["number"],
            item["state"],
            item["title"],
            item["author"],
            created_at.strftime("%Y-%m-%d %H:%M:%S"),
            item["updatedAt"],
            fields.get("Status"),
            fields.get("Domain"),
            fields.get("Feature Area"),
            fields.get("Found In"),
            fields.get("Fix Required For"),
            fields.get("PR"),
            fields.get("Comments"),
            item["url"]
        ]

        all_rows.append(row)

        for sheet_name, filter_rule in FILTER_CONFIG.items():

            field_name = filter_rule.get("field")
            expected_value = filter_rule.get("value")

            if not field_name or not expected_value:
                print(f"Invalid filter config for {sheet_name}")
                continue

            field_value = fields.get(field_name)

            if field_value == expected_value:
                filtered_rows[sheet_name].append(row)

    headers = [
        "Repo", "Number", "State", "Title", "Author",
        "Created", "Updated", "Status", "Domain", "Feature Area",
        "Found In", "Fix Required For", "PR",
        "Comments", "URL"
    ]

    # Main sheet
    try:
        main_sheet = client.worksheet(PROJECT_SHEET_NAME)
    except:
        main_sheet = client.add_worksheet(title=PROJECT_SHEET_NAME, rows="3000", cols="25")

    update_sheet(main_sheet, headers, all_rows)

    # Filtered sheets
    for sheet_name, rows in filtered_rows.items():

        try:
            sheet = client.worksheet(sheet_name)
        except:
            sheet = client.add_worksheet(title=sheet_name, rows="3000", cols="25")

        update_sheet(sheet, headers, rows)

    print(f"Total Issues: {len(all_rows)}")

    for sheet_name, rows in filtered_rows.items():
        print(f"{sheet_name}: {len(rows)} issues")

    print("Google Sheets updated successfully.")

if __name__ == "__main__":
    main()
