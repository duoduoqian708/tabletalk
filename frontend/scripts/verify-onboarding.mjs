// 接入流程 + 知识库合并页验证：新建连接 → 构建门禁 → 进度条 → 确认闸 → 知识库页两栏
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/cleared-onboard'
const port = 8768
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

  // 1. 空状态 → 使用演示库（创建连接后应弹构建门禁）
  const onboard = await page.$('.onboarding .primary')
  if (onboard) await onboard.click()
  await page.waitForSelector('.kb-gate', { timeout: 15000 })
  check('构建门禁弹出（未构建卡死）', true, 'kb-gate visible')
  const gateBadge = await page.textContent('.kb-gate .kb-badge')
  check('门禁徽标=未构建', gateBadge.includes('未构建'), gateBadge)

  // 2. 点「开始构建」→ 进度条出现
  await page.click('.kb-gate .btn.save')
  await page.waitForSelector('.kb-bar', { timeout: 8000 })
  check('构建进度条出现', true, '.kb-bar')
  // 等构建完成（门禁变为待确认）
  await page.waitForSelector('.kb-badge.pending', { timeout: 60000 })
  check('构建完成 → 待确认', true, 'pending badge')

  // 3. 确认闸：一键确认启用 → 门禁消失
  await page.click('.kb-gate .btn.save')
  await page.waitForSelector('.kb-gate', { state: 'detached', timeout: 15000 }).catch(() => {})
  const gateGone = (await page.$('.kb-gate')) === null
  check('确认后门禁消失（ready）', gateGone, gateGone ? 'gate detached' : 'still visible')

  // 4. 知识库页：两栏布局 + 图预览
  await page.click('.nav .tab >> text=知识库')
  await page.waitForSelector('.kb-cols', { timeout: 10000 })
  check('知识库页两栏布局', true, '.kb-cols')
  const badgeReady = await page.textContent('.kb-topbar .kb-badge')
  check('顶栏徽标=已就绪', badgeReady.includes('已就绪'), badgeReady)
  const kgCanvas = await page.$('.kg2-svg')
  check('右区图画布', kgCanvas !== null, 'kg2-svg')
  const left = await page.$('.kb-left')
  check('左区内容区', left !== null, 'kb-left')
  const fkBtn = await page.textContent('.kg2-btn.on')
  check('关系过滤 FK 默认开', fkBtn === 'FK', fkBtn)
  const stats = await page.textContent('.kb-stats')
  check('底部统计', stats.includes('文档') && stats.includes('标签') && stats.includes('关系'), stats.replace(/\n/g, ' '))

  // 5. 图节点点击 → 浮层详情
  await page.click('.kg2-node circle')
  await page.waitForSelector('.kg2-inspector', { timeout: 5000 })
  check('节点浮层出现', true, 'kg2-inspector')
  const inspTitle = await page.textContent('.kg2-insp-head .mono')
  check('浮层表名', inspTitle.length > 0, inspTitle)

  // 6. 文档编辑：✎ 打开编辑器
  await page.click('.rv-table .mini-acts button >> nth=0')
  await page.waitForSelector('.kb-edit-ta', { timeout: 5000 })
  check('注释编辑器打开', true, '.kb-edit-ta')
  await page.click('.kb-edit-acts .btn.ghost') // 取消

  await page.screenshot({ path: '/tmp/onboard-kb-page.png' })
} catch (e) {
  fail++
  console.log(`FAIL 异常 → ${e.message}`)
  await browser.newPage().catch(() => {})
  try {
    const p = await (await browser.contexts())[0]?.pages()[0]
    if (p) await p.screenshot({ path: '/tmp/onboard-fail.png' })
  } catch { }
} finally {
  console.log(`\n${pass} passed, ${fail} failed`)
  sidecar.kill('SIGKILL')
  await browser.close()
  process.exit(fail > 0 ? 1 : 0)
}
