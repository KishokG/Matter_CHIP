/**
 * lib/knack.js
 *
 * Reusable helpers: log into the Knack app once, then export any number of
 * tables (by URL + heading) within that same authenticated session.
 */

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

/**
 * Navigates to a table's URL and exports it as CSV, saving to outputPath.
 * Assumes the page/context is already logged in (call loginToKnack first).
 */
async function exportTableCsv(page, context, { tableUrl, tableHeading, username, password, outputPath, debugDir }) {
  console.log(`Navigating to table view: ${tableUrl}`);
  await page.goto(tableUrl, { waitUntil: "networkidle" });

  // Some Knack apps gate per-page, so re-check for a login form here too.
  const reLoggedIn = await loginIfPresent(page, { username, password });
  if (reLoggedIn) {
    await page.goto(tableUrl, { waitUntil: "networkidle" });
  }

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

module.exports = { loginToKnack, exportTableCsv };
