/**
 * RSF Usage Ledger — Apps Script webhook bound to a Google Sheet.
 *
 * This is the real integration code from the production app, included as-is
 * because it's already fully parameterized: no hardcoded sheet, no real
 * token, nothing production-specific. See docs/architecture.md for how this
 * fits into the rest of the system.
 *
 * One-time setup:
 *   1. Open the Google Sheet → Extensions → Apps Script.
 *   2. Paste this whole file over the default Code.gs. Replace TOKEN below with a
 *      long random string (e.g. run `openssl rand -hex 24` in Terminal).
 *   3. Project Settings (gear icon) → Script Properties → Add script property:
 *        key = SHEET_ID, value = your sheet's ID (from its URL). Keeping this
 *        out of the source file itself means the committed code has no
 *        hardcoded reference to which specific sheet it talks to.
 *   4. Deploy → New deployment → type "Web app".
 *        Execute as: Me.   Who has access: Anyone.
 *      Copy the resulting /exec URL.
 *   5. In Render → your RSF service → Environment, set:
 *        RSF_LEDGER_URL   = the /exec URL
 *        RSF_LEDGER_TOKEN = the same random string as TOKEN
 *
 * The "RSF Usage Ledger" tab and its header are created automatically on the
 * first write — you don't need to make the tab by hand.
 *
 * Updating an already-deployed script (e.g. adding new columns):
 *   1. Paste the updated code over the existing Code.gs (keep your real TOKEN).
 *      SHEET_ID lives in Script Properties, not source, so pasting new code
 *      never touches it.
 *   2. Deploy → Manage deployments → edit the existing deployment (pencil icon)
 *      → Version: New version → Deploy. This keeps the same /exec URL, so
 *      Render's RSF_LEDGER_URL doesn't need to change.
 *   3. sheet_() only writes a header row when the tab is brand new or empty —
 *      it will NOT retroactively add columns to an existing header row. If
 *      HEADERS gained new entries, manually add those column names to the end
 *      of row 1 in the live sheet yourself, in the same order as HEADERS
 *      below. New writes will then land in the right columns; old rows simply
 *      have those cells blank.
 *
 * --------------------------------------------------------------------------
 * Public-usage rate limiter (added 2026-09-05) — this SAME script/deployment
 * now also answers the RSF server's "has this visitor used up today's
 * allowance?" check, so there's only one Apps Script webhook to maintain,
 * not two.
 *
 *   - Storage is Script Properties (PropertiesService), NOT a Sheet row per
 *     hit — a per-visitor counter needs fast key lookup and in-place
 *     pruning, which Script Properties does cleanly; a Sheet would mean an
 *     ever-growing, ever-scanned table. Nothing here touches the "RSF Usage
 *     Ledger" tab or its rows.
 *   - RSF's server sends only a keyed HASH of the visitor's IP (never the
 *     raw IP) plus the limit/window it wants enforced — this script owns no
 *     policy (15/day, etc.), only the atomic counting mechanism.
 *   - LockService.getScriptLock() serializes the read-modify-write per
 *     request so two near-simultaneous hits from the same visitor can't both
 *     read the same "count so far" and both be allowed through.
 *   - This call is in RSF's live request path (unlike the fire-and-forget
 *     ledger write), so RSF's server uses a short timeout and fails OPEN on
 *     any error here — a problem with this script degrades to "no rate
 *     limiting right now," never to "RSF is down."
 *   - Redeploying this file (Manage deployments → New version, same steps as
 *     above) is all that's needed to activate this — no new Render env vars;
 *     it reuses RSF_LEDGER_URL/RSF_LEDGER_TOKEN.
 */

const TOKEN = 'CHANGE_ME_to_a_long_random_string';
const TAB = 'RSF Usage Ledger';
const HEADERS = [
  'timestamp', 'analysis_id', 'rsf_version', 'beta_user', 'company', 'role_title',
  'source_url', 'score', 'grade', 'bucket', 'recommendation', 'applied',
  'user_agreed', 'user_action', 'written_feedback', 'silent_usage', 'outcome',
  'notes', 'entry_type', 'confidence', 'source',
  'resume_attached', 'profile_completeness', 'optional_fields_count'
];

// The sheet ID lives in Script Properties (Project Settings → Script
// Properties), not in source — see the setup notes above. Using openById
// works whether this script is bound to the sheet or a standalone project.
function sheet_() {
  const sheetId = PropertiesService.getScriptProperties().getProperty('SHEET_ID');
  if (!sheetId) {
    throw new Error('SHEET_ID script property is not set — see setup notes at the top of this file.');
  }
  const ss = SpreadsheetApp.openById(sheetId);
  let sh = ss.getSheetByName(TAB);
  if (!sh) { sh = ss.insertSheet(TAB); sh.appendRow(HEADERS); }
  if (sh.getLastRow() === 0) sh.appendRow(HEADERS);
  return sh;
}

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (!TOKEN || TOKEN === 'CHANGE_ME_to_a_long_random_string' || body.token !== TOKEN) {
      return json_({ ok: false, error: 'unauthorized' });
    }
    if (body.action === 'rate_limit_check') {
      return json_(checkRateLimit_(body));
    }
    const r = body.row || {};
    sheet_().appendRow(HEADERS.map(function (h) {
      return (r[h] !== undefined && r[h] !== null) ? r[h] : '';
    }));
    return json_({ ok: true });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

// Prefix for rate-limit keys in Script Properties, so they're visually
// distinguishable from any other properties this script might ever hold.
const RATE_LIMIT_PROP_PREFIX = 'rl_';

/**
 * Atomically: does this hashed visitor have room left in the given rolling
 * window? If yes, record this hit and say so; if no, say so without
 * recording anything new. `body` carries only a one-way hash (hashed_ip),
 * never a raw IP, and the limit/window the caller wants enforced -- this
 * function has no opinion on policy, only on counting correctly under
 * concurrent calls.
 */
function checkRateLimit_(body) {
  const hashedIp = String(body.hashed_ip || '');
  if (!hashedIp) return { ok: false, error: 'missing hashed_ip' };
  const limit = Number(body.limit) || 15;
  const windowMs = (Number(body.window_seconds) || 86400) * 1000;
  const now = Number(body.now_ms) || Date.now();
  const cutoff = now - windowMs;

  const lock = LockService.getScriptLock();
  lock.waitLock(10000); // throws if not acquired within 10s
  try {
    const props = PropertiesService.getScriptProperties();
    const key = RATE_LIMIT_PROP_PREFIX + hashedIp;
    let hits = [];
    const raw = props.getProperty(key);
    if (raw) {
      try {
        hits = JSON.parse(raw).filter(function (t) { return t > cutoff; });
      } catch (parseErr) {
        hits = []; // corrupt value -- treat as no history rather than fail closed
      }
    }
    const allowed = hits.length < limit;
    if (allowed) hits.push(now);
    props.setProperty(key, JSON.stringify(hits));
    return { ok: true, allowed: allowed, count: hits.length };
  } finally {
    lock.releaseLock();
  }
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
    .setMimeType(ContentService.MimeType.JSON);
}
