// WS2 T2.4 e2e：C2 建议只引用 enabled 技能——关闭 report 后建议列表不再含报告类问题。
// 用法：node scripts/verify-t24-c2filter.mjs
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tabletalk-t24'
const port = 8776
const baseUrl = `http://127.0.0.1:${port}`
fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const py = fs.existsSync(path.join(backendDir, '.venv/bin/python')) ? path.join(backendDir, '.venv/bin/python') : 'python3'
const sidecar = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], { cwd: backendDir, env: { ...process.env, TABLETALK_DATA_DIR: dataDir }, stdio: 'ignore' })
async function waitHealth(t = 30000) { const d = Date.now() + t; while (Date.now() < d) { try { const r = await fetch(`${baseUrl}/api/v1/health`); if (r.ok) return } catch {} await new Promise(r => setTimeout(r, 500)) } throw new Error('health timeout') }
let pass = 0, fail = 0
function check(n, c, g) { if (c) { pass++; console.log(`PASS ${n} → ${g}`) } else { fail++; console.log(`FAIL ${n} → ${g}`) } }

async function collectSugs(page) {
  await page.click('.compose-input').catch(() => {})
  await page.waitForSelector('.ai-panel', { state: 'attached', timeout: 5000 }).catch(() => {})
  await page.waitForSelector('.ah-sug', { state: 'attached', timeout: 5000 }).catch(() => {})
  await page.waitForTimeout(400)
  return await page.$$eval('.ah-sug', els => els.map(e => e.textContent?.trim() || '')).catch(() => [])
}

const browser = await chromium.launch()
try {
  await waitHealth()
  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  const onboard = await page.$('.onboarding .primary')
  if (onboard) { await onboard.click(); await page.waitForSelector('.appbar', { timeout: 20000 }) }
  await buildAndConfirm(page)
  await page.waitForSelector('.graph3d', { timeout: 20000 })
  const token = await page.evaluate(async () => (await (await fetch('/api/v1/bootstrap')).json()).token)
  const headers = { 'X-TableTalk-Token': token, 'Content-Type': 'application/json' }

  // 前置：skill 表里 report 为 enabled
  const skills0 = await (await fetch(`${baseUrl}/api/v1/skills`, { headers })).json()
  const report0 = skills0.skills.find(s => s.id === 'report')
  check('report 技能默认启用', report0?.enabled === true, String(report0?.enabled))

  // 领域标签不是构建时自动生成，需先 annotate-tags（mock 同步快速），再确认一个 draft
  const conns = await (await fetch(`${baseUrl}/api/v1/connections`, { headers })).json()
  const connId = Array.isArray(conns) ? conns[0]?.id : (conns.connections?.[0]?.id)
  await fetch(`${baseUrl}/api/v1/knowledge/${connId}/annotate-tags`, { method: 'POST', headers }).catch(() => {})
  const tags0 = await (await fetch(`${baseUrl}/api/v1/knowledge/${connId}/tags`, { headers })).json()
  const draft = (tags0.library || []).find(t => t.status !== 'confirmed') || (tags0.library || [])[0]
  let confirmedTag = null
  if (draft?.name) {
    await fetch(`${baseUrl}/api/v1/knowledge/${connId}/tags/confirm`, { method: 'POST', headers, body: JSON.stringify({ name: draft.name }) }).catch(() => {})
    confirmedTag = draft.name
  }
  check('至少一个领域标签已确认', !!confirmedTag, confirmedTag || '无标签')

  // 重载使 AiRail 的 dynamicSugs effect（依赖 currentId/enabledSkills）重跑并拉到刚确认的标签
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.graph3d', { timeout: 20000 })
  await page.waitForTimeout(600)

  // ——启用态：建议列表含报告类问题 ——
  const sugsOn = await collectSugs(page)
  const hasReportOn = sugsOn.some(s => s.includes('报告'))
  check('report 启用时建议含报告类问题', hasReportOn, sugsOn.join(' | ').slice(0, 120))

  // ——关闭 report 技能 ——
  const up = await fetch(`${baseUrl}/api/v1/skills/report`, { method: 'PUT', headers, body: JSON.stringify({ enabled: false }) })
  check('PUT 关闭 report 成功', up.ok, String(up.status))
  const skills1 = await (await fetch(`${baseUrl}/api/v1/skills`, { headers })).json()
  check('GET 反映 report 已禁用', skills1.skills.find(s => s.id === 'report')?.enabled === false, String(skills1.skills.find(s => s.id === 'report')?.enabled))

  // 重载页面 → 重渲染建议（enabledSkills 重新拉取）
  await page.reload({ waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.graph3d', { timeout: 20000 })
  const sugsOff = await collectSugs(page)
  const hasReportOff = sugsOff.some(s => s.includes('报告') || /report/i.test(s))
  check('关闭 report 后建议不含报告类问题', !hasReportOff, sugsOff.join(' | ').slice(0, 120))
  // 兜底仍应在（query 常开）
  check('query 兜底建议仍在', sugsOff.length >= 1, `count=${sugsOff.length}`)

  // 复原开关，避免污染
  await fetch(`${baseUrl}/api/v1/skills/report`, { method: 'PUT', headers, body: JSON.stringify({ enabled: true }) }).catch(() => {})

  console.log(`\nT2.4 C2 建议过滤: ${pass} passed, ${fail} failed`)
} finally {
  sidecar.kill('SIGTERM')
  process.exit(fail ? 1 : 0)
}