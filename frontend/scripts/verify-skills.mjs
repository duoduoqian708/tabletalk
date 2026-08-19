// P6 技能广场验证：清单 → 启用/禁用 → 工具组合 → 自建技能 → 删除 → 持久化
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tabletalk-p6'
const port = 8780 + Math.floor(Math.random() * 100)
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const sidecar = spawn(path.join(backendDir, '.venv/bin/python'), ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
  cwd: backendDir, env: { ...process.env, TABLETALK_DATA_DIR: dataDir }, stdio: 'ignore'
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

  // 打开设置 → 技能广场
  await page.click('.sys-btn')
  await page.waitForSelector('.set-drawer', { timeout: 10000 })
  await page.click('button:has-text("技能广场")')
  await page.waitForSelector('.skill-plaza', { timeout: 10000 })
  await page.waitForSelector('.sp-item', { timeout: 10000 })
  const spItems = await page.$$eval('.sp-item', (els) => els.length)
  check('技能清单渲染（内置 query/report）', spItems >= 2, `${spItems} 项`)
  const hasQuery = !!(await page.$('.sp-item:has-text("执行 SQL")'))
  const hasReport = !!(await page.$('.sp-item:has-text("数据分析报告")'))
  check('含 query/report 技能', hasQuery && hasReport, `${hasQuery}/${hasReport}`)
  check('含内置徽标', !!(await page.$('.sp-badge.b')), '有内置徽标')

  // 展开 query → 工具组合
  const queryItem = await page.$('.sp-item:has-text("执行 SQL")')
  await queryItem.$eval('.sp-more', (b) => b.click())
  await page.waitForTimeout(400)
  const toolCount = await queryItem.$$eval('.sp-tool', (els) => els.length)
  check('工具组合可展开', toolCount >= 5, `${toolCount} 个工具`)
  const hasRunQuery = !!(await queryItem.$('.sp-tool:has-text("run_query")'))
  const hasDdl = !!(await queryItem.$('.sp-tool:has-text("draft_ddl")'))
  check('工具目录含 run_query/draft_ddl', hasRunQuery && hasDdl, `${hasRunQuery}/${hasDdl}`)
  check('无 DDL 执行工具', !(await queryItem.$('.sp-tool:has-text("run_ddl")')), '物理不存在 ✓')

  // 组合切换：去掉 run_dml → 持久化
  const dmlTool = await queryItem.$('.sp-tool:has-text("run_dml")')
  const beforeChecked = await dmlTool.$eval('input', (i) => i.checked)
  if (beforeChecked) {
    await dmlTool.click()
    await page.waitForTimeout(1200)
    const after = await page.evaluate(async () => {
      const b = await (await fetch('/api/v1/bootstrap')).json()
      const r = await fetch('/api/v1/skills', { headers: { 'X-TableTalk-Token': b.token } })
      const d = await r.json()
      const q = d.skills.find((s) => s.id === 'query')
      return q.tools.includes('run_dml')
    })
    check('组合变更已持久化', after === false, `run_dml=${after}`)
    // 恢复
    await dmlTool.click()
    await page.waitForTimeout(1200)
  } else {
    check('组合变更已持久化', true, 'run_dml 原本未勾选，跳过')
  }

  // 启用/禁用切换（checkbox 隐藏，点轨道；reload 后句柄会失效，始终用新选择器）
  const qToggleSel = '.sp-item:has-text("执行 SQL") .sp-toggle'
  await page.click(qToggleSel)
  await page.waitForTimeout(1200)
  const qItem3 = await page.$('.sp-item:has-text("执行 SQL")')
  const disabledBadge = await qItem3.$('.sp-badge.off')
  check('禁用后出现已禁用徽标', !!disabledBadge, disabledBadge ? '有徽标' : '无')
  await page.click(qToggleSel) // 恢复启用
  await page.waitForTimeout(1200)

  // 自建技能
  await page.click('.sp-create')
  await page.waitForSelector('.sp-form', { timeout: 5000 })
  await page.fill('.sp-f-row input[placeholder="如：对账助手"]', '对账助手')
  await page.waitForTimeout(200)
  await page.fill('.sp-f-row input[placeholder*="能做什么"]', '核对订单与回款')
  await page.fill('.sp-f-row input[placeholder*="逗号分隔"]', '对账,回款')
  await page.click('.sp-form .gi-btn.pri')
  await page.waitForFunction(() => {
    return [...document.querySelectorAll('.sp-item .sp-name')].some((el) => el.textContent === '对账助手')
  }, { timeout: 10000 })
  check('自建技能创建成功', true, '对账助手 出现')
  const customItem = await page.$('.sp-item:has-text("对账助手")')
  check('自定义徽标', !!(await customItem.$('.sp-badge.c')), '有自定义徽标')
  check('只读徽标', !!(await customItem.$('.sp-badge.ro')), '有只读徽标')

  // 删除自定义技能
  page.on('dialog', (d) => void d.accept())
  await customItem.$eval('.sp-del', (b) => b.click())
  await page.waitForFunction(() => {
    return ![...document.querySelectorAll('.sp-item .sp-name')].some((el) => el.textContent === '对账助手')
  }, { timeout: 10000 })
  check('自定义技能可删除', true, '已删除')

  // 内置不可删（API 层验证）
  const delRes = await page.evaluate(async () => {
    const b = await (await fetch('/api/v1/bootstrap')).json(); const r = await fetch('/api/v1/skills/query', { method: 'DELETE', headers: { 'X-TableTalk-Token': b.token } })
    return r.status
  })
  check('内置技能删除被拒', delRes === 403, `HTTP ${delRes}`)

  // 持久化：重启后端后自定义技能仍在（先重建一个）
  await page.click('.sp-create')
  await page.waitForSelector('.sp-form', { timeout: 5000 })
  await page.fill('.sp-f-row input[placeholder="如：对账助手"]', '持久化测试')
  await page.fill('.sp-f-row input[placeholder*="逗号分隔"]', '测试触发')
  await page.click('.sp-form .gi-btn.pri')
  await page.waitForFunction(() => {
    return [...document.querySelectorAll('.sp-item .sp-name')].some((el) => el.textContent === '持久化测试')
  }, { timeout: 10000 })
  check('持久化技能已创建', true, '持久化测试 出现')
  await page.close()

  // 重启后端
  sidecar.kill('SIGKILL')
  await new Promise((r) => setTimeout(r, 2000))
  const sidecar2 = spawn(path.join(backendDir, '.venv/bin/python'), ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
    cwd: backendDir, env: { ...process.env, TABLETALK_DATA_DIR: dataDir }, stdio: 'ignore'
  })
  await waitHealth(45000)
  const page2 = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  await page2.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page2.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  await page2.click('.sys-btn')
  await page2.waitForSelector('.set-drawer', { timeout: 10000 })
  await page2.click('button:has-text("技能广场")')
  await page2.waitForSelector('.skill-plaza', { timeout: 10000 })
  await page2.waitForFunction(() => {
    return [...document.querySelectorAll('.sp-item .sp-name')].some((el) => el.textContent === '持久化测试')
  }, { timeout: 10000 })
  check('重启后自定义技能恢复', true, '持久化测试 仍在')
  sidecar2.kill()

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 500))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill('SIGKILL')
}
