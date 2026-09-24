import { chromium } from '@playwright/test'
const browser = await chromium.launch()
const page = await browser.newPage()
const errors = []
page.on('pageerror', e => errors.push('PAGEERROR: ' + String(e).slice(0, 200)))
await page.goto('http://127.0.0.1:8777/', { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
const tabs = await page.locator('.nav .tab, .tab, nav button').allInnerTexts().catch(() => [])
console.log('NAV:', tabs.join(' | ').slice(0, 200))
// 尝试点知识库
try {
  await page.locator('text=知识库').first().click({ timeout: 5000 })
  await page.waitForTimeout(1500)
  console.log('knowledge view OK, errors:', errors.length ? errors.join(';') : 'none')
} catch (e) {
  console.log('click knowledge failed:', String(e).slice(0, 120))
}
await browser.close()
