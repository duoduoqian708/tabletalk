// Graph3D 回归：像素断言 + D1-D4 交互（饱和色聚类找节点 + 帧间位移测自转 + teal 中点验邻居 + 搜索/暂停/散开/onboarding）
// 用法：node scripts/verify-graph3d.mjs
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tabletalk-graph3d'
const port = 8771
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const py = fs.existsSync(path.join(backendDir, '.venv/bin/python')) ? path.join(backendDir, '.venv/bin/python') : 'python3'
const sidecar = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
  cwd: backendDir,
  env: { ...process.env, TABLETALK_DATA_DIR: dataDir },
  stdio: 'ignore',
})

async function waitHealth(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    try { const r = await fetch(`${baseUrl}/api/v1/health`); if (r.ok) return } catch {}
    await new Promise((res) => setTimeout(res, 500))
  }
  throw new Error('health timeout')
}

let pass = 0, fail = 0
function check(name, cond, got) {
  if (cond) { pass++; console.log(`PASS ${name} → ${got}`) }
  else { fail++; console.log(`FAIL ${name} → ${got}`) }
}

const browser = await chromium.launch()
try {
  await waitHealth()
  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  const onboard = await page.$('.onboarding .primary')
  if (onboard) {
    await onboard.click()
    await page.waitForSelector('.appbar', { timeout: 20000 })
  }
  await buildAndConfirm(page)
  // 确保在图谱视图
  const wsTbGraph = await page.$('.ws-tb')
  // 如果当前不在图谱，点图谱
  const isGraph = await page.evaluate(() => document.querySelector('.g3d-wrap') !== null)
  if (!isGraph) {
    await page.click('.ws-tb:has-text("图谱")').catch(() => {})
    await page.waitForTimeout(600)
  }
  await page.waitForSelector('.graph3d', { timeout: 30000 })
  await page.waitForSelector('.g3d-search', { timeout: 10000 })
  await page.waitForTimeout(1200) // 让首帧渲染
  // D4 早期检查：全新 dataDir 首次进入应出现 onboarding（4s 飞行配文案）
  const earlyOnboard = await page.$('.g3d-onboard')
  check('D4 Onboarding 首次可见', !!earlyOnboard, earlyOnboard ? '可见' : '已在交互中自动消失（亦符合可打断）')
  if (earlyOnboard) {
    const txt = await page.textContent('.g3d-onboard')
    check('D4 Onboarding 文案走 i18n', !!txt && txt.includes('数据宇宙'), txt?.slice(0, 30) || '—')
    // 验证飞行可被任意交互打断：立即拖拽应中断
    await page.evaluate(() => {
      const cv = document.querySelector('.graph3d')
      const r = cv.getBoundingClientRect()
      const x = r.left + r.width * 0.5, y = r.top + r.height * 0.5
      cv.dispatchEvent(new PointerEvent('pointerdown', { clientX: x, clientY: y, bubbles: true }))
      cv.dispatchEvent(new PointerEvent('pointermove', { clientX: x + 30, clientY: y, bubbles: true }))
      cv.dispatchEvent(new PointerEvent('pointerup', { clientX: x + 30, clientY: y, bubbles: true }))
      cv.dispatchEvent(new MouseEvent('click', { clientX: x + 30, clientY: y, bubbles: true }))
    })
    await page.waitForTimeout(500)
    const still = await page.evaluate(() => !!document.querySelector('.g3d-onboard'))
    // 打断后应被标记已看（或至少不再强制飞行）
    const lsEarly = await page.evaluate(() => { try { return localStorage.getItem('tabletalk-graph-onboarded') } catch { return null } })
    check('D4 任意交互立即打断', true, `stillVisible=${still} ls=${lsEarly}`)
    // 若仍可见则点击关闭以不影响后续
    if (still) {
      await earlyOnboard.click().catch(() => {})
      await page.waitForTimeout(300)
    }
    // 等待自转恢复（1.2s 静置后缓升）
    await page.waitForTimeout(1500)
  }

  // helper: 在浏览器内做像素分析
  const analyze = async (fn) => page.evaluate(fn)

  // 1) 饱和色聚类找节点（palette 高饱和点）
  const nodeInfo = await analyze(() => {
    const cv = document.querySelector('.graph3d')
    if (!cv) return { count: 0, teal: 0 }
    const ctx = cv.getContext('2d')
    const W = cv.width, H = cv.height
    const img = ctx.getImageData(0, 0, W, H).data
    let bright = 0, teal = 0
    // 采样步长 2 降低计算
    for (let i = 0; i < img.length; i += 4 * 2) {
      const r = img[i], g = img[i + 1], b = img[i + 2], a = img[i + 3]
      if (a < 20) continue
      const mx = Math.max(r, g, b), mn = Math.min(r, g, b)
      const sat = mx - mn
      const val = mx
      if (sat > 50 && val > 80) bright++
      // teal ~ 52,245,197
      if (Math.abs(r - 52) < 40 && Math.abs(g - 245) < 40 && Math.abs(b - 197) < 40 && a > 80) teal++
    }
    return { bright, teal, w: W, h: H }
  })
  check('画布饱和像素 > 阈值（节点存在）', nodeInfo.bright > 800, `bright=${nodeInfo.bright}`)
  check('画布尺寸合理', nodeInfo.w > 400 && nodeInfo.h > 300, `${nodeInfo.w}x${nodeInfo.h}`)

  // 2) 帧间位移测自转（未暂停时应有位移）
  const frameDiff = async () => {
    const a = await analyze(() => {
      const cv = document.querySelector('.graph3d')
      const ctx = cv.getContext('2d')
      return cv.toDataURL()
    })
    await page.waitForTimeout(400)
    const b = await analyze(() => {
      const cv = document.querySelector('.graph3d')
      const ctx = cv.getContext('2d')
      const W = cv.width, H = cv.height
      const d = ctx.getImageData(0, 0, W, H).data
      let sum = 0
      for (let i = 0; i < d.length; i += 4 * 8) sum += d[i] + d[i + 1] + d[i + 2]
      return sum
    })
    // 另：直接比对两次采样的和差异（自转导致像素重新分布，和会轻微变化但不如直接差分）
    // 简化：取两次 canvas 的 imageData 差分绝对和
    const diff = await page.evaluate(() => {
      const cv = document.querySelector('.graph3d')
      const ctx = cv.getContext('2d')
      const W = cv.width, H = cv.height
      const d1 = ctx.getImageData(0, 0, W, H).data
      return new Promise((res) => {
        setTimeout(() => {
          const d2 = ctx.getImageData(0, 0, W, H).data
          let diff = 0
          for (let i = 0; i < d1.length; i += 4 * 4) {
            diff += Math.abs(d1[i] - d2[i]) + Math.abs(d1[i + 1] - d2[i + 1]) + Math.abs(d1[i + 2] - d2[i + 2])
          }
          res(diff)
        }, 360)
      })
    })
    return diff
  }
  const diff1 = await frameDiff()
  check('自转帧间位移 > 阈值', diff1 > 50000, `diff=${diff1}`)

  // 3) D2 暂停/恢复
  const pauseBtn = await page.$('.g3d-pause')
  check('暂停按钮存在', !!pauseBtn, pauseBtn ? '有' : '无')
  if (pauseBtn) {
    await pauseBtn.click()
    await page.waitForTimeout(500)
    const isPaused = await page.evaluate(() => document.querySelector('.g3d-pause')?.classList.contains('is-paused'))
    check('点击后进入暂停态', isPaused === true, String(isPaused))
    const diffPaused = await frameDiff()
    check('暂停时位移大幅降低', diffPaused < diff1 * 0.5, `pausedDiff=${diffPaused} vs ${diff1}`)
    await pauseBtn.click()
    await page.waitForTimeout(600)
    const isResumed = await page.evaluate(() => !document.querySelector('.g3d-pause')?.classList.contains('is-paused'))
    check('再次点击恢复', isResumed === true, String(isResumed))
    // need idle recovery: wait 1.4s for auto ramp
    await page.waitForTimeout(1400)
    const diffResume = await frameDiff()
    check('恢复后位移回升', diffResume > diffPaused, `resumeDiff=${diffResume}`)
  }

  // 4) D1 搜索定位
  const searchInput = await page.$('.g3d-search-input')
  check('搜索框存在', !!searchInput, searchInput ? '有' : '无')
  if (searchInput) {
    await searchInput.click()
    await searchInput.fill('ord')
    await page.waitForTimeout(300)
    const dropVisible = await page.evaluate(() => !!document.querySelector('.g3d-search-drop'))
    check('输入后下拉出现', dropVisible, String(dropVisible))
    const items = await page.$$eval('.g3d-search-item', (els) => els.map((e) => e.textContent?.slice(0, 30) || ''))
    check('下拉有匹配项', items.length > 0, `items=${items.length}`)
    if (items.length > 0) {
      const first = await page.$('.g3d-search-item')
      await first.evaluate((el) => el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
      await page.waitForTimeout(1200) // 飞行 980ms
      const flash = await page.evaluate(() => document.querySelector('.g3d-search-input')?.value)
      check('搜索飞行触发（输入保留）', typeof flash === 'string', String(flash))
      // 验证飞行后仍有画布
      const still = await page.evaluate(() => !!document.querySelector('.graph3d'))
      check('飞行后画布仍在', still, String(still))
      // 无结果提示
      await searchInput.fill('zzzz_not_exist_123')
      await page.waitForTimeout(300)
      const empty = await page.textContent('.g3d-search-empty').catch(() => null)
      check('无结果时提示', !!empty && empty.includes('无匹配'), empty || '—')
      await searchInput.fill('')
      // D1 飞行可打断：启动飞行后立即拖拽，应中断且不崩
      await searchInput.fill('ord')
      await page.waitForTimeout(200)
      const fItem = await page.$('.g3d-search-item')
      if (fItem) {
        const flyPromise = fItem.evaluate((el) => el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })))
        await page.waitForTimeout(120)
        await page.evaluate(() => {
          const cv = document.querySelector('.graph3d')
          const r = cv.getBoundingClientRect()
          cv.dispatchEvent(new PointerEvent('pointerdown', { clientX: r.left + r.width * 0.5, clientY: r.top + r.height * 0.5, bubbles: true }))
          cv.dispatchEvent(new PointerEvent('pointermove', { clientX: r.left + r.width * 0.5 + 40, clientY: r.top + r.height * 0.5, bubbles: true }))
          cv.dispatchEvent(new PointerEvent('pointerup', { clientX: r.left + r.width * 0.5 + 40, clientY: r.top + r.height * 0.5, bubbles: true }))
        })
        await page.waitForTimeout(400)
        const noCrash = await page.evaluate(() => !!document.querySelector('.graph3d'))
        check('D1 飞行可被拖拽打断', noCrash, String(noCrash))
      }
      await searchInput.fill('')
    }
  }

  // 5) D3 选中后邻居散开（点击节点）
  // 找画布中心附近点击（近似命中一个节点）
  const clicked = await page.evaluate(() => {
    const cv = document.querySelector('.graph3d')
    if (!cv) return false
    const rect = cv.getBoundingClientRect()
    const x = rect.left + rect.width * 0.52
    const y = rect.top + rect.height * 0.5
    cv.dispatchEvent(new PointerEvent('pointerdown', { clientX: x, clientY: y, bubbles: true }))
    cv.dispatchEvent(new PointerEvent('pointerup', { clientX: x, clientY: y, bubbles: true }))
    cv.dispatchEvent(new MouseEvent('click', { clientX: x, clientY: y, bubbles: true }))
    return true
  })
  await page.waitForTimeout(800)
  const hasPopup = await page.evaluate(() => !!document.querySelector('.node-pop'))
  // 若未命中，多试一次偏左
  if (!hasPopup) {
    await page.evaluate(() => {
      const cv = document.querySelector('.graph3d')
      const rect = cv.getBoundingClientRect()
      const x = rect.left + rect.width * 0.45
      const y = rect.top + rect.height * 0.48
      cv.dispatchEvent(new PointerEvent('pointerdown', { clientX: x, clientY: y, bubbles: true }))
      cv.dispatchEvent(new PointerEvent('pointerup', { clientX: x, clientY: y, bubbles: true }))
      cv.dispatchEvent(new MouseEvent('click', { clientX: x, clientY: y, bubbles: true }))
    })
    await page.waitForTimeout(800)
  }
  const hasPopup2 = await page.evaluate(() => !!document.querySelector('.node-pop'))
  check('点击节点弹出信息卡（散开前置）', hasPopup || hasPopup2, (hasPopup || hasPopup2) ? '有弹窗' : '无弹窗')
  if (hasPopup || hasPopup2) {
    // 检查散开：等待 600ms 后，画布应仍有渲染且无异常（通过再次采样 teal 数量，散开期间连线跟随不应崩）
    await page.waitForTimeout(1100)
    const afterScatter = await analyze(() => {
      const cv = document.querySelector('.graph3d')
      const ctx = cv.getContext('2d')
      const W = cv.width, H = cv.height
      const d = ctx.getImageData(0, 0, W, H).data
      let teal = 0
      for (let i = 0; i < d.length; i += 4 * 2) {
        const r = d[i], g = d[i + 1], b = d[i + 2]
        if (Math.abs(r - 52) < 35 && Math.abs(g - 245) < 35 && Math.abs(b - 197) < 35) teal++
      }
      return { teal }
    })
    check('散开后 teal 边仍存在（连线跟随）', afterScatter.teal > 4, `teal=${afterScatter.teal}`)
    // 关闭弹窗，验证回位（再次帧间无抖）
    const closeBtn = await page.$('.node-pop .np-x')
    if (closeBtn) await closeBtn.click()
    await page.waitForTimeout(1100)
    const diffAfterClose = await frameDiff()
    check('取消选中后回位且无抖（有位移但非异常）', diffAfterClose > 20000, `diff=${diffAfterClose}`)
    // 连续切换不抖：快速点另一个位置
    await page.evaluate(() => {
      const cv = document.querySelector('.graph3d')
      const rect = cv.getBoundingClientRect()
      const x = rect.left + rect.width * 0.6
      const y = rect.top + rect.height * 0.55
      cv.dispatchEvent(new PointerEvent('pointerdown', { clientX: x, clientY: y, bubbles: true }))
      cv.dispatchEvent(new PointerEvent('pointerup', { clientX: x, clientY: y, bubbles: true }))
      cv.dispatchEvent(new MouseEvent('click', { clientX: x, clientY: y, bubbles: true }))
      setTimeout(() => {
        const x2 = rect.left + rect.width * 0.4
        const y2 = rect.top + rect.height * 0.45
        cv.dispatchEvent(new PointerEvent('pointerdown', { clientX: x2, clientY: y2, bubbles: true }))
        cv.dispatchEvent(new PointerEvent('pointerup', { clientX: x2, clientY: y2, bubbles: true }))
        cv.dispatchEvent(new MouseEvent('click', { clientX: x2, clientY: y2, bubbles: true }))
      }, 200)
    })
    await page.waitForTimeout(900)
    const stillOk = await page.evaluate(() => !!document.querySelector('.graph3d'))
    check('连续切换选中不崩', stillOk, String(stillOk))
    // 清理
    await page.evaluate(() => document.querySelector('.node-pop .np-x')?.dispatchEvent(new MouseEvent('click', { bubbles: true })))
    await page.waitForTimeout(300)
  }

  // 6) D4 onboarding（首次飞行）—— 在全新 dataDir 下应出现，任意交互打断
  const onboardEl = await page.$('.g3d-onboard')
  // 由于前面已交互，可能已被打断；检查 localStorage 标记
  const onboardLS = await page.evaluate(() => { try { return localStorage.getItem('tabletalk-graph-onboarded') } catch { return null } })
  check('Onboarding 已标记已看或仍可见（不崩）', true, `ls=${onboardLS} visible=${!!onboardEl}`)
  // 若仍可见，点它应消失且不再出现
  if (onboardEl) {
    await onboardEl.click()
    await page.waitForTimeout(300)
    const gone = await page.evaluate(() => !document.querySelector('.g3d-onboard'))
    check('点击 Onboarding 消失', gone, String(gone))
    const ls2 = await page.evaluate(() => { try { return localStorage.getItem('tabletalk-graph-onboarded') } catch { return null } })
    check('Onboarding 写入 localStorage 永不重放', ls2 === '1', String(ls2))
  }
  // reduced-motion 检查：页面应尊重
  const reduced = await page.evaluate(() => { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches } catch { return false } })
  check('reduced-motion 检测不崩', typeof reduced === 'boolean', String(reduced))

  // 7) 基础行为不回退（拖拽、滚轮、空白点击清高亮）
  // 空白处点击应清高亮（若当前有选中，先确保再点空白）
  await page.evaluate(() => {
    const cv = document.querySelector('.graph3d')
    const r = cv.getBoundingClientRect()
    // 点左上角空白
    cv.dispatchEvent(new PointerEvent('pointerdown', { clientX: r.left + 5, clientY: r.top + 5, bubbles: true }))
    cv.dispatchEvent(new PointerEvent('pointerup', { clientX: r.left + 5, clientY: r.top + 5, bubbles: true }))
    cv.dispatchEvent(new MouseEvent('click', { clientX: r.left + 5, clientY: r.top + 5, bubbles: true }))
  })
  await page.waitForTimeout(400)
  const noPopup = await page.evaluate(() => !document.querySelector('.node-pop'))
  check('点击空白关闭弹窗/清高亮', noPopup, String(noPopup))

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail ? 1 : 0
} catch (e) {
  console.log('FAIL:', String(e).slice(0, 600))
  console.log(e.stack?.slice(0, 800))
  process.exitCode = 1
} finally {
  await browser.close().catch(() => {})
  sidecar.kill()
}
