import { chromium } from '@playwright/test'
const browser = await chromium.launch()
const page = await browser.newPage()
await page.goto('http://127.0.0.1:8777/', { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)
// 找出导航相关元素
const navHtml = await page.evaluate(() => {
  const el = document.querySelector('.nav') || document.querySelector('nav') || document.querySelector('header')
  return el ? el.outerHTML.slice(0, 1200) : '(no nav found)'
})
console.log(navHtml)
await browser.close()
