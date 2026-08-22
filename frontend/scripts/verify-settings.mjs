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
    body: JSON.stringify({ name: '订单库', dialect: 'sqlite', file: demoFile, read_only: true, password: 'hunter2' }),
  })
  await fetch(`${baseUrl}/api/v1/connections`, {
    method: 'POST', headers: h,
    body: JSON.stringify({ name: '备份库', dialect: 'sqlite', file: demoFile, read_only: false, sensitive: ['payroll_*'] }),
  })
  await fetch(`${baseUrl}/api/v1/connections`, {
    method: 'POST', headers: h,
    body: JSON.stringify({ name: '订单PG', dialect: 'postgres', host: '127.0.0.1', port: 5432, database: 'orders', user: 'postgres', password: 'pg-secret', read_only: false }),
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
  check('三张连接卡', rows.length === 3, `got ${rows.length}`)

  // 卡1（订单库，当前）结构
  const card1 = rows[0]
  check('卡1 高亮为当前 (cur)', (await card1.getAttribute('class')).includes('cur'))
  check('卡1 名称渲染', (await card1.$eval('.conn-name', (e) => e.textContent)) === '订单库')
  check('卡1 方言 chip', (await card1.$eval('.conn-dialect', (e) => e.textContent.trim())) === 'sqlite')
  check('卡1 目标行含 demo.db', ((await card1.$eval('.conn-target .ct-line', (e) => e.textContent)) || '').includes('demo.db'))
  check('卡1 无旧状态点 .st', (await card1.$('.st')) === null)
  const btns1 = await card1.$$eval('.conn-actions .mini-btn', (bs) => bs.map((b) => b.textContent.trim()))
  check('卡1 动作 2×2 四按钮', btns1.length === 4, JSON.stringify(btns1))
  check('卡1 无「当前使用」标签（绿框已表意）', (await card1.$('.cur-tag')) === null)
  // 名称独占一行、完整展示；下面才是标签栏
  const line1Kids = await card1.$$eval('.conn-line1 > *', (els) => els.map((e) => e.className))
  check('卡1 名称独占一行（仅名称）', line1Kids.length === 1 && line1Kids[0] === 'conn-name', JSON.stringify(line1Kids))
  const tags = await card1.$$eval('.conn-tags > *', (els) => els.map((e) => e.className))
  check('卡1 标签栏 = 方言 chip + 只读', tags[0].startsWith('conn-dialect') && tags[1].startsWith('ro-tag'), JSON.stringify(tags))
  const box = await card1.boundingBox()
  check('卡片高度加大（≥160px）', (box?.height ?? 0) >= 160, `h=${box?.height}`)
  check('卡1 无黄色默认标签（默认在按钮上表达）', (await card1.$('.def-tag')) === null)
  // 删除按钮常显红框背景（不依赖 hover）
  const delStyle = await rows[0].$eval('.mini-btn.dang', (b) => {
    const s = getComputedStyle(b)
    return { border: s.borderColor, bg: s.backgroundColor }
  })
  check('删除按钮常显红框背景', delStyle.border !== 'rgba(0, 0, 0, 0)' && delStyle.bg !== 'rgba(0, 0, 0, 0)', JSON.stringify(delStyle))

  // 卡2：敏感名单展示 + 非当前
  const card2 = rows[1]
  check('卡2 非当前', !(await card2.getAttribute('class')).includes('cur'))
  const sensText = await card2.$eval('.conn-sens .sens-list', (e) => e.textContent)
  check('卡2 敏感名单行展示', (sensText || '').includes('payroll_*'), String(sensText))
  check('卡2 目标行', ((await card2.$eval('.conn-target .ct-line', (e) => e.textContent)) || '').includes('demo.db'))
  // 卡1 无敏感配置 → 敏感行应隐藏
  check('卡1 无敏感 → 行隐藏', (await card1.$('.conn-sens')) === null)

  // 卡3（PG 配置）：地址一行、库名单独一行
  const card3 = rows[2]
  const lines = await card3.$$eval('.conn-target .ct-line', (ls) => ls.map((l) => l.textContent))
  check('卡3 地址/库名分行（两行）', lines.length === 2, JSON.stringify(lines))
  check('卡3 地址行 host:port', (lines[0] || '').includes('127.0.0.1:5432'), JSON.stringify(lines))
  check('卡3 库名行 db:orders', (lines[1] || '').includes('db:orders'), JSON.stringify(lines))

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
  const defBtnCls = await rows[0].$$eval('.conn-actions .mini-btn', (bs) => bs[1].className)
  check('卡1 设默认后按钮呈 set 样式（默认标识在按钮上）', defBtnCls.includes('set'), defBtnCls)
  check('卡1 设默认后仍无黄色默认标签', (await rows[0].$('.def-tag')) === null)
  const stored = await page.evaluate(() => localStorage.getItem('tabletalk-default-conn'))
  check('默认已持久化到 localStorage', !!stored)

  console.log('== 编辑弹窗：密码占位 + 保存置灰 ==')
  // 点卡3（PG，已存密码）「编辑」→ 弹窗打开（onEditConnection 会先关抽屉；sqlite 无密码框，须用 PG 卡）
  const rowsNow = await page.$$('.conn-row')
  await rowsNow[2].$$eval('.conn-actions .mini-btn', (bs) => bs[2].click())
  await page.waitForSelector('.modal input[type=password]', { timeout: 10000 })
  await page.waitForTimeout(400)
  const pwdInput = await page.$('.modal input[type=password]')
  const pwdVal = pwdInput ? await pwdInput.inputValue() : null
  check('编辑弹窗密码框为 *** 占位（真值不出网）', pwdVal === '••••••••', `got ${JSON.stringify(pwdVal)}`)
  if (pwdInput) {
    await pwdInput.focus()
    await page.waitForTimeout(200)
    const after = await pwdInput.inputValue()
    check('聚焦后占位清空（可直接输入新密码）', after === '')
  }
  // 保存按钮：未测试 → 置灰不可点（有提示）
  const saveBtn = await page.$('.modal .btn.save')
  const saveDisabled1 = saveBtn ? await saveBtn.isDisabled() : true
  check('未测试时保存按钮置灰不可点', saveDisabled1 === true)
  const saveOpacity = saveBtn ? await saveBtn.evaluate((b) => getComputedStyle(b).opacity) : '0'
  check('置灰样式（opacity 降低）', parseFloat(saveOpacity) < 1, `opacity=${saveOpacity}`)
  const saveTitle = saveBtn ? await saveBtn.getAttribute('title') : ''
  check('置灰时有提示（先测试再保存）', (saveTitle || '').includes('测试'), saveTitle)
  // PG 无服务器 → 测试失败 → 保存仍不可点（必须测试通过才能保存）
  await page.click('.modal .btn.tl')
  await page.waitForTimeout(2000)
  const saveDisabled2 = saveBtn ? await saveBtn.isDisabled() : true
  check('测试失败 → 保存仍置灰', saveDisabled2 === true)
  await page.click('.modal .close')
  await page.waitForTimeout(400)

  console.log('== 测试标签（重开抽屉）==')
  await page.click('.appbar .sys-btn')
  await page.waitForSelector('.set-drawer .conn-row', { timeout: 10000 })
  await page.waitForTimeout(400)
  const rowsB = await page.$$('.conn-row')
  check('测试按钮为普通按钮（无 ok/fail 高亮类）', (await rowsB[0].$('.mini-btn.test.ok, .mini-btn.test.fail')) === null)
  // 点卡1（sqlite 可读）测试 → 「测试通过」标签点亮
  await rowsB[0].$$eval('.conn-actions .mini-btn', (bs) => bs[0].click())
  await page.waitForTimeout(900)
  check('测试通过标签点亮', (await rowsB[0].$('.ok-tag')) !== null)
  check('测试通过标签文案', ((await rowsB[0].$eval('.ok-tag', (e) => e.textContent)) || '').includes('测试通过'))

  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILURES`)
  process.exitCode = failures === 0 ? 0 : 1
} finally {
  await browser.close()
  sidecar.kill()
}
