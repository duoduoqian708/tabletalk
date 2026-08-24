
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
const root = '/Users/mac/demo-project/tabletalk'
const backendDir = path.join(root, 'backend')
const dataDir = '/tmp/tabletalk-measure'
const baseUrl = 'http://127.0.0.1:8766'
fs.rmSync(dataDir, { recursive: true, force: true }); fs.mkdirSync(dataDir, { recursive: true })
const py = path.join(backendDir, '.venv/bin/python')
const sidecar = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--port', '8766'], { cwd: backendDir, env: { ...process.env, TABLETALK_DATA_DIR: dataDir, TABLETALK_WEB_DIST: path.join(root, 'frontend/dist') }, stdio: 'ignore' })
async function waitHealth(t=30000){const d=Date.now()+t;while(Date.now()<d){try{const r=await fetch(baseUrl+'/api/v1/health');if(r.ok)return}catch{}await new Promise(r=>setTimeout(r,400))}throw new Error('health timeout')}
const browser = await chromium.launch()
try {
  await waitHealth()
  const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('.appbar, .onboarding', { timeout: 40000 })
  const onboard = await page.$('.onboarding .primary'); if (onboard) await onboard.click()
  await page.waitForSelector('.sys-btn', { timeout: 25000 })
  const g1 = await page.evaluate(() => {
    const btn = document.querySelector('.sys-btn')
    const gear = btn.querySelector('.gear')
    const br = btn.getBoundingClientRect()
    const gr = gear.getBoundingClientRect()
    const gcs = getComputedStyle(gear)
    return {
      btn: { w: Math.round(br.width), h: Math.round(br.height), left: Math.round(br.left), top: Math.round(br.top) },
      gear: { w: Math.round(gr.width), h: Math.round(gr.height), left: Math.round(gr.left), top: Math.round(gr.top) },
      offsetX: Math.round(gr.left - br.left), offsetY: Math.round(gr.top - br.top),
      gearDisplay: gcs.display, gearLineHeight: gcs.lineHeight, gearFont: gcs.fontSize,
    }
  })
  await page.click('.sys-btn'); await page.waitForSelector('.set-x', { timeout: 8000 }); await page.waitForTimeout(400)
  const g2 = await page.evaluate(() => {
    const x = document.querySelector('.set-x')
    const xr = x.getBoundingClientRect()
    const cs = getComputedStyle(x)
    // 找 X 字符的文本节点位置：直接量按钮内第一个 text 的 range
    let charRect = null
    const walker = document.createTreeWalker(x, NodeFilter.SHOW_TEXT)
    const n = walker.nextNode()
    if (n) { const range = document.createRange(); range.selectNodeContents(n); charRect = range.getBoundingClientRect() }
    return {
      btn: { w: Math.round(xr.width), h: Math.round(xr.height) },
      char: charRect ? { w: Math.round(charRect.width), h: Math.round(charRect.height), left: Math.round(charRect.left), top: Math.round(charRect.top) } : null,
      offsetX: charRect ? Math.round(charRect.left - xr.left) : null,
      offsetY: charRect ? Math.round(charRect.top - xr.top) : null,
      fontSize: cs.fontSize, lineHeight: cs.lineHeight,
    }
  })
  console.log('GEAR=' + JSON.stringify(g1))
  console.log('X=' + JSON.stringify(g2))
} catch (e) { console.log('ERR:', e && e.stack ? e.stack : String(e)); process.exitCode=1 } finally { await browser.close(); sidecar.kill('SIGKILL') }
