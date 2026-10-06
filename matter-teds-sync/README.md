# Matter TEDS → Google Sheets Sync

Downloads CSV exports from the Matter TEDS Knack tool (using your website
login) and writes each one into a Google Sheet, on a schedule via GitHub
Actions. Supports syncing **multiple releases/tables** in a single run.

## How it works

1. **`config/releases.json`** — the list of tables you want synced. Each
   entry has its own table URL, the heading text identifying which table on
   the page to export (the page can have several tables), and which Google
   Sheet + tab it should land in. This file isn't a secret — check it into
   the repo and edit it whenever you need to add/change a release.
2. **`scripts/sync-all.js`** — the orchestrator. Logs into Knack **once**,
   then loops through every entry in `config/releases.json`, downloading and
   uploading each one. This is what the GitHub Action runs.
3. **`scripts/lib/knack.js`** and **`scripts/lib/sheets.js`** — shared logic
   for logging in/exporting a table, and for parsing/writing a CSV into a
   sheet tab. Used by `sync-all.js`.
4. **`scripts/download-teds-csv.js`** / **`scripts/upload-to-sheets.js`** —
   optional single-table testers, driven entirely by env vars, for trying
   out a *new* table/URL before adding it to the config.
5. **`analysis/TEDS_results.py`** (+ `analysis/sve_html_report.py`) — runs
   after the sync step. Reads the raw results from the `1.6_SVE_Results` tab
   (populated by the sync step above) and a master test-case list, computes
   pass/fail/certification status per test case, writes the `1.5_Summary`
   and `Summary Changes` tabs back into that same sheet, and generates a
   standalone HTML report. This step is independent of the Node sync — it
   just expects the raw data tab to already be populated when it runs,
   which is why it comes right after in the same workflow.

## One-time setup

### 1. Add GitHub Secrets
In your repo: **Settings → Secrets and variables → Actions → New repository secret**

These are shared across every release — no per-release secrets needed:

| Secret name | Value |
|---|---|
| `KNACK_APP_URL` | Base app URL, e.g. `https://zigbeecertifiedproducts.knack.com/test-event-data-stockpile-teds` |
| `KNACK_USERNAME` | Your Knack login email |
| `KNACK_PASSWORD` | Your Knack login password |
| `CREDENTIALS_JSON` | Full contents of your service account's JSON key file (paste the whole JSON — not a file path) |

### 2. Fill in `config/releases.json`
Each entry looks like:

```json
{
  "name": "matter-1.7",
  "description": "Matter 1.7 TE#1 - Anonymized DUTs",
  "tableUrl": "https://zigbeecertifiedproducts.knack.com/...(the full URL of that table's page)",
  "tableHeading": "Anonymized DUTs",
  "sheetId": "171Wwn6JCx_zWum9oLiEO_4GCDVKiYsN2cAcr6ubsp5c",
  "tabName": "Matter 1.7"
}
```

- `tableUrl` — copy straight from your browser's address bar while viewing that table (see "Getting a table URL" below).
- `tableHeading` — the heading text exactly as it appears above the table (e.g. `Anonymized DUTs`). This is what tells the script which of several tables on the page to export.
- `sheetId` — from the sheet's URL: `https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`
- `tabName` — the tab within that sheet to write to. Created automatically if it doesn't exist.

Add as many entries as you like — one per release/table. `sheetId`/`tabName` can point to the same sheet with different tabs, or entirely different sheets.

### 3. Share every target Sheet with the service account
Open the service account JSON, copy the `client_email` value, and share each
Google Sheet referenced in `config/releases.json` with that email as an
**Editor**.

### Getting a table URL
1. Log into the Knack app in your browser.
2. Navigate to the exact page showing the table you want (Export button, Search by keyword, etc. visible).
3. Copy the full URL from the address bar (everything including the `#...` part).

## Testing locally

Test the full config-driven sync:

