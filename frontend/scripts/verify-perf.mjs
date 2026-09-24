// 性能专项验证：星图帧间隔 + 拖拽跟手 + 表格视图打开后的主线程余量（mini 降帧）
// 全部用 rAF 时间戳/长任务计数断言，不读任何截图。
// 用法：node scripts/verify-perf.mjs [--baseline]（--baseline 对比 HEAD 版本，输出两列）
import { chromium } from '@playwright/test'
// --big：预建 150 表 SQLite 连接（复现真实大库场景；默认走演示库 14 表）
const BIG = process.argv.includes('--big')
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = BIG ? '/tmp/tabletalk-perf-big' : '/tmp/tabletalk-perf'
const port = BIG ? 8774 : 8773
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const py = fs.existsSync(path.join(backendDir, '.venv/bin/python')) ? path.join(backendDir, '.venv/bin/python') : 'python3'
const sidecar = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
  cwd: backendDir,
  env: { ...process.env, TABLETALK_DATA_DIR: dataDir },
  stdio: 'ignore',
})

async function waitHealth(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try { const r = await fetch(`${baseUrl}/api/v1/health`); if (r.ok) return } catch {}
    await new Promise((res) => setTimeout(res, 500))
  }
  throw new Error('health timeout')
}

let pass = 0, fail = 0
function check(name, cond, got) {
  if (cond) { pass++; console.log(`PASS ${name} -> ${got}`) }
  else { fail++; console.log(`FAIL ${name} -> ${got}`) }
}

// 页内采集：连续 nFrames 帧的 rAF 间隔 + 长任务（>50ms）计数
const COLLECT = (nFrames) => new Promise((resolve) => {
  const ts = []
  let longTasks = 0
  let po
  try {
    po = new PerformanceObserver((l) => { longTasks += l.getEntries().length })
    po.observe({ entryTypes: ['longtask'] })
  } catch {}
  const cb = (t) => {
    ts.push(t)
    if (ts.length < nFrames) requestAnimationFrame(cb)
    else { try { po?.disconnect() } catch {}; resolve({ ts, longTasks }) }
  }
  requestAnimationFrame(cb)
})

function stats(ts) {
  const d = []
  for (let i = 1; i < ts.length; i++) d.push(ts[i] - ts[i - 1])
  const spikes = d.filter((x) => x > 100).length
  const trimmed = d.filter((x) => x <= 100)
  d.sort((a, b) => a - b)
  const mean = trimmed.reduce((s, x) => s + x, 0) / (trimmed.length || 1)
  const p95 = d[Math.floor(d.length * 0.95)]
  const max = d[d.length - 1]
  const janky = d.filter((x) => x > 25).length
  return { mean, p95, max, janky, n: d.length, spikes }
}

async function measure(page, nFrames = 240) {
  const r = await page.evaluate(COLLECT, nFrames)
  return { ...stats(r.ts), longTasks: r.longTasks }
}

// 拖拽模拟：在画布上以 ~120move/s 连续拖 1.2s，同时测帧间隔
async function dragMeasure(page) {
  const p = page.evaluate(() => new Promise((resolve) => {
    const cv = document.querySelector('.graph3d')
    const rect = cv.getBoundingClientRect()
    const ts = []
    let longTasks = 0
    let po
    try {
      po = new PerformanceObserver((l) => { longTasks += l.getEntries().length })
      po.observe({ entryTypes: ['longtask'] })
    } catch {}
    const cb = (t) => { ts.push(t); if (ts.length < 90) requestAnimationFrame(cb); else { try { po?.disconnect() } catch {}; resolve({ ts, longTasks }) } }
    requestAnimationFrame(cb)
    const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2
    cv.dispatchEvent(new PointerEvent('pointerdown', { clientX: cx, clientY: cy, pointerId: 1, bubbles: true }))
    let i = 0
    const total = 150
    const step = () => {
      if (i >= total) {
        cv.dispatchEvent(new PointerEvent('pointerup', { clientX: cx + total * 4, clientY: cy, pointerId: 1, bubbles: true }))
        return
      }
      i++
      cv.dispatchEvent(new PointerEvent('pointermove', { clientX: cx + i * 4, clientY: cy + Math.sin(i / 9) * 30, pointerId: 1, bubbles: true }))
      setTimeout(step, 8)
    }
    setTimeout(step, 100)
  }))
  return p.then((r) => ({ ...stats(r.ts), longTasks: r.longTasks }))
}

