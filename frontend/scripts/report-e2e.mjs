// 报告模式 E2E 验证：自起后端 → 连演示库 → 点"报告" → 输入问题 → 验证报告卡渲染
// 用法：node scripts/report-e2e.mjs
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-report-e2e'
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
    try {
      const r = await fetch(`${baseUrl}/api/v1/health`)
      if (r.ok) return
    } catch { /* 未就绪 */ }
    await new Promise((res) => setTimeout(res, 500))
  }
  throw new Error('sidecar health timeout')
}

const checks = []
function check(name, ok, detail = '') {
  checks.push({ name, ok, detail })
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + detail.slice(0, 100) : ''}`)
}

const browser = await chromium.launch()
try {
  await waitHealth()
  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })

  // 收集 console error
  const errs = []
  page.on('console', (m) => {
    if (m.type() === 'error') errs.push(m.text())
  })
  page.on('pageerror', (e) => errs.push(String(e)))

  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  check('应用启动', true)

  // 连演示库
  const onboard = await page.$('.onboarding .primary')
  if (onboard) {
    await onboard.click()
    await page.waitForSelector('.appbar', { timeout: 20000 })
  }
  await buildAndConfirm(page)
  await page.waitForSelector('.sch-item', { timeout: 20000 })
  check('schema 树加载', true)

  // 切到 AI 标签
  await page.click('text=AI')
  await page.waitForSelector('.airail', { timeout: 10000 })
  check('AI 面板加载', true)

  // 点"报告"按钮（mode=report 兜底）
  const rptBtn = await page.$('.rpt-btn')
  check('报告按钮存在', !!rptBtn)
  if (rptBtn) {
    await rptBtn.click()
    await page.waitForTimeout(200)
    const active = await page.$('.rpt-btn.on')
    check('报告按钮激活态', !!active)
  }

  // 输入问题 + 发送
  const input = await page.$('.airail-in .a-field')
  check('输入框存在', !!input)
  await input.fill('出一份 2026 上半年销售分析报告')
  await page.click('.airail-in .send')

  // 等待报告卡出现（最多 30s）
  await page.waitForSelector('.rpt-card', { timeout: 30000 })
  check('报告卡渲染', true)

  // 验证标题
  const title = await page.$eval('.rpt-title', (el) => el.textContent.trim())
  check('报告有标题', !!title && title.length > 0, title)

  // 验证章节
  const secs = await page.$$('.rpt-sec')
  check('章节数 ≥ 2', secs.length >= 2, `共 ${secs.length} 章`)

  // 验证图表
  const charts = await page.$$('.rpt-chart')
  check('SVG 图表渲染', charts.length >= 2, `共 ${charts.length} 个图表`)

  // 验证叙述
  const narr = await page.$('.rpt-narr')
  check('叙述区存在', !!narr)
  if (narr) {
    const narrText = await narr.textContent()
    check('叙述有内容', narrText.length > 10, narrText.slice(0, 60))
  }

  // 验证数字回溯引用
  const refs = await page.$$('.rpt-ref')
  check('数字回溯引用存在', refs.length > 0, `共 ${refs.length} 个`)

  // 验证来源 footer
  const refSec = await page.$('.rpt-refs')
  check('来源回溯 footer', !!refSec)

  // 测试澄清交互：欠定义的问题
  await page.click('.ah-btn[title="新建对话"]')
  await page.waitForTimeout(300)
  const rptBtn2 = await page.$('.rpt-btn')
  if (rptBtn2) await rptBtn2.click()
  const input2 = await page.$('.airail-in .a-field')
  await input2.fill('出一份报告')
  await page.click('.airail-in .send')

  await page.waitForSelector('.clarify-input-box', { timeout: 15000 })
  check('澄清挂起 UI 出现', true)

  // 回答澄清
  const clrInput = await page.$('.clarify-input-box .a-field')
  check('澄清输入框存在', !!clrInput)
  if (clrInput) {
    await clrInput.fill('2026 年上半年')
    const submitBtn = await page.$('.clarify-input-box .send')
    if (submitBtn) {
      await submitBtn.click()
      // 应该收到报告
      await page.waitForSelector('.rpt-card', { timeout: 30000 })
      check('澄清回答后生成报告', true)
    }
  }

  // 截图
  const shotPath = path.resolve(root, '../docs/screenshots/report-mode.png')
  fs.mkdirSync(path.dirname(shotPath), { recursive: true })
  await page.screenshot({ path: shotPath, fullPage: false })
  check('截图保存', true, shotPath)

  // console 错误检查
  check('无 console error', errs.length === 0, errs.slice(0, 3).join(' | '))

  // 汇总
  const passed = checks.filter((c) => c.ok).length
  console.log(`\n结果：${passed}/${checks.length} 通过`)
  if (passed !== checks.length) process.exitCode = 1
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 400))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
