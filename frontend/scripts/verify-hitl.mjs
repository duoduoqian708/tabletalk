// P4 HITL 验证（确定性）：mock 写操作链路 —— 上下文筹码 → 黄卡风险面板 → 不自动执行 → 确认 → 留痕 → 审计落盘
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tabletalk-p4'
const port = 8772
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
  const boot = await (await fetch(`${baseUrl}/api/v1/bootstrap`)).json()
  const token = boot.token
  const connRes = await fetch(`${baseUrl}/api/v1/connections`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': token },
    body: JSON.stringify({ name: '可写演示库', dialect: 'sqlite', file: `${dataDir}/demo.db`, read_only: false })
  })
  check('创建可写连接', connRes.ok, String(connRes.status))

  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  page.on('pageerror', e => console.log('PAGEERROR:', String(e).slice(0, 200)))
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.g-node', { timeout: 60000 })
  await buildAndConfirm(page)  // 新连接 → 过构建门禁（不构建卡死）

  // ── 1. 上下文筹码：单击节点 → 筹码出现并可移除 ──
  await page.click('.g-node')
  await page.waitForSelector('.ctx-chip', { timeout: 10000 })
  check('选中节点出现上下文筹码', true, '有筹码')
  const chipTxt = await page.textContent('.ctx-chip')
  check('筹码显示表名', /上下文：\S+/.test(chipTxt || ''), chipTxt?.slice(0, 30) || '—')
  await page.click('.ctx-x')
  await page.waitForTimeout(300)
  check('筹码可移除', !(await page.$('.ctx-chip')), '已移除')

  // ── 2. 只读查询自动执行（mock 触发词：退货率） ──
  await page.click('.compose-input')
  await page.type('.compose-input', '退货率最高的 10 个产品')
  await page.click('.airail .send')
  await page.waitForSelector('.sql', { timeout: 30000 })
  await page.waitForSelector('.ws-tab', { timeout: 30000 })
  check('只读查询自动执行进结果区', true, '有标签')

  // ── 3. DML HITL 链路（mock 触发词：库存为 0 → UPDATE） ──
  await page.click('.compose-input')
  await page.type('.compose-input', '给库存为 0 的产品涨价 10%')
  await page.click('.airail .send')
  await page.waitForSelector('.sql.review', { timeout: 40000 }).catch(() => {})
  check('写操作出黄卡(review)', !!(await page.$('.sql.review')), (await page.$('.sql.review')) ? '有黄卡' : '无黄卡')

  const hasRisk = !!(await page.$('.risk-panel'))
  check('风险评估面板默认展开', hasRisk, hasRisk ? '有面板' : '无面板')
  if (hasRisk) {
    const riskTxt = await page.textContent('.risk-panel')
    check('风险面板含影响行数/不可回滚', /行/.test(riskTxt || '') && /回滚/.test(riskTxt || ''), riskTxt?.slice(0, 70) || '—')
  }
  const autoRan = !!(await page.$('.exec-stamp'))
  check('写操作未自动执行', !autoRan, autoRan ? '被自动执行了!' : '未自动执行 ✓')

  await page.click('.sql.review .btn.warn')
  await page.waitForSelector('.exec-stamp', { timeout: 30000 }).catch(() => {})
  const hasStamp = !!(await page.$('.exec-stamp'))
  check('确认后留痕标记出现', hasStamp, hasStamp ? '有留痕' : '无留痕')
  if (hasStamp) {
    const stamp = await page.textContent('.exec-stamp')
    check('留痕含影响行数与审计字样', /行/.test(stamp || '') && /审计/.test(stamp || ''), stamp?.slice(0, 60) || '—')
  }

  // ── 4. 审计落盘（确认执行 = tier dml + status 已确认执行） ──
  await page.waitForTimeout(800)
  const auditPath = path.join(dataDir, 'audit.log')
  let executedEntry = ''
  if (fs.existsSync(auditPath)) {
    const lines = fs.readFileSync(auditPath, 'utf8').split('\n').filter(Boolean)
    executedEntry = lines.filter((l) => l.includes('"tier": "dml"') && l.includes('UPDATE')).pop() ?? ''
  }
  check('审计日志含已确认 UPDATE 留痕', executedEntry.includes('已确认执行'), executedEntry ? `found: ${executedEntry.slice(0, 90)}` : '未找到')

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 500))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
