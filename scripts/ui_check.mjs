/**
 * UI verification driver for the Step-5 validation panel.
 * Drives headless Chrome over the DevTools protocol (no test-runner deps):
 *   1. login as a demo operator
 *   2. upload data/samples/sample_passport.png (doc type passport)
 *   3. wait for the analyze pipeline to finish
 *   4. assert the Document Validation panel renders the real checks
 *   5. dump panel text + save a full-page screenshot
 * Usage: node scripts/ui_check.mjs [--screenshot]
 */
import { spawn } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const APP_URL = 'http://localhost:5173/'
const CHROME =
  process.env.CHROME_PATH ??
  join(process.env.LOCALAPPDATA ?? '', 'Google/Chrome/Application/chrome.exe')
const SAMPLE = process.env.SAMPLE_PATH
  ? new URL(`../${process.env.SAMPLE_PATH}`, import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')
  : new URL('../data/samples/sample_passport.png', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')
const SAMPLE_NAME = process.env.SAMPLE_PATH?.split('/').pop() ?? 'sample_passport.png'
const DOC_TYPE = process.env.DOC_TYPE ?? 'passport'
const EXPECT_INVALID = process.env.EXPECT_INVALID === '1'
const SHOT_PATH = process.env.SHOT_PATH ?? 'logs/ui_validation_panel.png'

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
  send(method, params = {}, sessionId) {
    const id = ++this.id
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      wsSend(this.ws, JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }))
    })
  }
}

