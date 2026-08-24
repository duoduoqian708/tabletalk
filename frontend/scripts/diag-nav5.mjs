import { chromium } from '@playwright/test'
const browser = await chromium.launch()
const page = await browser.newPage()
const errors = []
page.on('pageerror', e => errors.push('PAGEERROR: ' + String(e).slice(0, 400)))
page.on('console', m => { if (m.type() === 'error') errors.push('CONSOLE: ' + m.text().slice(0, 200)) })
await page.goto('http://127.0.0.1:8777/', { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
// 直接 JS 点击知识库按钮
const clicked = await page.evaluate(() => {
  const btn = [...document.querySelectorAll('nav button')].find(b => b.textContent.includes('知识库'))
  if (!btn) return false
  btn.click()
  return true
})
console.log('clicked:', clicked)
await page.waitForTimeout(2500)
const body = await page.evaluate(() => document.body.innerText.slice(0, 500))
console.log('AFTER:', body.replace(/\n+/g, ' | '))
console.log('errors:', errors.length ? errors.join('\n') : '(none)')
await browser.close()
