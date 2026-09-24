import { chromium } from '@playwright/test'
const browser = await chromium.launch()
const page = await browser.newPage()
const errors = []
page.on('pageerror', e => errors.push('PAGEERROR: ' + String(e).slice(0, 200)))
await page.goto('http://127.0.0.1:8777/', { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
await page.locator('nav .tab', { hasText: '知识库' }).click({ timeout: 5000 })
await page.waitForTimeout(2500)
const body = (await page.locator('body').innerText()).replace(/\n+/g, ' | ').slice(0, 400)
console.log('AFTER CLICK:', body)
console.log('errors:', errors.length ? errors.join(';') : 'none')
await browser.close()
