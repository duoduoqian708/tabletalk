// P2 图谱画布验证：连接演示库 → 画布出节点/边 → 单击检查器 → 双击开数据 → 工具栏切回 → ⌘1/⌘2
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-p2'
const port = 8769
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
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  const onboard = await page.$('.onboarding .primary')
  if (onboard) { await onboard.click(); await page.waitForSelector('.appbar', { timeout: 20000 }) }
  await buildAndConfirm(page)

  // 画布出现
  await page.waitForSelector('.gcanvas', { timeout: 30000 })
  await page.waitForSelector('.g-node', { timeout: 30000 })
  const nodeCount = await page.$$eval('.g-node', (els) => els.length)
  check('画布节点数 > 0', nodeCount > 0, String(nodeCount))
  const edgeCount = await page.$$eval('.g-svg path', (els) => els.length)
  check('画布边数 > 0', edgeCount > 0, String(edgeCount))
  check('模式切换存在', !!(await page.$('.g-mode')), '有 .g-mode')
  check('图例存在', !!(await page.$('.g-legend')), '有 .g-legend')
  check('缩放控件存在', !!(await page.$('.g-zoom')), '有 .g-zoom')

  // 单击节点 → 检查器
  const firstNode = await page.$('.g-node')
  await firstNode.click()
  await page.waitForSelector('.g-inspector', { timeout: 10000 })
  check('单击出检查器', true, '有 .g-inspector')
  const inspName = await page.textContent('.gi-name')
  check('检查器有表名', !!inspName && inspName.includes('检查器'), inspName || '—')

  // 检查器"打开数据" → 表格视图
  await page.click('.gi-btn.pri')
  await page.waitForTimeout(800)
  const tbText = await page.textContent('.ws-tb.on')
  check('切到表格视图', tbText?.includes('表格'), tbText || '—')
  await page.waitForSelector('.tableview', { timeout: 15000 }).catch(() => {})
  const hasTable = !!(await page.$('.tableview'))
  check('表格视图有数据表', hasTable, hasTable ? '有表' : '无表')

  // 工具栏切回图谱
  await page.click('.ws-tb:has-text("图谱")')
  await page.waitForSelector('.gcanvas', { timeout: 10000 })
  check('工具栏切回图谱', true, 'gcanvas 可见')

  // ⌘1 / ⌘2 快捷键
  await page.keyboard.press('Meta+2')
  await page.waitForTimeout(400)
  const tb2 = await page.textContent('.ws-tb.on')
  check('⌘2 切表格', tb2?.includes('表格'), tb2 || '—')
  await page.keyboard.press('Meta+1')
  await page.waitForTimeout(400)
  await page.waitForSelector('.gcanvas', { timeout: 10000 })
  check('⌘1 切图谱', true, 'gcanvas 可见')

  // 模式切换点击（在图谱视图中）
  await page.click('.g-mode-btn:has-text("治理")')
  const modeOn = await page.textContent('.g-mode-btn.on')
  check('治理模式切换', modeOn?.includes('治理'), modeOn || '—')
  await page.click('.g-mode-btn:has-text("浏览")')

  // 双击节点开数据
  await page.waitForSelector('.gcanvas', { timeout: 10000 })
  await page.waitForTimeout(600)
  const node2 = await page.$('.g-node')
  if (node2) {
    try {
      await node2.dblclick({ timeout: 8000 })
    } catch {
      await node2.dblclick({ force: true })
    }
  }
  await page.waitForTimeout(1200)
  const tb3 = await page.textContent('.ws-tb.on')
  check('双击节点开数据', tb3?.includes('表格'), tb3 || '—')

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 400))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
