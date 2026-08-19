// 图谱 k-hop 探索 + 知识 top-N 检索验证
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-khop'
const port = 8820 + Math.floor(Math.random() * 30)
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

  // ── 1. k-hop 控件：单击节点出现 ──
  await page.click('.g-node:has-text("orders")')
  await page.waitForSelector('.g-hop-bar', { timeout: 10000 })
  check('k 跳控件出现', true, '有 .g-hop-bar')
  const hopBtns = await page.$$eval('.ghb-btn', (els) => els.map((e) => e.textContent?.trim()))
  check('1/2/3 跳按钮', hopBtns.length === 3, hopBtns.join(' | '))

  // ── 2. 圈定子图：跳数变化 → 圈外淡化节点数变化 ──
  const dimAt = async () => page.$$eval('.g-node.out-hop', (els) => els.length)
  const dim2 = await dimAt()
  check('2 跳圈定有圈外淡化', dim2 > 0, `${dim2} 个淡化`)
  await page.click('.ghb-btn:has-text("1 跳")')
  await page.waitForTimeout(400)
  const dim1 = await dimAt()
  check('1 跳圈定更严（淡化更多）', dim1 > dim2, `1跳=${dim1} 2跳=${dim2}`)
  await page.click('.ghb-btn:has-text("3 跳")')
  await page.waitForTimeout(400)
  const dim3 = await dimAt()
  check('3 跳圈定更宽（淡化更少）', dim3 < dim2, `3跳=${dim3} 2跳=${dim2}`)

  // ── 3. 知识 top-N 检索 ──
  await page.click('button:has-text("知识库")')
  await page.waitForSelector('.kb-search', { timeout: 15000 })
  await page.fill('.rs-input', '订单')
  await page.click('.rs-btn')
  await page.waitForSelector('.review-results', { timeout: 15000 })
  const n = await page.$$eval('.rr-item', (els) => els.length)
  check('top-N 检索出结果', n > 0, `${n} 条`)
  if (n > 0) {
    const first = await page.textContent('.rr-item')
    check('结果含表/列/注释信息', (first || '').includes('orders') || (first || '').includes('订单'), first?.slice(0, 60) || '—')
  }
  // 清空检索
  await page.click('.rr-x')
  await page.waitForTimeout(300)
  check('检索结果可关闭', !(await page.$('.review-results')), '已关闭')

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 500))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill('SIGKILL')
}
