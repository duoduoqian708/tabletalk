// P4 真实模型验证：复用用户配置（火山引擎 deepseek-v4-flash）
// 只读查询自动执行（硬性）+ DML 黄卡（尽力而为，失败时输出对话诊断）
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import os from 'node:os'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-real'
const port = 8773
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const userSettings = path.join(os.homedir(), '.cleared', 'settings.json')
if (fs.existsSync(userSettings)) fs.copyFileSync(userSettings, path.join(dataDir, 'settings.json'))

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
  await page.waitForSelector('.g-node', { timeout: 60000 })

  // 只读查询（真实模型）
  await page.click('.compose-input')
  await page.type('.compose-input', 'orders 表总共有多少条订单记录？')
  await page.click('.airail .send')
  await page.waitForSelector('.sql', { timeout: 120000 }).catch(() => {})
  const hasCard = !!(await page.$('.sql'))
  check('真实模型出 SQL 卡', hasCard, hasCard ? '有卡片' : '无卡片')
  await page.waitForTimeout(15000)
  const hasTab = !!(await page.$('.ws-tab'))
  check('只读查询自动执行进结果区', hasTab, hasTab ? '有标签' : '无标签')

  // DML（尽力而为）
  await page.click('.compose-input')
  await page.type('.compose-input', '请直接执行写操作：把 orders 表里所有 status 为 pending 的记录更新为 paid（UPDATE 语句）')
  await page.click('.airail .send')
  await page.waitForSelector('.sql.review', { timeout: 150000 }).catch(() => {})
  const hasReview = !!(await page.$('.sql.review'))
  check('真实模型写操作出黄卡', hasReview, hasReview ? '有黄卡' : '无黄卡')
  if (hasReview) {
    const hasRisk = !!(await page.$('.risk-panel'))
    check('风险面板展开', hasRisk, hasRisk ? '有面板' : '无面板')
    await page.click('.sql.review .btn.warn')
    await page.waitForSelector('.exec-stamp', { timeout: 30000 }).catch(() => {})
    check('确认执行留痕', !!(await page.$('.exec-stamp')), (await page.$('.exec-stamp')) ? '有留痕' : '无留痕')
  } else {
    // 诊断：输出对话内容
    const rail = await page.evaluate(() => document.querySelector('.airail')?.textContent?.slice(0, 500) ?? '')
    console.log('DIAG rail:', rail)
  }

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 500))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
