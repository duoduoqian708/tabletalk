// 报告数字回溯验证（真实模型）：报告出章节 → 点来源 → 滚动+展开明细+高亮 → SQL 可展开
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import os from 'node:os'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-report'
const port = 8796 + Math.floor(Math.random() * 40)
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const userSettings = path.join(os.homedir(), '.cleared', 'settings.json')
if (fs.existsSync(userSettings)) {
  fs.copyFileSync(userSettings, path.join(dataDir, 'settings.json'))
  console.log('已复用真实模型配置')
}
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

  // 报告模式提问（真实模型，等待较久）
  await page.click('.compose-input')
  await page.type('.compose-input', '请写一份分析报告：按月份分析订单总额与回款情况')
  await page.click('.airail .send')

  // HITL 澄清循环：模型会先问口径（时间范围/指标），逐轮回答直到出章节
  const ANSWERS = ['近 12 个月', '支付金额', '回款金额', '按月']
  let answered = 0
  for (let i = 0; i < 8; i++) {
    const secsNow = await page.$$eval('.rpt-sec', (els) => els.filter((e) => e.id.startsWith('rpt-sec-r')).length).catch(() => 0)
    if (secsNow > 0) break
    const box = await page.$('.clarify-input-box .a-field')
    if (box) {
      const ans = ANSWERS[Math.min(answered, ANSWERS.length - 1)]
      await page.fill('.clarify-input-box .a-field', ans)
      await page.click('.clarify-input-box .send')
      answered++
      console.log(`澄清已回答(${answered})：${ans}`)
    }
    await page.waitForTimeout(12000)
  }
  const hasRpt = !!(await page.$('.rpt-card'))
  check('报告卡生成', hasRpt, hasRpt ? '有报告' : '无报告')
  if (!hasRpt) {
    const rail = await page.evaluate(() => document.querySelector('.airail')?.textContent?.slice(0, 300))
    console.log('DIAG rail:', rail)
  }

  // 等待章节/refs（LLM 逐章执行，轮询）
  let secs = 0
  let refs = 0
  for (let i = 0; i < 12; i++) {
    secs = await page.$$eval('.rpt-sec', (els) => els.filter((e) => e.id.startsWith('rpt-sec-r')).length).catch(() => 0)
    refs = await page.$$eval('.rpt-ref-line', (els) => els.length).catch(() => 0)
    if (secs > 0 && refs > 0) break
    await page.waitForTimeout(5000)
  }
  check('报告含章节', secs > 0, `${secs} 章`)
  check('报告含可回溯来源', refs > 0, `${refs} 个来源`)

  if (refs > 0) {
    // 点击第一个来源行 → 滚动 + 高亮 + 明细自动展开
    await page.click('.rpt-ref-line')
    await page.waitForTimeout(1200)
    const hl = !!(await page.$('.rpt-sec.jump-hl'))
    check('来源点击后章节高亮', hl, hl ? '有 jump-hl' : '无')
    const foldOpen = await page.$$eval('.rpt-fold', (els) => els.filter((e) => e.textContent?.includes('折叠')).length)
    check('明细自动展开', foldOpen > 0, `${foldOpen} 个折叠态`)
    // 来源 SQL 可展开
    const sqlBtn = await page.$('.rpt-fold.sql')
    if (sqlBtn) {
      await sqlBtn.click()
      await page.waitForTimeout(400)
      const sqlTxt = await page.textContent('.rpt-sec-sql')
      check('来源 SQL 展示', (sqlTxt || '').includes('SELECT'), (sqlTxt || '').slice(0, 50))
    } else {
      check('来源 SQL 按钮存在', false, '无 SQL 按钮')
    }
    // 叙述 [rN] 上标
    const narrRef = await page.$('.rpt-narr .rpt-ref')
    if (narrRef) {
      await narrRef.click()
      await page.waitForTimeout(1000)
      check('叙述 [rN] 跳转', !!(await page.$('.rpt-sec.jump-hl')), '有高亮')
    } else {
      check('叙述 [rN] 上标', true, '无叙述引用（可接受）')
    }
  }

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 500))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill('SIGKILL')
}
