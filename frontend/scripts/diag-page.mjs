import { chromium } from '@playwright/test'

const browser = await chromium.launch()
const page = await browser.newPage()
const errors = []
page.on('console', m => { if (m.type() === 'error') errors.push(m.text().slice(0, 300)) })
page.on('pageerror', e => errors.push('PAGEERROR: ' + String(e).slice(0, 300)))

await page.goto('http://127.0.0.1:8777/', { waitUntil: 'networkidle' })
await page.waitForTimeout(3000)

console.log('URL:', page.url())
console.log('TITLE:', await page.title())
console.log('ROOT HTML len:', (await page.content()).length)
const body = await page.locator('body').innerText().catch(() => '(no body text)')
console.log('BODY TEXT (first 400):', body.slice(0, 400))
console.log('--- console/page errors ---')
console.log(errors.length ? errors.join('\n') : '(none)')
await browser.close()
