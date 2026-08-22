// 设置「数据源管理」卡片 DOM 断言验证：结构 / 目标信息 / 敏感行隐藏 / 点卡切当前 / 设默认点亮
// 用法：node scripts/verify-settings.mjs
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tabletalk-verify-settings'
const port = 8768
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })

const python = fs.existsSync(path.join(backendDir, '.venv/bin/python'))
  ? path.join(backendDir, '.venv/bin/python')
  : 'python3'
const sidecar = spawn(python, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
  cwd: backendDir,
  env: { ...process.env, TABLETALK_DATA_DIR: dataDir },
  stdio: 'ignore'
})

async function waitHealth(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try {
      const r = await fetch(`${baseUrl}/api/v1/health`)
      if (r.ok) return
    } catch { /* not ready */ }
    await new Promise((res) => setTimeout(res, 500))
  }
  throw new Error('sidecar health timeout')
}

let failures = 0
function check(name, cond, extra = '') {
  if (cond) console.log(`  ✓ ${name}`)
  else { failures++; console.log(`  ✗ ${name} ${extra}`) }
}

const browser = await chromium.launch()
try {
  await waitHealth()
  // 预置两个连接：c1=订单库（只读，当前），c2=备份库（敏感名单）
  const demoFile = path.join(dataDir, 'demo.db')
  const token = await fetch(`${baseUrl}/api/v1/bootstrap`).then((r) => r.json()).then((j) => j.token)
  const h = { 'Content-Type': 'application/json', 'X-TableTalk-Token': token }
  await fetch(`${baseUrl}/api/v1/connections`, {
    method: 'POST', headers: h,
    body: JSON.stringify({ name: '订单库', dialect: 'sqlite', file: demoFile, read_only: true }),
  })
  await fetch(`${baseUrl}/api/v1/connections`, {
    method: 'POST', headers: h,
    body: JSON.stringify({ name: '备份库', dialect: 'sqlite', file: demoFile, read_only: false, sensitive: ['payroll_*'] }),
  })

  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  await page.waitForSelector('.appbar .sys-btn', { timeout: 40000 })
  await page.waitForTimeout(1500)

  // 打开设置
  await page.click('.appbar .sys-btn')
  await page.waitForSelector('.set-drawer', { timeout: 10000 })
  await page.waitForTimeout(500)

  console.log('== dsm 卡片结构 ==')
  const rows = await page.$$('.conn-row')
  check('两张连接卡', rows.length === 2, `got ${rows.length}`)

  // 卡1（订单库，当前）结构
  const card1 = rows[0]
  check('卡1 高亮为当前 (cur)', (await card1.getAttribute('class')).includes('cur'))
  check('卡1 名称渲染', (await card1.$eval('.conn-name', (e) => e.textContent)) === '订单库')
  check('卡1 方言 chip', (await card1.$eval('.conn-dialect', (e) => e.textContent.trim())) === 'sqlite')
  check('卡1 目标行含 demo.db', ((await card1.$eval('.conn-target .ct-text', (e) => e.textContent)) || '').includes('demo.db'))
  check('卡1 无旧状态点 .st', (await card1.$('.st')) === null)
  const btns1 = await card1.$$eval('.conn-actions .mini-btn', (bs) => bs.map((b) => b.textContent.trim()))
  check('卡1 动作 2×2 四按钮', btns1.length === 4, JSON.stringify(btns1))
  check('卡1 当前 tag', (await card1.$eval('.cur-tag', (e) => e.textContent)) === '当前使用')
  check('卡1 初始无默认 tag（defaultId 未设置）', (await card1.$('.def-tag')) === null)

  // 卡2：敏感名单展示 + 非当前
  const card2 = rows[1]
  check('卡2 非当前', !(await card2.getAttribute('class')).includes('cur'))
  const sensText = await card2.$eval('.conn-sens .sens-list', (e) => e.textContent)
  check('卡2 敏感名单行展示', (sensText || '').includes('payroll_*'), String(sensText))
  check('卡2 目标行', ((await card2.$eval('.conn-target .ct-text', (e) => e.textContent)) || '').includes('demo.db'))
  // 卡1 无敏感配置 → 敏感行应隐藏
  check('卡1 无敏感 → 行隐藏', (await card1.$('.conn-sens')) === null)

  console.log('== 点卡切换当前 ==')
  await card2.click()
  await page.waitForTimeout(300)
  const cls2 = await card2.getAttribute('class')
  check('点击卡2 → 点亮为当前', cls2.includes('cur'))

  console.log('== 设默认点亮 + 持久化 ==')
  // 点卡1 的「设为默认」按钮（不应触发行点击）
  await rows[0].$$eval('.conn-actions .mini-btn', (bs) => bs[1].click())
  await page.waitForTimeout(400)
  const cls1 = await rows[0].getAttribute('class')
  check('卡1 设默认后点亮为当前', cls1.includes('cur'))
  check('卡1 设默认后 ★ 默认 tag 出现', ((await rows[0].$eval('.def-tag', (e) => e.textContent)) || '').includes('★'))
  const stored = await page.evaluate(() => localStorage.getItem('tabletalk-default-conn'))
  check('默认已持久化到 localStorage', !!stored)

  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURES`)
  process.exitCode = failures === 0 ? 0 : 1
} finally {
  await browser.close()
  sidecar.kill()
}