const fmt = (m) => `mean=${m.mean.toFixed(1)}ms(trimmed,spikes>${m.spikes}) p95=${m.p95.toFixed(1)}ms max=${m.max.toFixed(1)}ms janky(>25ms)=${m.janky}/${m.n} longTasks=${m.longTasks}`

// 有头模式：无头 Chromium 将 rAF 限在 ~30fps 且合成事件不触发 :hover，测不出真实帧耗与悬停
const browser = await chromium.launch({ headless: false })
try {
  await waitHealth()

  if (BIG) {
    // 生成 150 表库（链式 FK 保证有边）并经 API 预建连接 -> 应用直接选中，跳过演示引导
    const dbPath = path.join(dataDir, 'big.db')
    const gen = spawn(py, ['-c', `
import sqlite3, os
os.makedirs(${JSON.stringify(dataDir)}, exist_ok=True)
conn = sqlite3.connect(${JSON.stringify(dbPath)})
for i in range(150):
    prev = f", FOREIGN KEY (c1) REFERENCES t{i-1:03d}(id)" if i > 0 else ""
    conn.execute(f"CREATE TABLE t{i:03d} (id INTEGER PRIMARY KEY, c1 INTEGER, c2 TEXT, c3 REAL, c4 TEXT, c5 TEXT{prev})")
    conn.executemany(f"INSERT INTO t{i:03d} (c1,c2,c3,c4,c5) VALUES (?,?,?,?,?)",
                     [(j, f"name_{i}_{j}", float(j), f"v{j}", f"tag{j % 7}") for j in range(30)])
conn.commit(); conn.close()
print("ok")
`], { stdio: 'pipe' })
    await new Promise((res, rej) => { gen.on('exit', (c) => (c === 0 ? res() : rej(new Error('db gen failed')))) })
    const b = await (await fetch(`${baseUrl}/api/v1/bootstrap`)).json()
    const r = await fetch(`${baseUrl}/api/v1/connections`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': b.token },
      body: JSON.stringify({ name: 'big', dialect: 'sqlite', file: dbPath }),
    })
    if (!r.ok) throw new Error(`create conn failed: ${r.status} ${await r.text()}`)
  }

  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  page.on('pageerror', (e) => console.log('[pageerror]', String(e).slice(0, 300)))
  page.on('crash', () => console.log('[crash] renderer crashed'))
  page.on('framenavigated', (f) => { if (f === page.mainFrame()) console.log('[nav]', f.url()) })
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  if (!BIG) {
    const onboard = await page.$('.onboarding .primary')
    if (onboard) {
      await onboard.click()
      await page.waitForSelector('.appbar', { timeout: 20000 })
    }
    await buildAndConfirm(page)
  }
  const isGraph = await page.evaluate(() => document.querySelector('.g3d-wrap') !== null)
  if (!isGraph) {
    await page.click('.ws-tb:has-text("图谱")').catch(() => {})
    await page.waitForTimeout(600)
  }
  await page.waitForSelector('.graph3d', { timeout: 30000 })
  await page.waitForTimeout(2500) // 等 onboarding 飞行结束 + 稳定自转

  // 1) 静置自转帧间隔
  const idle = await measure(page, 240)
  console.log(`[idle 自转] ${fmt(idle)}`)
  check('静置自转: 平均帧间隔 <= 20ms', idle.mean <= 20, idle.mean.toFixed(1) + 'ms')
  check('静置自转: p95 <= 30ms', idle.p95 <= 30, idle.p95.toFixed(1) + 'ms')
  check('静置自转: 长任务(>50ms) <= 2', idle.longTasks <= 2, String(idle.longTasks))

  // 2) 拖拽跟手
  const drag = await dragMeasure(page)
  await page.waitForTimeout(800)
  console.log(`[拖拽] ${fmt(drag)}`)
  check('拖拽: 平均帧间隔 <= 20ms', drag.mean <= 20, drag.mean.toFixed(1) + 'ms')
  check('拖拽: p95 <= 34ms', drag.p95 <= 34, drag.p95.toFixed(1) + 'ms')
  check('拖拽: 长任务 <= 3', drag.longTasks <= 3, String(drag.longTasks))

  // 3) 打开表数据视图（mini 模式）：主线程余量
  // 用搜索飞到一张表 -> NodePopup -> 打开数据
  await page.fill('.g3d-search-input', BIG ? 't000' : 'orders')
  await page.press('.g3d-search-input', 'Enter')
  await page.waitForSelector('.node-pop', { timeout: 15000 })
  await page.waitForTimeout(1400) // 等飞行结束
  await page.click('.node-pop .np-open')
  await page.waitForSelector('.table-data-view table tbody tr', { timeout: 20000 })
  await page.waitForTimeout(600)

  // 悬停滑动模拟：真实鼠标事件在表格行上快速移动，同时测帧间隔
  const hoverP = page.evaluate(() => new Promise((resolve) => {
    window.__perfTs = []
    window.__perfLong = 0
    try {
      const po = new PerformanceObserver((l) => { window.__perfLong += l.getEntries().length })
      po.observe({ entryTypes: ['longtask'] })
      window.__perfPo = po
    } catch {}
    const cb = (t) => { window.__perfTs.push(t); requestAnimationFrame(cb) }
    requestAnimationFrame(cb)
    resolve(null)
  }))
  // 真实鼠标快速扫过各行（每 16ms 换一行，模拟用户快速滑动）
  const rowsInfo = await page.evaluate(() => Array.from(document.querySelectorAll('.table-data-view tbody tr')).slice(0, 60).map((r) => {
    const b = r.getBoundingClientRect()
    return { x: b.left + b.width / 2, y: b.top + b.height / 2 }
  }))
  console.log(`[debug] rowsInfo=${rowsInfo.length}`)
  for (const pt of rowsInfo) {
    await page.mouse.move(pt.x, pt.y)
    await page.waitForTimeout(14)
  }
  await page.waitForTimeout(300)
  console.log('[debug] after-loop:', await page.evaluate(() => ({
    tdv: !!document.querySelector('.table-data-view'),
    rows: document.querySelectorAll('.table-data-view tbody tr').length,
    pop: !!document.querySelector('.node-pop'),
    drop: !!document.querySelector('.g3d-search-drop')
  })))
  const hoverRaw = await page.evaluate(() => {
    try { window.__perfPo?.disconnect() } catch {}
    return { ts: window.__perfTs ?? [], longTasks: window.__perfLong ?? 0 }
  })
  const hs = stats(hoverRaw.ts)
  const hoverM = { ...hs, longTasks: hoverRaw.longTasks }
  console.log(`[表格悬停(星图mini)] ${fmt(hoverM)}`)
  check('表格悬停: 平均帧间隔 <= 20ms', hoverM.mean <= 20, hoverM.mean.toFixed(1) + 'ms')
  check('表格悬停: p95 <= 30ms', hoverM.p95 <= 30, hoverM.p95.toFixed(1) + 'ms')
  check('表格悬停: 长任务 <= 2', hoverM.longTasks <= 2, String(hoverM.longTasks))

  // 4) 悬停生效性：逐行验证 :hover 背景变化确实应用（快速移动后逐行取 computed style）
  const rowsForHover = await page.evaluate(() => Array.from(document.querySelectorAll('.table-data-view tbody tr')).slice(0, 20).map((r) => {
    const b = r.getBoundingClientRect()
    return { x: b.left + b.width / 2, y: b.top + b.height / 2 }
  }))
  let applied = 0
  for (const pt of rowsForHover) {
    await page.mouse.move(pt.x, pt.y)
    const bg = await page.evaluate((y) => {
      const rows = Array.from(document.querySelectorAll('.table-data-view tbody tr'))
      for (const r of rows) {
        const b = r.getBoundingClientRect()
        if (y >= b.top && y <= b.bottom) return getComputedStyle(r.cells[0]).backgroundColor
      }
      return ''
    }, pt.y)
    if (bg && bg !== 'rgba(0, 0, 0, 0)') applied++
  }
  const hoverOk = { applied, total: rowsForHover.length }
  check('悬停样式即时生效 >= 18/20 行', hoverOk.applied >= 18, `${hoverOk.applied}/${hoverOk.total}`)

  console.log(`\n[${BIG ? '150表大库' : '14表演示库'}] ${pass} pass, ${fail} fail`)
  process.exitCode = fail > 0 ? 1 : 0
} finally {
  sidecar.kill()
  await browser.close().catch(() => {})
}
