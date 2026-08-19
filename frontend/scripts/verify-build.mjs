// 搭查（积木台）验证：多选节点 → JOIN 骨架 → 直接运行 → 查询节点沉淀 → 重跑/删除
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-build'
const port = 8790 + Math.floor(Math.random() * 60)
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const sidecar = spawn(path.join(backendDir, '.venv/bin/python'), ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
  cwd: backendDir, env: { ...process.env, CLEARED_DATA_DIR: dataDir }, stdio: 'ignore'
})
async function waitHealth(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try { const r = await fetch(`${baseUrl}/api/v1/health`); if (r.ok) return } catch { }
    await new Promise((res) => setTimeout(res, 500))
  }
  throw new Error('health timeout')
}
const browser = await chromium.launch()
let pass = 0, fail = 0
function check(name, cond, got) {
  if (cond) { pass++; console.log(`PASS ${name} → ${got}`) }
  else { fail++; console.log(`FAIL ${name} → ${got}`) }
}
try {
  await waitHealth()
  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  page.on('pageerror', e => console.log('PAGEERROR:', String(e).slice(0, 200)))
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  const onboard = await page.$('.onboarding .primary')
  if (onboard) { await onboard.click(); await page.waitForSelector('.appbar', { timeout: 20000 }) }
  await buildAndConfirm(page)
  await page.waitForSelector('.g-node', { timeout: 60000 })
  await page.waitForTimeout(600)

  // ── 1. 搭查模式：选节点 ──
  await page.click('.g-mode-btn:has-text("搭查")')
  await page.waitForSelector('.g-build-bar', { timeout: 10000 })
  await page.click('.g-node:has-text("orders")')
  await page.waitForTimeout(300)
  await page.click('.g-node:has-text("customers")')
  await page.waitForTimeout(300)
  await page.click('.g-node:has-text("payments")')
  await page.waitForTimeout(500)
  const chips = await page.$$eval('.gbb-chip', (els) => els.map((e) => e.textContent))
  check('搭查集合 chips（3 表）', chips.length === 3, chips.join(', '))
  const inBuild = await page.$$eval('.g-node.in-build', (els) => els.length)
  check('节点加入集合高亮', inBuild === 3, `${inBuild} 个高亮`)

  // ── 2. 生成 JOIN 骨架 ──
  await page.click('.g-build-bar .ggb-btn.pri')
  await page.waitForSelector('.g-skel-panel', { timeout: 10000 })
  const skelSql = await page.inputValue('.gsk-ta')
  check('骨架含 FROM orders', skelSql.includes('FROM orders'), skelSql.split('\n')[0]?.slice(0, 40) || '—')
  check('骨架含 JOIN', skelSql.includes('JOIN'), '有 JOIN 子句')
  check('骨架含 LIMIT 保护', skelSql.includes('LIMIT'), '有 LIMIT')

  // ── 3. 直接运行 → 结果 + 查询节点沉淀 ──
  await page.click('.g-skel-panel .ggb-btn.pri')
  await page.waitForSelector('.ws-tab', { timeout: 30000 })
  const tabTxt = await page.textContent('.ws-tab.on')
  check('骨架执行结果进表格', (tabTxt || '').includes('数据'), tabTxt?.slice(0, 40) || '—')

  // 回图谱：查询节点已沉淀
  await page.keyboard.press('Meta+1')
  await page.waitForTimeout(800)
  await page.waitForSelector('.g-qnode', { timeout: 10000 })
  const qTitle = await page.$eval('.g-qnode text:first-of-type', (t) => t.textContent)
  check('查询节点沉淀', (qTitle || '').includes('▤'), qTitle || '—')

  // ── 4. 查询节点重跑 ──
  await page.$eval('.g-qnode', (n) => n.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })))
  await page.waitForSelector('.ws-tab', { timeout: 30000 })
  const tabsAfter = await page.$$eval('.ws-tab', (els) => els.length)
  check('查询节点重跑出新结果', tabsAfter >= 2, `${tabsAfter} 个标签`)

  // ── 5. 右键删除查询节点 ──
  await page.keyboard.press('Meta+1')
  await page.waitForTimeout(800)
  await page.click('.g-qnode', { button: 'right' })
  await page.waitForSelector('.g-menu', { timeout: 5000 })
  await page.click('.g-menu button:has-text("删除查询节点")')
  await page.waitForTimeout(600)
  const qLeft = await page.$$eval('.g-qnode', (els) => els.length)
  check('查询节点可删除', qLeft === 0, `${qLeft} 个`)

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 500))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill('SIGKILL')
}
