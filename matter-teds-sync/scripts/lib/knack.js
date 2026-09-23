/**
 * lib/knack.js
 *
 * Reusable helpers: log into the Knack app once, then export any number of
 * tables (by URL + heading) within that same authenticated session.
 */

const fs = require("fs");

async function loginIfPresent(page, { username, password }) {
  const emailField = page.locator(
    'input[type="email"], input[name*="email" i], input[id*="email" i], input[name*="username" i]'
  ).first();
  if (await emailField.count()) {
    const passwordField = page.locator('input[type="password"]').first();
    const loginButton = page.locator(
      'button:has-text("Log In"), button:has-text("Sign In"), input[type="submit"]'
    ).first();
    await emailField.fill(username);
    await passwordField.fill(password);
    await Promise.all([
      page.waitForLoadState("networkidle"),
      loginButton.click(),
    ]);
    return true;
  }
  return false;
}

async function loginToKnack(page, { appUrl, username, password }) {
  console.log(`Navigating to ${appUrl}`);
  await page.goto(appUrl, { waitUntil: "networkidle" });
  const loggedIn = await loginIfPresent(page, { username, password });
  console.log(loggedIn ? "Login form detected, signed in." : "No login form detected — assuming already authenticated.");
}

function waitForAnyDownload(page, context, timeout) {
  const onSamePage = page.waitForEvent("download", { timeout }).catch(() => null);
  const onNewPage = context
    .waitForEvent("page", { timeout })
    .then((newPage) => newPage.waitForEvent("download", { timeout }).catch(() => null))
    .catch(() => null);
  return Promise.race([onSamePage, onNewPage]).then((d) => d || null);
}

// Knack keeps background requests going on some pages, so "networkidle" is
// only waited for briefly — callers wait for the element they need anyway.
async function gotoAndSettle(page, url) {
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 15000 }).catch(() => {});
}

async function gotoWithLogin(page, url, { username, password }) {
  // A URL differing only after "#" is a same-document navigation in Knack's
  // single-page app (e.g. two tables on one test-event page), which doesn't
  // reliably re-render or close open popups — load it fresh instead.
  if (page.url().split("#")[0] === url.split("#")[0]) {
    await page.goto("about:blank");
  }
  await gotoAndSettle(page, url);

  // Some Knack apps gate per-page, so re-check for a login form here too.
  const reLoggedIn = await loginIfPresent(page, { username, password });
  if (reLoggedIn) {
    await gotoAndSettle(page, url);
  }
}

/**
 * Navigates to a table's URL and exports it as CSV, saving to outputPath.
 * Assumes the page/context is already logged in (call loginToKnack first).
 */
async function exportTableCsv(page, context, { tableUrl, tableHeading, username, password, outputPath, debugDir }) {
  console.log(`Navigating to table view: ${tableUrl}`);
  await gotoWithLogin(page, tableUrl, { username, password });

  console.log(`Looking for the "${tableHeading}" table...`);
  const heading = page.locator(`:text-is("${tableHeading}")`).first();
  await heading.waitFor({ state: "visible", timeout: 30000 });
  await heading.scrollIntoViewIfNeeded();

  const exportButton = heading.locator(
    'xpath=following::*[self::button or self::a][contains(translate(normalize-space(string(.)), "EXPORT", "export"), "export")][1]'
  );
  await exportButton.waitFor({ state: "visible", timeout: 30000 });
  console.log(`Clicking "Export" for the "${tableHeading}" table...`);

  const firstDownload = waitForAnyDownload(page, context, 15000);
  await exportButton.click();
  let download = await firstDownload;

  if (!download) {
    if (debugDir) {
      await page.screenshot({ path: `${debugDir}/debug-after-export-click.png`, fullPage: true }).catch(() => {});
    }
    const exportModal = page.locator('[role="dialog"]:has-text("Export Data"), div:has-text("Export Data")').first();
    const modalVisible = await exportModal.isVisible().catch(() => false);

    const candidates = modalVisible
      ? [
          exportModal.locator('a:has-text("Commas")'),
          exportModal.locator('a:has-text(".csv")'),
          exportModal.getByRole("link", { name: /csv/i }),
        ]
      : [
          page.locator('a:has-text("Commas (.csv)")'),
          page.locator('a:has-text(".csv")'),
          page.getByRole("link", { name: /csv/i }).first(),
        ];

    for (const candidate of candidates) {
      if (await candidate.count().catch(() => 0)) {
        console.log("Trying the Commas (.csv) export link...");
        const nextDownload = waitForAnyDownload(page, context, 20000);
        await candidate.first().click().catch(() => {});
        download = await nextDownload;
        if (download) break;
      }
    }
  }

  if (!download) {
    if (debugDir) {
      await page.screenshot({ path: `${debugDir}/debug-screenshot.png`, fullPage: true }).catch(() => {});
    }
    throw new Error(
      `Could not trigger a CSV download for table "${tableHeading}" at ${tableUrl}.`
    );
  }

  await download.saveAs(outputPath);
  console.log(`CSV saved to ${outputPath}`);
}

