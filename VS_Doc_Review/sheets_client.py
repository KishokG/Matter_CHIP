"""
Talks to the Google Sheets API and hands back plain lists of lists.
Keeping this isolated means the validators never need to know anything
about auth, quotas or the gspread API.
"""

from typing import List

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _client(service_account_file: str) -> gspread.Client:
    creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
    return gspread.authorize(creds)


def fetch_tab_values(
    spreadsheet_id: str,
    tab_name: str,
    service_account_file: str,
) -> List[List[str]]:
    """
    Returns every row of `tab_name` as a list of lists of strings.
    Row 1 of the returned list is row 1 of the sheet (nothing is skipped
    here - the caller decides where the header/data actually start).
    Short rows are NOT padded; do that where columns are read.
    """
    client = _client(service_account_file)
    sheet = client.open_by_key(spreadsheet_id)
    worksheet = sheet.worksheet(tab_name)
    return worksheet.get_all_values()