/** Node's global WebSocket (undici) supports string payloads only. */
function wsSend(ws, str) {
  ws.send(str)
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
const profile = mkdtempSync(join(tmpdir(), 'ui-check-'))
const chrome = spawn(CHROME, [
  '--headless=new',
  '--remote-debugging-port=9333',
  `--user-data-dir=${profile}`,
  '--no-first-run',
  '--disable-gpu',
  '--window-size=1440,2400',
  'about:blank',
], { stdio: 'ignore' })

try {
  // Connect to the page target (Chrome needs a few seconds to boot).
  let target = null
  for (let i = 0; i < 40 && !target; i++) {
    await sleep(500)
    try {
      const targets = await (await fetch('http://127.0.0.1:9333/json/list')).json()
      target = targets.find((t) => t.type === 'page') ?? null
    } catch {
      /* devtools endpoint not up yet */
    }
  }
  if (!target) throw new Error('Chrome DevTools endpoint never came up on :9333')
  const ws = await connectCdp(target.webSocketDebuggerUrl)
  const cdp = new Cdp(ws)
  await cdp.send('Runtime.enable')
  await cdp.send('Page.enable')
  await cdp.send('Emulation.setDeviceMetricsOverride',
    { width: 1440, height: 2400, deviceScaleFactor: 1, mobile: false })

  // 1. Open the app → login screen.
  await cdp.send('Page.navigate', { url: APP_URL })
  await waitFor('login form', () =>
    evalJs(cdp, `!!document.querySelector('#username')`))
  console.log('PASS  login screen rendered')

  // Login: health check runs against the proxied backend.
  await evalJs(cdp, `(async () => {
    const set = (el, v) => {
      const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
      s.call(el, v); el.dispatchEvent(new Event('input', { bubbles: true }))
    }
    set(document.querySelector('#username'), 'ui.check')
    document.querySelector('form button[type=submit]').click()
  })()`)
  await waitFor('dashboard after login', () =>
    evalJs(cdp, `!!document.querySelector('#doc-type')`))
  console.log('PASS  logged in, dashboard rendered (backend reachable)')

  // 2. Upload the passport sample via the real file input.
  const data = readFileSync(SAMPLE).toString('base64')
  const uploaded = await evalJs(cdp, `(async () => {
    const b64 = ${JSON.stringify(data)}
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0))
    const file = new File([bytes], ${JSON.stringify(SAMPLE_NAME)}, { type: 'image/png' })
    const dt = new DataTransfer(); dt.items.add(file)
    const input = document.querySelector('#file-input')
    input.files = dt.files
    input.dispatchEvent(new Event('change', { bubbles: true }))
    const select = document.querySelector('#doc-type')
    if (select && ${JSON.stringify(DOC_TYPE)} !== 'passport') {
      const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set
      setter.call(select, ${JSON.stringify(DOC_TYPE)})
      select.dispatchEvent(new Event('change', { bubbles: true }))
    }
    await new Promise((r) => setTimeout(r, 200))
    document.querySelector('.btn-primary.btn-block').click()
    return true
  })()`)
  console.log('PASS  upload submitted:', uploaded)

  // 3. Wait for the pipeline to finish → validation panel appears.
  await waitFor('Document Validation panel with checks', async () => {
    return evalJs(cdp, `(() => {
      const h2s = [...document.querySelectorAll('.panel-title')]
      const v = h2s.find((h) => h.textContent.includes('Document Validation'))
      return !!v && document.querySelectorAll('.check').length > 0
    })()`)
  }, { timeoutMs: 90000 })
  console.log('PASS  analysis finished, validation checks rendered')

  // 4. Dump the validation panel state.
  const result = await evalJs(cdp, `(() => {
    const panels = [...document.querySelectorAll('.panel')]
    const panel = panels.find((p) =>
      p.querySelector('.panel-title')?.textContent.includes('Document Validation'))
    const badge = panel.querySelector('.validity-row .badge')?.textContent ?? null
    const summary = panel.querySelector('.validity-row .hint')?.textContent ?? null
    const checks = [...panel.querySelectorAll('.check')].map((li) => ({
      status: li.className.includes('check-pass') ? 'PASS'
        : li.className.includes('check-fail') ? 'FAIL' : 'WARNING',
      field: li.querySelector('strong')?.textContent ?? '',
      message: li.querySelector('p')?.textContent ?? '',
    }))
    const conf = panel.querySelector('.confidence-label')?.textContent ?? null
    const notes = [...panel.querySelectorAll('.mini-list li')].map((li) => li.textContent)
    const pipeline = [...document.querySelectorAll('.pipe-row')].map((r) => r.textContent.trim().replace(/\\s+/g, ' '))
    return { badge, summary, conf, checks, notes, pipeline }
  })()`)

  console.log('\n=== Document Validation panel (as rendered) ===')
  console.log('badge:      ', result.badge)
  console.log('summary:    ', result.summary)
  console.log('confidence: ', result.conf)
  console.log('pipeline:   ', result.pipeline.join(' | '))
  console.log(`checks (${result.checks.length}):`)
  for (const c of result.checks) console.log(`  [${c.status.padEnd(7)}] ${c.field.padEnd(18)} ${c.message.slice(0, 90)}`)
  console.log('notes:')
  for (const n of result.notes) console.log('  -', n.slice(0, 110))

  // Assertions.
  const fails = result.checks.filter((c) => c.status === 'FAIL')
  const warns = result.checks.filter((c) => c.status === 'WARNING')
  if (EXPECT_INVALID) {
    if (!result.badge?.includes('INVALID')) throw new Error('expected INVALID badge')
    if (fails.length === 0) throw new Error('expected FAIL checks on the garbage sample')
    console.log(`\nPASS  badge = INVALID, FAIL checks rendered: ${fails.length} (warnings: ${warns.length})`)
  } else {
    if (!result.badge?.includes('VALID')) throw new Error('validity badge missing/invalid')
    if (fails.length !== 0) throw new Error(`unexpected FAIL checks: ${JSON.stringify(fails)}`)
    if (result.checks.length < 10) throw new Error('too few checks rendered')
    if (!result.pipeline.join(' ').includes('document_validation')) throw new Error('pipeline row missing')
    console.log('\nPASS  badge = VALID, no FAIL checks, checks rendered:', result.checks.length,
      `(warnings: ${warns.length})`)
  }

  // 5. Screenshots: full page + validation panel close-up.
  if (process.argv.includes('--screenshot')) {
    await cdp.send('Emulation.setDeviceMetricsOverride',
      { width: 1440, height: 1200, deviceScaleFactor: 1, mobile: false })
    await sleep(300)
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(SHOT_PATH, Buffer.from(shot.data, 'base64'))
    console.log('screenshot saved:', SHOT_PATH)

    const panelBox = await evalJs(cdp, `(() => {
      const panels = [...document.querySelectorAll('.panel')]
      const p = panels.find((x) => x.querySelector('.panel-title')?.textContent.includes('Document Validation'))
      p.scrollIntoView({ block: 'start' })
      const r = p.getBoundingClientRect()
      return { x: r.x, y: r.y, width: r.width, height: Math.min(r.height, 1400) }
    })()`)
    await sleep(300)
    const clip = { ...panelBox, scale: 1 }
    const shot2 = await cdp.send('Page.captureScreenshot', { format: 'png', clip })
    writeFileSync(SHOT_PATH.replace('.png', '_only.png'), Buffer.from(shot2.data, 'base64'))
    console.log('screenshot saved:', SHOT_PATH.replace('.png', '_only.png'))
  }

  console.log('\nUI VERIFICATION: ALL CHECKS PASSED')
} finally {
  chrome.kill()
  await sleep(300)
}
