/**
 * Sellbird fetch ledger: Google Sheet web app for `sellbird fetch`.
 *
 * Setup (once): open the sheet, Extensions > Apps Script, paste this file,
 * set TOKEN below to a long random string, then Deploy > New deployment >
 * Web app (Execute as: Me, Who has access: Anyone). Put the web app URL in
 * SHEET_WEBHOOK_URL and the same TOKEN in SHEET_TOKEN.
 *
 * POST {token, action: 'load'}    -> {seen_ids, seen_orgs, skipped_orgs, credits_used, last_segment}
 * POST {token, columns, rows, run} -> appends prospects and a run record
 * POST {token, angles: {id: text}} -> fills the angle column for those rows
 *
 * The token travels only in the POST body, never in the URL.
 */

const TOKEN = 'CHANGE-ME-TO-A-LONG-RANDOM-STRING';
const PROSPECTS = 'Prospects';
const RUNS = 'Runs';
const RUN_COLUMNS = ['ran_at', 'segment', 'searched', 'enriched', 'credits_used',
                     'prospects', 'seen_ids', 'skipped_orgs'];

function sheet_(name, columns) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(name);
  // The sheet starts life with one tab: that tab becomes Prospects.
  if (!sh && name === PROSPECTS && ss.getSheets().length === 1 &&
      !ss.getSheetByName(RUNS)) {
    sh = ss.getSheets()[0].setName(PROSPECTS);
  }
  if (!sh) sh = ss.insertSheet(name);
  if (columns && sh.getLastRow() === 0) {
    sh.appendRow(columns);
    sh.setFrozenRows(1);
  } else if (columns) {
    // Columns added since the sheet was made go on the end of the header.
    const head = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
    const missing = columns.filter(c => head.indexOf(c) < 0);
    if (missing.length) sh.getRange(1, head.length + 1, 1, missing.length).setValues([missing]);
  }
  return sh;
}

function records_(sh) {
  if (sh.getLastRow() < 2) return [];
  const values = sh.getDataRange().getValues();
  const head = values[0];
  return values.slice(1).map(r => Object.fromEntries(head.map((h, i) => [h, r[i]])));
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
      .setMimeType(ContentService.MimeType.JSON);
}

function doGet(e) {
  return json_({ok: true, note: 'POST only'});
}

function load_() {
  const prospects = records_(sheet_(PROSPECTS));
  const runs = records_(sheet_(RUNS, RUN_COLUMNS));
  const ids = new Set(prospects.map(p => String(p.apollo_id)).filter(String));
  runs.forEach(r => String(r.seen_ids || '').split(',').filter(String).forEach(i => ids.add(i)));
  return json_({
    seen_ids: Array.from(ids),
    seen_orgs: Array.from(new Set(prospects.map(p => String(p.company)).filter(String))),
    // Companies a reviewer marked skip in a plan: never offered again.
    skipped_orgs: Array.from(new Set(runs.flatMap(r => String(r.skipped_orgs || '').split('|')).filter(String))),
    credits_used: runs.reduce((n, r) => n + (Number(r.credits_used) || 0), 0),
    last_segment: runs.length ? String(runs[runs.length - 1].segment) : '',
  });
}

function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  if (body.token !== TOKEN) return json_({error: 'unauthorized'});
  if (body.action === 'load') return load_();
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    if (body.rows && body.columns) {
      const sh = sheet_(PROSPECTS, body.columns);
      const head = sh.getRange(1, 1, 1, sh.getLastColumn()).getValues()[0];
      const out = body.rows.map(r => head.map(h => (r[h] === undefined ? '' : r[h])));
      if (out.length) sh.getRange(sh.getLastRow() + 1, 1, out.length, head.length).setValues(out);
    }
    if (body.run) {
      sheet_(RUNS, RUN_COLUMNS).appendRow(RUN_COLUMNS.map(c => body.run[c] === undefined ? '' : body.run[c]));
    }
    if (body.angles) {
      const sh = sheet_(PROSPECTS);
      const values = sh.getDataRange().getValues();
      const head = values[0];
      const idCol = head.indexOf('apollo_id'), angleCol = head.indexOf('angle');
      if (idCol >= 0 && angleCol >= 0) {
        for (let i = 1; i < values.length; i++) {
          const angle = body.angles[String(values[i][idCol])];
          if (angle) sh.getRange(i + 1, angleCol + 1).setValue(angle);
        }
      }
    }
    return json_({ok: true});
  } finally {
    lock.releaseLock();
  }
}
