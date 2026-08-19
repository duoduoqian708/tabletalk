// P5 知识治理验证：治理模式统计条 → 检查器写注释(confirmed) → 加标签(draft) → 确认标签 → 节点状态刷新
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-p5'
const port = 8774
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
  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  page.on('pageerror', e => console.log('PAGEERROR:', String(e).slice(0, 200)))
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  const onboard = await page.$('.onboarding .primary')
  if (onboard) { await onboard.click(); await page.waitForSelector('.appbar', { timeout: 20000 }) }
  await buildAndConfirm(page)
  await page.waitForSelector('.g-node', { timeout: 60000 })

  // ── 1. 治理模式统计条 ──
  await page.click('.g-mode-btn:has-text("治理")')
  await page.waitForSelector('.g-gov-bar', { timeout: 10000 })
  check('治理统计条出现', true, '有统计条')
  const statTxt = await page.textContent('.g-gov-bar')
  check('统计条含未注释表', /未注释表/.test(statTxt || ''), statTxt?.slice(0, 60) || '—')

  // ── 2. 检查器治理形态：注释编辑框 + 标签添加框 ──
  await page.click('.g-node')
  await page.waitForSelector('.g-inspector', { timeout: 10000 })
  const hasNoteTa = !!(await page.$('.gi-note-ta'))
  const hasTagAdd = !!(await page.$('.gi-tag-add'))
  check('治理检查器含注释编辑', hasNoteTa, hasNoteTa ? '有编辑框' : '无')
  check('治理检查器含标签添加', hasTagAdd, hasTagAdd ? '有添加框' : '无')
  const hasGovTag = !!(await page.$('.gi-gov'))
  check('检查器带治理徽标', hasGovTag, hasGovTag ? '有徽标' : '无')

  // ── 3. 写注释 → 保存 → confirmed 展示 ──
  await page.fill('.gi-note-ta', '测试注释：这是 P5 验证写入的注释。')
  await page.click('.gi-note-edit .gi-btn.pri')
  await page.waitForFunction(
    () => document.querySelector('.gi-comment')?.textContent?.includes('P5 验证写入') ?? false,
    { timeout: 15000 }
  )
  check('注释保存成功', true, '注释已更新')
  const cTxt = await page.textContent('.gi-comment')
  check('注释内容正确', (cTxt || '').includes('P5 验证写入'), cTxt?.slice(0, 40) || '—')
  check('注释为已确认态', !(await page.$('.gi-comment.draft')), '非 draft')

  // ── 4. 添加标签（draft）→ 统计条更新 → 确认标签 ──
  await page.fill('.gi-tag-input', '测试域')
  await page.click('.gi-tag-add .gi-btn')
  await page.waitForSelector('.gi-tag', { timeout: 15000 }).catch(() => {})
  const hasTag = !!(await page.$('.gi-tag'))
  check('标签添加成功', hasTag, hasTag ? '有标签' : '无标签')
  await page.waitForTimeout(1200)
  const stat2 = await page.textContent('.g-gov-bar')
  const tagStat = stat2?.match(/待确认标签\s*(\d+)/)?.[1] ?? null
  check('待确认标签计数=1', tagStat === '1', tagStat === null ? '无统计' : `待确认标签 ${tagStat}`)

  // 确认标签
  const tagOk = await page.$('.gi-tag-act.ok')
  if (tagOk) {
    await tagOk.click()
    await page.waitForTimeout(1500)
    const hasDraftTag = await page.$('.gi-tag.draft')
    check('确认后标签转正', !hasDraftTag, hasDraftTag ? '仍是 draft' : '已确认 ✓')
  } else {
    check('标签确认按钮存在', false, '无确认按钮')
  }

  // ── 5. 图谱节点状态刷新（注释后应为已注释样式） ──
  await page.waitForTimeout(1000)
  const nodeState = await page.evaluate(() => {
    const g = document.querySelector('.g-node.sel')
    return g ? (g.querySelector('text:last-of-type')?.textContent ?? '') : ''
  })
  check('节点状态行刷新', nodeState.includes('✓'), nodeState || '—')

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 500))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
