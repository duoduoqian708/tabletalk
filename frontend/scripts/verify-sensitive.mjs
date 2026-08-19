// 敏感名单验证：数据源管理 → 展开敏感名单 → 保存 → 标记 + API 往返
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-sens'
const port = 8798 + Math.floor(Math.random() * 30)
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
  // API 层：创建带敏感名单的连接
  const boot = await (await fetch(`${baseUrl}/api/v1/bootstrap`)).json()
  const token = boot.token
  const created = await (await fetch(`${baseUrl}/api/v1/connections`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Cleared-Token': token },
    body: JSON.stringify({ name: '敏感库', dialect: 'sqlite', file: `${dataDir}/demo.db`, read_only: true, sensitive: ['orders'] })
  })).json()
  check('API 创建带敏感名单', (created.sensitive ?? []).includes('orders'), JSON.stringify(created.sensitive))

  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  page.on('pageerror', e => console.log('PAGEERROR:', String(e).slice(0, 200)))
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar', { timeout: 40000 })

  // 设置 → 数据源管理 → 敏感名单编辑
  await page.click('.sys-btn')
  await page.waitForSelector('.set-drawer', { timeout: 10000 })
  await page.waitForSelector('.conn-row', { timeout: 10000 })
  const hasSensBtn = !!(await page.$('.conn-row .mini-btn:has-text("敏感名单")'))
  check('连接行有敏感名单编辑', hasSensBtn, hasSensBtn ? '有按钮' : '无')
  const hasSensTag = !!(await page.$('.conn-row .sens-tag'))
  check('已有敏感名单显示屏蔽标记', hasSensTag, hasSensTag ? '有标记' : '无标记')

  // 展开编辑：追加一条
  await page.click('.conn-row .mini-btn:has-text("敏感名单")')
  await page.waitForSelector('.sens-input', { timeout: 5000 })
  const curVal = await page.inputValue('.sens-input')
  await page.fill('.sens-input', `${curVal}, payments.*`)
  await page.click('.conn-sens .mini-btn.set')
  await page.waitForTimeout(1200)
  const tagTxt = await page.textContent('.conn-row .sens-tag')
  check('保存后屏蔽标记更新', (tagTxt || '').includes('2'), tagTxt || '—')

  // API 层确认持久化
  const lst = await (await fetch(`${baseUrl}/api/v1/connections`, { headers: { 'X-Cleared-Token': token } })).json()
  const sens = lst.find((c) => c.name === '敏感库')?.sensitive ?? []
  check('API 确认敏感名单已更新', sens.length === 2 && sens.includes('payments.*'), JSON.stringify(sens))

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 500))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill('SIGKILL')
}
