/**
 * upload-to-sheets.js
 *
 * Manual single-sheet tester — uploads ./teds-export.csv into one Google
 * Sheet tab using env vars directly, without touching config/releases.json.
 *
 * REQUIRED ENV VARS:
 *   GOOGLE_SERVICE_ACCOUNT_JSON - service account JSON contents (or a file path to it)
 *   GOOGLE_SHEET_ID             - the spreadsheet ID
 *   GOOGLE_SHEET_TAB_NAME       - (optional) tab name, defaults to "TEDS Export"
 *
 * For syncing multiple releases on a schedule, use sync-all.js + config/releases.json instead.
 */

const path = require("path");
const { uploadCsvToSheet } = require("./lib/sheets");

const CSV_PATH = path.join(__dirname, "teds-export.csv");
const SHEET_ID = process.env.GOOGLE_SHEET_ID;
const TAB_NAME = process.env.GOOGLE_SHEET_TAB_NAME || "TEDS Export";
const SERVICE_ACCOUNT_JSON = process.env.GOOGLE_SERVICE_ACCOUNT_JSON;

if (!SHEET_ID || !SERVICE_ACCOUNT_JSON) {
  console.error("Missing required env vars: GOOGLE_SHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON");
  process.exit(1);
}

uploadCsvToSheet({ csvPath: CSV_PATH, sheetId: SHEET_ID, tabName: TAB_NAME, serviceAccountJson: SERVICE_ACCOUNT_JSON }).catch(
  (err) => {
    console.error("Failed to upload to Google Sheets:", err);
    process.exit(1);
  }
);
