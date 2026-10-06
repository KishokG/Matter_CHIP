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
// Optional: restrict to a single release by name (matches the "name" field
// in config/releases.json). Leave unset to run every release, as normal.
const RELEASE_NAME_FILTER = (process.env.RELEASE_NAME || "").trim();
// Optional: restrict to releases of a given "type" — "registration" or
// "results" — matching the "type" field in config/releases.json. This is
// what lets the workflow run "just registration data" or "just event
// results" as separate stages. Leave unset to run every release regardless
// of type.
const RELEASE_TYPE_FILTER = (process.env.RELEASE_TYPE || "").trim();
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

  let filtered = releases;

  if (RELEASE_TYPE_FILTER) {
    filtered = filtered.filter((r) => (r.type || "registration") === RELEASE_TYPE_FILTER);
    if (filtered.length === 0) {
      const types = [...new Set(releases.map((r) => r.type || "registration"))];
      throw new Error(
        `No releases with type "${RELEASE_TYPE_FILTER}" found in ${CONFIG_PATH}. Available types: ${types.join(", ")}`
      );
    }
  }

  if (RELEASE_NAME_FILTER) {
    filtered = filtered.filter((r) => r.name === RELEASE_NAME_FILTER);
    if (filtered.length === 0) {
      throw new Error(
        `No release named "${RELEASE_NAME_FILTER}" found (after type filtering) in ${CONFIG_PATH}. Available: ${releases.map((r) => r.name).join(", ")}`
      );
    }
  }

  return filtered;
}

function appendStepSummary(markdown) {
  const summaryPath = process.env.GITHUB_STEP_SUMMARY;
  if (!summaryPath) return; // not running in GitHub Actions (or older runner) — skip silently
  try {
    fs.appendFileSync(summaryPath, markdown + "\n");
  } catch (err) {
    console.error("Could not write to GITHUB_STEP_SUMMARY:", err.message);
  }
}

async function main() {
  const releases = loadReleases();
  console.log(`Loaded ${releases.length} release(s) from ${CONFIG_PATH}`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();

  const results = [];

  try {
    try {
      await loginToKnack(page, { appUrl: APP_URL, username: USERNAME, password: PASSWORD });
    } catch (err) {
      console.error("Knack login failed:", err.message);
      appendStepSummary(
        ["### Knack → Google Sheets Sync", "", `❌ **Login to Knack failed:** ${err.message}`, ""].join("\n")
      );
      throw err;
    }

    for (const release of releases) {
      const csvPath = path.join(__dirname, `${release.name}.csv`);
      console.log(`\n=== ${release.name} ===`);
      const detail = { name: release.name, type: release.type || "registration" };
      try {
        await exportTableCsv(page, context, {
          tableUrl: release.tableUrl,
          tableHeading: release.tableHeading || "Anonymized DUTs",
          username: USERNAME,
          password: PASSWORD,
          outputPath: csvPath,
          debugDir: DEBUG_DIR,
        });
        detail.downloadOk = true;

        const uploadResult = await uploadCsvToSheet({
          csvPath,
          sheetId: release.sheetId,
          tabName: release.tabName,
          serviceAccountJson: SERVICE_ACCOUNT_JSON,
        });
        detail.uploadOk = true;
        detail.rowCount = uploadResult.dataRowCount;
        detail.tabWasCreated = uploadResult.tabWasCreated;
        detail.status = "ok";
      } catch (err) {
        console.error(`Failed to sync "${release.name}":`, err.message);
        detail.status = "failed";
        detail.error = err.message;
      }
      results.push(detail);
    }
  } finally {
    await browser.close();
  }

  console.log("\n=== Summary ===");
  for (const r of results) {
    console.log(`${r.status === "ok" ? "✔" : "✘"} ${r.name}${r.error ? ` — ${r.error}` : ""}`);
  }

  // Write a GitHub Actions step summary so the run's summary page shows a
  // clear per-release breakdown (login/download/import), without needing
  // separate jobs or steps for each phase.
  const summaryLines = [
    "### Knack → Google Sheets Sync",
    "",
    `✅ Logged into Knack (\`${APP_URL}\`)`,
    "",
    "| Release | Type | Download | Import | Rows Imported | Sheet Tab |",
    "|---|---|---|---|---|---|",
  ];
  for (const r of results) {
    const downloadCell = r.downloadOk ? "✅" : "❌";
    const importCell = r.uploadOk ? "✅" : "❌";
    const rowsCell = r.status === "ok" ? `${r.rowCount} data row${r.rowCount === 1 ? "" : "s"}` : "—";
    const tabCell = r.status === "ok" ? (r.tabWasCreated ? "created" : "updated") : (r.error || "failed");
    summaryLines.push(`| ${r.name} | ${r.type} | ${downloadCell} | ${importCell} | ${rowsCell} | ${tabCell} |`);
  }
  appendStepSummary(summaryLines.join("\n"));

  if (results.some((r) => r.status !== "ok")) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
