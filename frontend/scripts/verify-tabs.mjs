// P3 验证：多标签结果 + 左缘微条关联跳转
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-p3'
const port = 8771
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
  await page.waitForSelector('.g-node', { timeout: 30000 })

  // 双击第一个节点 → 表格视图 + 标签栏 + 微条
  await page.waitForTimeout(600)
  await page.$eval('.g-node', (n) => {
    n.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))
  })
  await page.waitForSelector('.ws-data', { timeout: 10000 })
  await page.waitForSelector('.ws-tab', { timeout: 10000 })
  const tabCount1 = await page.$$eval('.ws-tab', (els) => els.length)
  check('开表后出现标签', tabCount1 === 1, String(tabCount1))
  check('左缘微条存在', !!(await page.$('.edge-rail')), '有 .edge-rail')
  const erBtn = await page.$('.er-btn')
  check('微条回图按钮存在', !!erBtn, '有 .er-btn')
  const dots = await page.$$eval('.er-dot', (els) => els.length)
  check('微条有关联表点', dots > 0, `${dots} 个`)

  // 点击关联表点 → 新标签
  if (dots > 0) {
    await page.click('.er-dot')
    await page.waitForTimeout(1200)
    const tabCount2 = await page.$$eval('.ws-tab', (els) => els.length)
    check('关联跳转新增标签', tabCount2 === 2, String(tabCount2))
  }

  // 标签切换：点第一个标签
  await page.$$eval('.ws-tab', (els) => els[0].click())
  await page.waitForTimeout(400)
  const onTxt = await page.textContent('.ws-tab.on')
  check('标签可切换', !!onTxt, onTxt?.slice(0, 40) || '—')

  // 关闭一个标签
  await page.click('.ws-tab .ws-tab-x')
  await page.waitForTimeout(400)
  const tabCount3 = await page.$$eval('.ws-tab', (els) => els.length)
  check('关闭标签', tabCount3 === 1, String(tabCount3))

  // ⌘1 回图谱
  await page.keyboard.press('Meta+1')
  await page.waitForTimeout(500)
  await page.waitForSelector('.gcanvas', { timeout: 10000 })
  check('⌘1 回图谱', true, 'gcanvas 可见')

  // 清空全部标签 → 自动回图谱（先回表格再删）
  await page.keyboard.press('Meta+2')
  await page.waitForTimeout(400)
  await page.click('.ws-tab .ws-tab-x')
  await page.waitForTimeout(800)
  const backToGraph = !!(await page.$('.gcanvas'))
  check('清空标签自动回图谱', backToGraph, backToGraph ? 'gcanvas 可见' : '未回')

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 400))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
