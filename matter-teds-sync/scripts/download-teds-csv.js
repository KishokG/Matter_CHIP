/**
 * download-teds-csv.js
 *
 * Manual single-table tester — downloads ONE table's CSV export using env
 * vars directly, without touching config/releases.json. Useful for testing
 * a single release/table before adding it to the config.
 *
 * REQUIRED ENV VARS:
 *   KNACK_APP_URL      - base URL of the Knack app
 *   KNACK_TABLE_URL    - full URL of the specific view/table
 *   KNACK_USERNAME     - login email/username
 *   KNACK_PASSWORD     - login password
 *   KNACK_TABLE_HEADING - (optional) heading text above the table, defaults to "Anonymized DUTs"
 *
 * For syncing multiple releases on a schedule, use sync-all.js + config/releases.json instead.
 */

const path = require("path");
const { chromium } = require("playwright");
const { loginToKnack, exportTableCsv } = require("./lib/knack");

const APP_URL = process.env.KNACK_APP_URL;
const TABLE_URL = process.env.KNACK_TABLE_URL;
const USERNAME = process.env.KNACK_USERNAME;
const PASSWORD = process.env.KNACK_PASSWORD;
const TABLE_HEADING = process.env.KNACK_TABLE_HEADING || "Anonymized DUTs";
const OUTPUT_PATH = path.join(__dirname, "teds-export.csv");

if (!APP_URL || !TABLE_URL || !USERNAME || !PASSWORD) {
  console.error(
    "Missing one or more required env vars: KNACK_APP_URL, KNACK_TABLE_URL, KNACK_USERNAME, KNACK_PASSWORD"
  );
  process.exit(1);
}

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();

  try {
    await loginToKnack(page, { appUrl: APP_URL, username: USERNAME, password: PASSWORD });
    await exportTableCsv(page, context, {
      tableUrl: TABLE_URL,
      tableHeading: TABLE_HEADING,
      username: USERNAME,
      password: PASSWORD,
      outputPath: OUTPUT_PATH,
      debugDir: __dirname,
    });
  } finally {
    await browser.close();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
