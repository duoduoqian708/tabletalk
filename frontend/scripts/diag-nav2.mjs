import { chromium } from '@playwright/test'
const browser = await chromium.launch()
const page = await browser.newPage()
const errors = []
page.on('pageerror', e => errors.push('PAGEERROR: ' + String(e).slice(0, 200)))
await page.goto('http://127.0.0.1:8777/', { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
// 用 tab 位置点击：第2个 tab = 知识库
const tabs = page.locator('.tab')
console.log('tab count:', await tabs.count())
await tabs.nth(1).click({ timeout: 5000 })
await page.waitForTimeout(2000)
const body = (await page.locator('body').innerText()).slice(0, 300)
console.log('AFTER CLICK:', body.replace(/\n+/g, ' | ').slice(0, 300))
console.log('errors:', errors.length ? errors.join(';') : 'none')
await browser.close()
