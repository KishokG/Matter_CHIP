/**
 * lib/tcsupport.js
 *
 * Builds the "TC ID | Number of DUTs supported" table: one row per active
 * TCID (from the "Matter Active TCIDs" export), with the number of DUTs
 * whose PICS indicated support for it (from tclist_matter*.csv).
 */

const TC_ID_PATTERN = /\[(TC-[^\]]+)\]/;

// "[TC-ACE-2.1] Attribute read privilege ..." -> "TC-ACE-2.1"; bare IDs pass through.
function extractTcId(value) {
  const text = String(value || "").trim();
  const match = text.match(TC_ID_PATTERN);
  if (match) return match[1].trim();
  return text.startsWith("TC-") ? text : "";
}

function columnIndex(header, name, fallback) {
  const idx = header.findIndex((h) => String(h).trim().toLowerCase() === name.toLowerCase());
  return idx === -1 ? fallback : idx;
}

/**
 * activeRows / tclistRows: parsed CSV rows, header row first.
 * Active TCIDs with no entry in tclist (no DUT declared PICS support) get 0.
 */
function buildTcSupportRows(activeRows, tclistRows) {
  const [tclistHeader = [], ...tclistData] = tclistRows;
  const tcidIdx = columnIndex(tclistHeader, "tcid", 0);
  const countIdx = columnIndex(tclistHeader, "count", 2);
  const supportCounts = new Map();
  for (const row of tclistData) {
    const tcId = extractTcId(row[tcidIdx]);
    if (tcId) supportCounts.set(tcId, parseInt(row[countIdx], 10) || 0);
  }

  const [activeHeader = [], ...activeData] = activeRows;
  const activeIdx = columnIndex(activeHeader, "Matter TCID", 0);
  const seen = new Set();
  const rows = [["TC ID", "Number of DUTs supported"]];
  let missing = 0;
  for (const row of activeData) {
    const tcId = extractTcId(row[activeIdx]);
    if (!tcId || seen.has(tcId)) continue;
    seen.add(tcId);
    if (!supportCounts.has(tcId)) missing++;
    rows.push([tcId, supportCounts.get(tcId) || 0]);
  }

  console.log(
    `Matched ${rows.length - 1 - missing} of ${rows.length - 1} active TCIDs in the tclist; ${missing} have no supporting DUT (0).`
  );
  return rows;
}

module.exports = { extractTcId, buildTcSupportRows };
