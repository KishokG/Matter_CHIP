/**
 * lib/sheets.js
 *
 * Reusable helper: parse a CSV and write it into a specific Google Sheet tab,
 * creating the tab if it doesn't exist and clearing old content first.
 */

const fs = require("fs");
const { parse } = require("csv-parse/sync");
const { google } = require("googleapis");

let cachedAuth = null;
function getAuth(serviceAccountJson) {
  if (cachedAuth) return cachedAuth;

  let credentials;
  try {
    credentials = JSON.parse(serviceAccountJson);
  } catch (err) {
    if (fs.existsSync(serviceAccountJson)) {
      credentials = JSON.parse(fs.readFileSync(serviceAccountJson, "utf8"));
    } else {
      throw err;
    }
  }

  cachedAuth = new google.auth.GoogleAuth({
    credentials,
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });
  return cachedAuth;
}

async function uploadCsvToSheet({ csvPath, sheetId, tabName, serviceAccountJson }) {
  if (!fs.existsSync(csvPath)) {
    throw new Error(`CSV not found at ${csvPath}`);
  }

  let csvContent = fs.readFileSync(csvPath, "utf8");
  // Knack's CSV export includes a UTF-8 BOM, which breaks csv-parse's quote
  // detection on the first field if left in place.
  csvContent = csvContent.replace(/^\uFEFF/, "");
  const rows = parse(csvContent, { skip_empty_lines: false, relax_column_count: true });
  console.log(`Parsed ${rows.length} rows (including header) from ${csvPath}.`);

  const auth = getAuth(serviceAccountJson);
  const sheets = google.sheets({ version: "v4", auth });

  const meta = await sheets.spreadsheets.get({ spreadsheetId: sheetId });
  const existingTab = meta.data.sheets.find((s) => s.properties.title === tabName);
  if (!existingTab) {
    console.log(`Tab "${tabName}" not found in sheet ${sheetId} — creating it.`);
    await sheets.spreadsheets.batchUpdate({
      spreadsheetId: sheetId,
      requestBody: { requests: [{ addSheet: { properties: { title: tabName } } }] },
    });
  }

  await sheets.spreadsheets.values.clear({ spreadsheetId: sheetId, range: `${tabName}` });
  await sheets.spreadsheets.values.update({
    spreadsheetId: sheetId,
    range: `${tabName}!A1`,
    valueInputOption: "RAW",
    requestBody: { values: rows },
  });

  console.log(`Wrote ${rows.length} rows to tab "${tabName}" in spreadsheet ${sheetId}.`);
}

module.exports = { uploadCsvToSheet };
