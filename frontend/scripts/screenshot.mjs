// Playwright Web 截图工具：自起后端（隔离 data_dir）→ 连演示库 → 知识审查 → AI 生成标签 → 截图
// 用法：node scripts/screenshot.mjs [输出路径]
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const outPath = process.argv[2] || path.resolve(root, '../docs/screenshots/knowledge-review.png')
const dataDir = '/tmp/tabletalk-shot'
const port = 8766
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
    } catch {
      /* 未就绪 */
    }
    await new Promise((res) => setTimeout(res, 500))
  }
  throw new Error('sidecar health timeout')
}

const browser = await chromium.launch()
try {
  await waitHealth()
  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })

  // 等待应用就绪（boot 消失、出现 appbar 或 onboarding）
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })

  const errBoot = await page.$('.boot-card .err')
  if (errBoot) {
    console.log('ERROR BOOT:', (await errBoot.textContent())?.slice(0, 200))
  }

  // 无连接时点"使用演示库"
  const onboard = await page.$('.onboarding .primary')
  if (onboard) {
    await onboard.click()
    await page.waitForSelector('.appbar', { timeout: 20000 })
  }
  await buildAndConfirm(page)
  await page.waitForSelector('.graph3d, .g-node', { timeout: 20000 })

  await page.click('button:has-text("知识库")')
  await page.waitForSelector('.review', { timeout: 20000 })

  const genBtn = await page.$('button:has-text("AI 生成标签")')
  if (genBtn) {
    await genBtn.click()
    await page.waitForTimeout(3500)
  }

  await page.waitForSelector('.rv-table-list .rv-table', { timeout: 20000 })
  fs.mkdirSync(path.dirname(outPath), { recursive: true })
  await page.screenshot({ path: outPath })
  console.log('OK screenshot →', outPath)
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 300))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