```bash
cd scripts
npm install
npx playwright install chromium

export KNACK_APP_URL="https://zigbeecertifiedproducts.knack.com/test-event-data-stockpile-teds"
export KNACK_USERNAME="you@example.com"
export KNACK_PASSWORD="yourpassword"
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat /path/to/service-account.json)"   # locally this can be named anything; only the GitHub *secret* is called CREDENTIALS_JSON

node sync-all.js
```

It prints a per-release summary at the end (✔/✘) and downloads each CSV as
`scripts/<release-name>.csv` for inspection.

To try out a *new* table before adding it to the config, use the single-table
testers instead:

```bash
export KNACK_TABLE_URL="<paste the new table's URL>"
export KNACK_TABLE_HEADING="Anonymized DUTs"   # or whatever heading applies
node download-teds-csv.js

export GOOGLE_SHEET_ID="<the sheet id>"
export GOOGLE_SHEET_TAB_NAME="<the tab name>"
node upload-to-sheets.js
```

If a download fails, check `scripts/debug-after-export-click.png` and
`scripts/debug-screenshot.png` — they capture what was on screen at the
point of failure.

## Enable the workflow
Once secrets are set, `config/releases.json` is filled in, and local testing
works, push this repo to GitHub. The workflow runs automatically on the cron
schedule (`.github/workflows/sync-teds-to-sheets.yml`, currently daily at
06:00 UTC), or trigger it manually from the **Actions** tab → **Sync Matter
TEDS CSV to Google Sheets** → **Run workflow**. Every downloaded CSV is also
attached to the run as a build artifact for 7 days.

## Notes / limitations
- If Knack ever adds two-factor authentication or a CAPTCHA to login, this
  script will need to be adapted (or you'd need a Knack API key instead,
  which avoids browser automation entirely).
- Clicking **Export** in Knack typically exports the *entire* filtered
  dataset behind that table, not just the visible page — confirm this
  matches what you see when exporting manually.
- Rotate `KNACK_PASSWORD` if it's ever been typed directly into a terminal
  command (shell history keeps a plaintext copy) — use `read -s` to enter it
  without echoing/logging it, going forward.

## SVE results analysis stage (`analysis/`)

This stage runs `analysis/TEDS_results.py`, which reads its entire
configuration from the `"analyses"` array in `config/releases.json` — no
sheet IDs, tab names, or column mappings are hardcoded in the script itself.
It loops through every entry in that array and, for each one:

- Reads raw results from `sourceSheetName` and the master test-case list
  from `masterTcSheet` (both in the sheet identified by `sheetId`).
- Computes pass/fail/certification status per test case and writes
  `summarySheetName` and `deltaSheetName`.
- Generates a standalone HTML report via `analysis/sve_html_report.py`,
  named `sve_summary_report_<analysis-name>_<today's-date>.html`.

Each entry in `"analyses"` looks like:

```json
{
  "name": "matter-1.6-sve",
  "sheetId": "1kRAZQ8JJmbD6b0w3Mw9SUwXF2RPZO_i_NBq2HxGUI30",
  "sourceSheetName": "1.6_SVE_Results",
  "masterTcSheet": "1.5_SVE_TC_List",
  "summarySheetName": "1.5_Summary",
  "deltaSheetName": "Summary Changes",
  "reportTitle": "SVE Results Summary",
  "reportSubtitle": "Matter 1.6",
  "columns": { "companyId": 3, "dutId": 4, "matterCase": 8, "testResult": 9 }
}
```

Add another entry (e.g. for Matter 1.7) the same way you'd add a release —
just point it at the right sheet/tabs. `columns` only needs to change if a
future export's column order differs from this one.

**This uses the same `CREDENTIALS_JSON` secret** as the sync step — the
workflow writes it out to `analysis/credentials.json` before running, and
deletes it again afterward regardless of success/failure.

To test locally:
```bash
cd analysis
pip install -r requirements.txt
cp /path/to/service-account.json ./credentials.json
python TEDS_results.py
```
Generated HTML reports land in the `analysis/` folder alongside the
scripts.
