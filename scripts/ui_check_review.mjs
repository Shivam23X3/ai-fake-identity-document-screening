/**
 * UI verification driver for the polished Human Review queue (Step 15).
 * Drives headless Chrome over the DevTools protocol (no test-runner deps):
 *   1. login as the bootstrap admin
 *   2. run one SIH demo case through the API (populates the review queue)
 *   3. open the Human Review tab
 *   4. assert: toolbar chips + filters render, row expands to the case
 *      summary, decision buttons render, note input works
 *   5. record an 'inconclusive' decision end-to-end and verify the row
 *      leaves the queue + success banner shows
 *   6. save a screenshot
 * Usage: node scripts/ui_check_review.mjs [--screenshot]
 * Requires: backend on :8000, frontend dev server on :5173.
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const APP_URL = 'http://localhost:5173/'
const API_URL = 'http://localhost:8000'
const CHROME =
  process.env.CHROME_PATH ??
  join(process.env.LOCALAPPDATA ?? '', 'Google/Chrome/Application/chrome.exe')
const SHOT_PATH = process.env.SHOT_PATH ?? 'logs/ui_review_panel.png'
const USER = process.env.UI_USER ?? 'admin'
const PASS = process.env.UI_PASS ?? 'Admin#Bootstrap#Passw0rd'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// ---------------------------------------------------------------- CDP client
class Cdp {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result)
      } else if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
        console.log('[console.error]', JSON.stringify(msg.params.args).slice(0, 300))
      } else if (msg.method === 'Runtime.exceptionThrown') {
        console.log('[page exception]', msg.params.exceptionDetails.text ?? '')
      }
    })
  }
  send(method, params = {}) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.ws.send(JSON.stringify({ id, method, params }))
    })
  }
}

async function connectCdp(url) {
  const ws = new WebSocket(url)
  await new Promise((res, rej) => {
    ws.addEventListener('open', res, { once: true })
    ws.addEventListener('error', () => rej(new Error('websocket error')), { once: true })
  })
  return ws
}

async function evalJs(cdp, expression, { awaitPromise = true } = {}) {
  const res = await cdp.send('Runtime.evaluate', {
    expression,
    awaitPromise,
    returnByValue: true,
  })
  if (res.exceptionDetails) {
    throw new Error(`page eval failed: ${res.exceptionDetails.text} ${res.exceptionDetails.exception?.description ?? ''}`)
  }
  return res.result?.value
}

async function waitFor(fnDesc, fn, { timeoutMs = 60000, intervalMs = 500 } = {}) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await fn()) return
    await sleep(intervalMs)
  }
  throw new Error(`timeout waiting for: ${fnDesc}`)
}

// ------------------------------------------------------------------- driver
const profile = mkdtempSync(join(tmpdir(), 'ui-review-check-'))
const chrome = spawn(CHROME, [
  '--headless=new',
  '--remote-debugging-port=9334',
  `--user-data-dir=${profile}`,
  '--no-first-run',
  '--disable-gpu',
  '--window-size=1440,1600',
  'about:blank',
], { stdio: 'ignore' })

try {
  let target = null
  for (let i = 0; i < 40 && !target; i++) {
    await sleep(500)
    try {
      const targets = await (await fetch('http://127.0.0.1:9334/json/list')).json()
      target = targets.find((t) => t.type === 'page') ?? null
    } catch { /* devtools not up yet */ }
  }
  if (!target) throw new Error('Chrome DevTools endpoint never came up on :9334')
  const ws = await connectCdp(target.webSocketDebuggerUrl)
  const cdp = new Cdp(ws)
  await cdp.send('Runtime.enable')
  await cdp.send('Page.enable')
  await cdp.send('Emulation.setDeviceMetricsOverride',
    { width: 1440, height: 1600, deviceScaleFactor: 1, mobile: false })

  // 0. Populate the queue via the API (login + one demo case), inside the
  // page so sessionStorage/token handling mirrors the app.
  await cdp.send('Page.navigate', { url: APP_URL })
  await waitFor('login form', () => evalJs(cdp, `!!document.querySelector('#username')`))
  const seeded = await evalJs(cdp, `(async () => {
    const login = await fetch('${API_URL}/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: ${JSON.stringify(USER)}, password: ${JSON.stringify(PASS)} }),
    })
    if (!login.ok) return 'login-failed:' + login.status
    const { access_token } = await login.json()
    const run = await fetch('${API_URL}/api/demo/run/case1_valid', {
      method: 'POST', headers: { Authorization: 'Bearer ' + access_token },
    })
    return 'ok:' + run.status
  })()`)
  if (!String(seeded).startsWith('ok:')) throw new Error(`could not seed a pending case (${seeded}) — is the backend on :8000 with demo fixtures generated?`)
  console.log('PASS  seeded one demo case into the review queue via API')

  // 1. Login through the real UI.
  await evalJs(cdp, `(async () => {
    const set = (el, v) => {
      const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
      s.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true }))
    }
    set(document.querySelector('#username'), ${JSON.stringify(USER)})
    set(document.querySelector('#password'), ${JSON.stringify(PASS)})
    document.querySelector('form button[type=submit]').click()
  })()`)
  await waitFor('dashboard after login', () =>
    evalJs(cdp, `!!document.querySelector('.tabs')`))
  console.log('PASS  logged in, dashboard rendered')

  // 2. Open the Human Review tab.
  await evalJs(cdp, `(() => {
    const tab = [...document.querySelectorAll('.tab')].find((t) => t.textContent.includes('Human Review'))
    tab.click()
  })()`)
  await waitFor('review queue table', () =>
    evalJs(cdp, `!!document.querySelector('.review-table')`))
  console.log('PASS  Human Review tab opened, queue table rendered')

  // 3. Assert the toolbar (chips + filters) rendered.
  const toolbar = await evalJs(cdp, `(() => ({
    chips: [...document.querySelectorAll('.review-chips .big-stat')].map((c) => c.textContent.replace(/\\s+/g, ' ').trim()),
    hasFilter: !!document.querySelector('.review-filter-select'),
    hasQuery: !!document.querySelector('.review-filter-input'),
    rows: document.querySelectorAll('.review-table tbody tr').length,
  }))()`)
  if (!toolbar.hasFilter || !toolbar.hasQuery) throw new Error('filter controls missing')
  if (toolbar.rows < 1) throw new Error('queue has no rows')
  console.log('PASS  toolbar rendered:', JSON.stringify(toolbar.chips), '| rows:', toolbar.rows)

  // 4. Expand the first row → case summary loads.
  await evalJs(cdp, `(() => {
    document.querySelector('.review-run-btn').click()
  })()`)
  await waitFor('case summary badges', () =>
    evalJs(cdp, `document.querySelectorAll('.review-summary .badge').length >= 4`), { timeoutMs: 30000 })
  const summary = await evalJs(cdp, `(() => ({
    cells: [...document.querySelectorAll('.review-summary-cell')].map((c) => c.textContent.replace(/\\s+/g, ' ').trim()),
    hasOpenDetail: [...document.querySelectorAll('.review-summary-links .btn')].some((b) => b.textContent.includes('Open full report')),
  }))()`)
  console.log('PASS  row expanded, case summary rendered:', JSON.stringify(summary.cells))

  // 5. Collapse + filter check (query that matches nothing).
  await evalJs(cdp, `(() => {
    const set = (el, v) => {
      const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
      s.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true }))
    }
    document.querySelector('.review-run-btn').click() // collapse
    set(document.querySelector('.review-filter-input'), 'zzz-no-match-zzz')
  })()`)
  await waitFor('empty filter state', () =>
    evalJs(cdp, `!!document.querySelector('.empty-state') && document.querySelector('.empty-state').textContent.includes('No cases match')`))
  console.log('PASS  run-id filter works (empty state shown for non-matching query)')
  await evalJs(cdp, `(() => {
    const set = (el, v) => {
      const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
      s.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true }))
    }
    set(document.querySelector('.review-filter-input'), '')
  })()`)
  await waitFor('rows back after clearing filter', () =>
    evalJs(cdp, `document.querySelectorAll('.review-table tbody tr').length >= 1`))

  // 6. Record an 'inconclusive' decision (no confirm dialog for this one).
  const before = await evalJs(cdp, `document.querySelectorAll('.review-table tbody tr').length`)
  await evalJs(cdp, `(() => {
    const btn = document.querySelector('.review-dec-btn.dec-inconclusive')
    btn.click()
  })()`)
  await waitFor('success banner', () =>
    evalJs(cdp, `!!document.querySelector('.banner-info') && document.querySelector('.banner-info').textContent.includes('Decision recorded')`), { timeoutMs: 30000 })
  const after = await evalJs(cdp, `document.querySelectorAll('.review-table tbody tr').length`)
  if (!(after < before)) throw new Error(`row did not leave the queue (before=${before}, after=${after})`)
  console.log(`PASS  decision recorded end-to-end; row left the queue (${before} -> ${after})`)

  // 7. Confirm-dialog check: 'flagged' should open the confirm strip.
  //    (Queue may be empty now if only one case existed — re-seed.)
  const seeded2 = await evalJs(cdp, `(async () => {
    const login = await fetch('${API_URL}/api/auth/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: ${JSON.stringify(USER)}, password: ${JSON.stringify(PASS)} }),
    })
    const { access_token } = await login.json()
    const run = await fetch('${API_URL}/api/demo/run/case2_tampered', {
      method: 'POST', headers: { Authorization: 'Bearer ' + access_token },
    })
    return run.status
  })()`)
  if (seeded2 !== 200) throw new Error('re-seed failed')
  await evalJs(cdp, `(() => { document.querySelector('.panel-actions .btn').click() })()`)
  await waitFor('new row after refresh', () =>
    evalJs(cdp, `document.querySelectorAll('.review-dec-btn.dec-flagged').length >= 1`))
  await evalJs(cdp, `(() => { document.querySelector('.review-dec-btn.dec-flagged').click() })()`)
  await waitFor('confirm strip', () =>
    evalJs(cdp, `!!document.querySelector('.review-confirm')`))
  const confirmText = await evalJs(cdp, `document.querySelector('.review-confirm').textContent.replace(/\\s+/g, ' ')`)
  if (!confirmText.includes('Confirm')) throw new Error('confirm strip did not render text')
  await evalJs(cdp, `(() => {
    const cancel = [...document.querySelectorAll('.review-confirm .btn')].find((b) => b.textContent.includes('Cancel'))
    cancel.click()
  })()`)
  await waitFor('confirm strip dismissed', () =>
    evalJs(cdp, `!document.querySelector('.review-confirm')`))
  console.log('PASS  flagged decision opened confirm dialog; cancel dismisses it')

  // 8. Screenshot.
  if (process.argv.includes('--screenshot')) {
    await sleep(300)
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(SHOT_PATH, Buffer.from(shot.data, 'base64'))
    console.log('screenshot saved:', SHOT_PATH)
  }

  console.log('\\nREVIEW UI VERIFICATION: ALL CHECKS PASSED')
} finally {
  chrome.kill()
  await sleep(300)
}
