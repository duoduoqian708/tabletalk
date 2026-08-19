import { chromium } from '@playwright/test'
import fs from 'node:fs'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
await page.goto('http://127.0.0.1:8765', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.appbar', { timeout: 40000 })
await page.waitForTimeout(2000)
await page.screenshot({ path: '/tmp/ui-check.png' })
await browser.close()
console.log('screenshot saved')