function looksLikeTclist(buffer) {
  const head = buffer.slice(0, 200).toString("utf8").replace(/^\uFEFF/, "").trim().toLowerCase();
  return head.startsWith("tcid");
}

/**
 * Downloads the TCList CSV for one event from the "Test Events TCList"
 * table (columns: Event | TCList CSV | TCList JSON), saving to outputPath.
 * The row is picked by an exact match on the Event cell, so it keeps
 * working when the record-id part of the filename changes.
 */
async function downloadTclistCsv(page, context, { pageUrl, tclistHeading, event, username, password, outputPath, debugDir }) {
  console.log(`Navigating to ${pageUrl}`);
  await gotoWithLogin(page, pageUrl, { username, password });

  const fail = async (message) => {
    if (debugDir) {
      await page.screenshot({ path: `${debugDir}/debug-tclist.png`, fullPage: true }).catch(() => {});
    }
    throw new Error(message);
  };

  console.log(`Looking for the "${event}" row in the "${tclistHeading}" table...`);
  const heading = page.locator(`:text-is("${tclistHeading}")`).first();
  await heading.waitFor({ state: "visible", timeout: 30000 }).catch(() => {});
  if (!(await heading.isVisible().catch(() => false))) {
    await fail(`Heading "${tclistHeading}" not found at ${pageUrl}.`);
  }
  const table = heading.locator("xpath=following::table[1]");
  await table.locator("tbody tr").first().waitFor({ state: "visible", timeout: 30000 }).catch(() => {});

  let csvLink = null;
  const events = [];
  for (const row of await table.locator("tbody tr").all()) {
    const rowEvent = (await row.locator("td").first().innerText().catch(() => "")).trim();
    events.push(rowEvent);
    if (rowEvent === event) {
      csvLink = row.locator("a").filter({ hasText: /\.csv\s*$/i }).first();
      break;
    }
  }
  if (!csvLink || !(await csvLink.count())) {
    await fail(`No TCList CSV for event "${event}" in "${tclistHeading}". Events listed: ${events.join(", ") || "none"}.`);
  }
  const fileName = (await csvLink.innerText()).trim();
  console.log(`Found ${fileName}`);

  // The link's href points at the stored file, so try fetching it within the
  // logged-in context first; clicking only opens Knack's file preview.
  const href = await csvLink.getAttribute("href");
  if (href && /^https?:|^\//.test(href)) {
    const response = await context.request.get(new URL(href, page.url()).toString()).catch(() => null);
    const body = response && response.ok() ? await response.body() : null;
    if (body && looksLikeTclist(body)) {
      fs.writeFileSync(outputPath, body);
      console.log(`${fileName} saved to ${outputPath}`);
      return;
    }
    console.log("Direct fetch of the link didn't return the CSV — using the file preview's Download button.");
  }

  // Fallback: the click may download straight away, or open the preview
  // whose "Download" button does.
  let download = await (async () => {
    const pending = waitForAnyDownload(page, context, 10000);
    await csvLink.click();
    return pending;
  })();
  if (!download) {
    const downloadButton = page
      .locator('a:has-text("Download"), button:has-text("Download")')
      .filter({ hasNotText: fileName })
      .last();
    if (await downloadButton.isVisible().catch(() => false)) {
      const pending = waitForAnyDownload(page, context, 20000);
      await downloadButton.click();
      download = await pending;
    }
  }
  if (!download) {
    await fail(`Could not download ${fileName} for event "${event}".`);
  }
  await download.saveAs(outputPath);
  if (!looksLikeTclist(fs.readFileSync(outputPath))) {
    await fail(`${fileName} was downloaded but doesn't look like a TCList CSV (expected a "tcid" header).`);
  }
  console.log(`${fileName} saved to ${outputPath}`);
}

module.exports = { loginToKnack, exportTableCsv, downloadTclistCsv };
