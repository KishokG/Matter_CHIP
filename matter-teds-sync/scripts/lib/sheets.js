/**
 * lib/sheets.js
 *
 * Reusable helpers: parse a CSV and write it (or any row list) into a
 * specific Google Sheet tab, creating the tab if it doesn't exist and
 * clearing old content first.
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

/**
 * Reads a CSV into an array of rows. Knack table exports are comma-separated,
 * but generated files like tclist_matter*.csv use ";" — the delimiter is
 * picked from whichever appears more often in the header line.
 */
function readCsvRows(csvPath) {
  if (!fs.existsSync(csvPath)) {
    throw new Error(`CSV not found at ${csvPath}`);
  }

  let csvContent = fs.readFileSync(csvPath, "utf8");
  // Knack's CSV export includes a UTF-8 BOM, which breaks csv-parse's quote
  // detection on the first field if left in place.
  csvContent = csvContent.replace(/^﻿/, "");
  const headerLine = csvContent.split(/\r?\n/, 1)[0];
  const count = (ch) => headerLine.split(ch).length - 1;
  const delimiter = count(";") > count(",") ? ";" : ",";

  return parse(csvContent, { delimiter, skip_empty_lines: false, relax_column_count: true });
}

// A1 notation needs the tab name quoted (with ' doubled) when it contains
// spaces/punctuation or could be mistaken for a cell reference.
function quoteTab(tabName) {
  return `'${tabName.replace(/'/g, "''")}'`;
}

async function uploadRowsToSheet({ rows, sheetId, tabName, serviceAccountJson }) {
  const auth = getAuth(serviceAccountJson);
  const sheets = google.sheets({ version: "v4", auth });

  const meta = await sheets.spreadsheets.get({ spreadsheetId: sheetId });
  const existingTab = meta.data.sheets.find((s) => s.properties.title === tabName);
  const tabWasCreated = !existingTab;
  if (tabWasCreated) {
    console.log(`Tab "${tabName}" not found in sheet ${sheetId} — creating it.`);
    await sheets.spreadsheets.batchUpdate({
      spreadsheetId: sheetId,
      requestBody: { requests: [{ addSheet: { properties: { title: tabName } } }] },
    });
  }

  await sheets.spreadsheets.values.clear({ spreadsheetId: sheetId, range: quoteTab(tabName) });
  await sheets.spreadsheets.values.update({
    spreadsheetId: sheetId,
    range: `${quoteTab(tabName)}!A1`,
    valueInputOption: "RAW",
    requestBody: { values: rows },
  });

  console.log(`Wrote ${rows.length} rows to tab "${tabName}" in spreadsheet ${sheetId}.`);

  return {
    rowCount: rows.length,
    dataRowCount: Math.max(0, rows.length - 1), // excluding header
    tabWasCreated,
  };
}

async function uploadCsvToSheet({ csvPath, sheetId, tabName, serviceAccountJson }) {
  const rows = readCsvRows(csvPath);
  console.log(`Parsed ${rows.length} rows (including header) from ${csvPath}.`);
  return uploadRowsToSheet({ rows, sheetId, tabName, serviceAccountJson });
}

module.exports = { readCsvRows, uploadRowsToSheet, uploadCsvToSheet };
