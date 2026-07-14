/**
 * sync-all.js
 *
 * Reads config/releases.json and, for every entry, downloads that table's
 * CSV export from Knack and uploads it into the specified Google Sheet tab.
 * Logs into Knack only once and reuses the session for every release.
 *
 * REQUIRED ENV VARS (set as GitHub Secrets — shared across all releases):
 *   KNACK_APP_URL               - base URL of the Knack app
 *   KNACK_USERNAME              - login email/username
 *   KNACK_PASSWORD              - login password
 *   GOOGLE_SERVICE_ACCOUNT_JSON - full contents of the service account JSON key
 *
 * Per-release settings (tableUrl, tableHeading, sheetId, tabName) live in
 * config/releases.json instead, since those aren't secrets and are easier
 * to maintain as a checked-in list.
 */

const path = require("path");
const fs = require("fs");
const { chromium } = require("playwright");
const { loginToKnack, exportTableCsv } = require("./lib/knack");
const { uploadCsvToSheet } = require("./lib/sheets");

const APP_URL = process.env.KNACK_APP_URL;
const USERNAME = process.env.KNACK_USERNAME;
const PASSWORD = process.env.KNACK_PASSWORD;
const SERVICE_ACCOUNT_JSON = process.env.GOOGLE_SERVICE_ACCOUNT_JSON;
const CONFIG_PATH = process.env.RELEASES_CONFIG_PATH || path.join(__dirname, "..", "config", "releases.json");
const DEBUG_DIR = __dirname;

if (!APP_URL || !USERNAME || !PASSWORD || !SERVICE_ACCOUNT_JSON) {
  console.error(
    "Missing one or more required env vars: KNACK_APP_URL, KNACK_USERNAME, KNACK_PASSWORD, GOOGLE_SERVICE_ACCOUNT_JSON"
  );
  process.exit(1);
}

function loadReleases() {
  if (!fs.existsSync(CONFIG_PATH)) {
    throw new Error(`Config file not found at ${CONFIG_PATH}`);
  }
  const { releases } = JSON.parse(fs.readFileSync(CONFIG_PATH, "utf8"));
  if (!Array.isArray(releases) || releases.length === 0) {
    throw new Error(`No releases found in ${CONFIG_PATH}`);
  }
  for (const r of releases) {
    for (const field of ["name", "tableUrl", "sheetId", "tabName"]) {
      if (!r[field]) throw new Error(`Release entry missing required field "${field}": ${JSON.stringify(r)}`);
    }
  }
  return releases;
}

async function main() {
  const releases = loadReleases();
  console.log(`Loaded ${releases.length} release(s) from ${CONFIG_PATH}`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();

  const results = [];

  try {
    await loginToKnack(page, { appUrl: APP_URL, username: USERNAME, password: PASSWORD });

    for (const release of releases) {
      const csvPath = path.join(__dirname, `${release.name}.csv`);
      console.log(`\n=== ${release.name} ===`);
      try {
        await exportTableCsv(page, context, {
          tableUrl: release.tableUrl,
          tableHeading: release.tableHeading || "Anonymized DUTs",
          username: USERNAME,
          password: PASSWORD,
          outputPath: csvPath,
          debugDir: DEBUG_DIR,
        });

        await uploadCsvToSheet({
          csvPath,
          sheetId: release.sheetId,
          tabName: release.tabName,
          serviceAccountJson: SERVICE_ACCOUNT_JSON,
        });

        results.push({ name: release.name, status: "ok" });
      } catch (err) {
        console.error(`Failed to sync "${release.name}":`, err.message);
        results.push({ name: release.name, status: "failed", error: err.message });
      }
    }
  } finally {
    await browser.close();
  }

  console.log("\n=== Summary ===");
  for (const r of results) {
    console.log(`${r.status === "ok" ? "✔" : "✘"} ${r.name}${r.error ? ` — ${r.error}` : ""}`);
  }

  if (results.some((r) => r.status !== "ok")) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
