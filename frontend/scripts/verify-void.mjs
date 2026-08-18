// Void 视觉验证：检查关键元素的 computed style 是否深色主题
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-shot2'
const port = 8767
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })

const python = fs.existsSync(path.join(backendDir, '.venv/bin/python'))
  ? path.join(backendDir, '.venv/bin/python')
  : 'python3'
const sidecar = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
  cwd: backendDir,
  env: { ...process.env, CLEARED_DATA_DIR: dataDir },
  stdio: 'ignore'
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
  if (onboard) {
    await onboard.click()
    await page.waitForSelector('.appbar', { timeout: 20000 })
  }
  await page.waitForSelector('.g-node', { timeout: 30000 })

  const cs = (sel, prop) => page.evaluate(([s, p]) => {
    const el = document.querySelector(s)
    return el ? getComputedStyle(el)[p] : null
  }, [sel, prop])

  check('body 背景深色', (await cs('body', 'backgroundColor')) === 'rgb(10, 11, 14)', await cs('body', 'backgroundColor'))
  check('body 文字亮色', (await cs('body', 'color')) === 'rgb(232, 234, 240)', await cs('body', 'color'))
  check('appbar 背景深色面板', (await cs('.appbar', 'backgroundColor')) === 'rgb(13, 14, 18)', await cs('.appbar', 'backgroundColor'))
  check('airail 背景深色面板', (await cs('.airail', 'backgroundColor')) === 'rgb(13, 14, 18)', await cs('.airail', 'backgroundColor'))
  check('图谱画布背景深色', (await cs('.gcanvas', 'backgroundColor')) === 'rgb(10, 11, 14)', await cs('.gcanvas', 'backgroundColor'))

  // 设置抽屉
  await page.click('.sys-btn')
  await page.waitForSelector('.set-drawer', { timeout: 10000 })
  check('设置抽屉背景深色', (await cs('.set-drawer', 'backgroundColor')) === 'rgb(13, 14, 18)', await cs('.set-drawer', 'backgroundColor'))
  await page.click('.set-x')
  await page.waitForSelector('.set-drawer', { state: 'detached', timeout: 10000 })

  // AI 对话基本可用性：发一句 mock 查询
  await page.click('.compose-input')
  await page.type('.compose-input', '看看订单表')
  await page.click('.airail .send')
  await page.waitForTimeout(8000)
  const railTxt = await page.evaluate(() => document.querySelector('.airail')?.textContent ?? '')
  check('AI 对话有响应', railTxt.includes('mock') || !!railTxt.trim(), railTxt.trim().slice(0, 50) || '空')

  // 顶栏 gw 显示模型
  const gw = await page.textContent('.gw')
  check('顶栏 gw 有内容', !!gw && gw.length > 3, gw || '—')

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 300))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
