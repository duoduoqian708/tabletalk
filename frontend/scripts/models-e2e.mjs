// 大模型管理 E2E 验证：多模型列表 / 添加 / 测试连接 / 设为默认 / 嵌入模型 tab
// 用法：node scripts/models-e2e.mjs
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tabletalk-models-e2e'
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
    } catch { /* 未就绪 */ }
    await new Promise((res) => setTimeout(res, 500))
  }
  throw new Error('sidecar health timeout')
}

const checks = []
function check(name, ok, detail = '') {
  checks.push({ name, ok, detail })
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ' — ' + String(detail).slice(0, 100) : ''}`)
}

const browser = await chromium.launch()
try {
  await waitHealth()
  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })

  const errs = []
  page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()) })
  page.on('pageerror', (e) => errs.push(String(e)))

  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })

  // 连演示库
  const onboard = await page.$('.onboarding .primary')
  if (onboard) { await onboard.click(); await page.waitForSelector('.appbar', { timeout: 20000 }) }
  await buildAndConfirm(page)
  await page.waitForSelector('.sch-item', { timeout: 20000 })

  // 打开设置抽屉
  await page.click('text=系统设置')
  await page.waitForSelector('.set-drawer', { timeout: 5000 })
  check('设置抽屉打开', true)

  // 切到大模型接入
  await page.click('.sr-it:has-text("大模型接入")')
  await page.waitForSelector('.model-tabs', { timeout: 3000 })
  check('大模型接入 section', true)

  // 文本模型 tab + 模型列表
  const chatModels = await page.$$('.model-list .model-item')
  check('文本模型列表存在', chatModels.length >= 1, `共 ${chatModels.length} 个`)

  // 切到嵌入模型 tab
  await page.click('.model-tab:has-text("向量嵌入模型")')
  await page.waitForTimeout(200)
  const embModels = await page.$$('.model-list .model-item')
  check('嵌入模型列表存在', embModels.length >= 1, `共 ${embModels.length} 个`)

  // 切回文本模型
  await page.click('.model-tab:has-text("文本推理模型")')
  await page.waitForTimeout(200)

  // 添加新模型
  await page.click('.model-add')
  await page.waitForSelector('.model-edit', { timeout: 3000 })
  check('添加模型表单展开', true)

  // 填表单
  const nameInput = await page.$('.model-edit input[placeholder="我的模型"]')
  await nameInput.fill('测试模型')
  const urlInput = await page.$('.model-edit input[placeholder="https://api.example.com/v1"]')
  await urlInput.fill('https://api.openai.com/v1')
  const modelInput = await page.$('.model-edit input[placeholder="gpt-4o / claude-sonnet-4.5"]')
  await modelInput.fill('gpt-4o')

  // 点添加
  await page.click('.model-edit .btn.save')
  await page.waitForTimeout(300)

  // 列表里应该有新模型
  const modelNames = await page.$$eval('.model-item .mi-name', (els) => els.map((e) => e.textContent.trim()))
  check('新模型出现在列表', modelNames.includes('测试模型'), modelNames.join(', '))

  // 设为默认
  const setDefaultBtn = (await page.$$('.model-item')).find(async (item) => {
    const name = await item.$eval('.mi-name', (e) => e.textContent.trim())
    return name === '测试模型'
  })
  if (setDefaultBtn) {
    await setDefaultBtn.click()  // 先点整行选中？不，点按钮
    const btns = await setDefaultBtn.$$('.mini-btn.set')
    if (btns.length) {
      await btns[0].click()
      await page.waitForTimeout(200)
      const curCount = await page.$$eval('.model-item.cur', (els) => els.length)
      check('设为默认生效', curCount === 1)
    }
  }

  // 测试 mock 模型（一定成功，验证能力徽章）
  const mockItem = (await page.$$('.model-item')).find(async (item) => {
    const name = await item.$eval('.mi-provider', (e) => e.textContent.trim())
    return name === 'mock'
  })
  if (mockItem) {
    const editBtn = await mockItem.$('.mini-btn:not(.set):not(.dang)')
    if (editBtn) {
      await editBtn.click()
      await page.waitForSelector('.model-edit', { timeout: 3000 })
      const testBtn = await page.$('.model-edit .btn.tl')
      await testBtn.click()
      await page.waitForTimeout(1000)
      const result = await page.$eval('.test-result', (e) => e.textContent.trim())
      check('mock 模型测试成功', result.startsWith('✓'), result)
      await page.click('.model-edit .btn.save')
      await page.waitForTimeout(200)

      // 检查能力徽章
      const badges = await page.$$eval('.model-item.cur .cap-badge', (els) => els.map((e) => e.textContent.trim()))
      check('能力徽章显示', badges.length >= 3, badges.join(' / '))
    }
  }

  // 保存设置
  await page.click('.save-bar .btn.save')
  await page.waitForTimeout(500)
  const saveResult = await page.$eval('.save-bar .test-result', (e) => e.textContent.trim())
  check('保存设置', saveResult.startsWith('✓'), saveResult)

  // 验证后端确实保存了
  const r = await fetch(`${baseUrl}/api/v1/settings`, { headers: { 'X-TableTalk-Token': 'test-token' } })
  const settings = await r.json()
  const hasTestModel = settings.ai_models.some((m) => m.name === '测试模型')
  check('后端持久化新模型', hasTestModel, `${settings.ai_models.length} 个模型`)

  // console 错误检查
  check('无 console error', errs.length === 0, errs.slice(0, 3).join(' | '))

  // 截图
  const shotPath = path.resolve(root, '../docs/screenshots/model-manager.png')
  fs.mkdirSync(path.dirname(shotPath), { recursive: true })
  await page.screenshot({ path: shotPath, fullPage: false })
  check('截图保存', true, shotPath)

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
