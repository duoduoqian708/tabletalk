import { chromium } from '@playwright/test'

function lum(hex) {
  const c = hex.replace('#', '')
  const [r, g, b] = [0, 2, 4].map(i => parseInt(c.slice(i, i + 2), 16) / 255)
  const f = (v) => v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}
function contrast(a, b) {
  const [l1, l2] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (l1 + 0.05) / (l2 + 0.05)
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1480, height: 940 }, deviceScaleFactor: 2 })
await page.goto('http://127.0.0.1:8765', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.appbar', { timeout: 40000 })
await page.waitForTimeout(1200)

const probe = await page.evaluate(() => {
  const g = (sel, props) => {
    const el = document.querySelector(sel)
    if (!el) return null
    const s = getComputedStyle(el)
    const out = {}
    props.forEach(p => out[p] = s[p])
    return out
  }
  return {
    bodyColor: g('body', ['color'])?.color,
    bodyBg: g('body', ['backgroundColor'])?.backgroundColor,
    bodySize: g('body', ['fontSize'])?.fontSize,
    wordmark: g('.wordmark', ['fontSize', 'color', 'fontFamily']),
    tab: g('.appbar .tab', ['fontSize', 'color']),
    tabOn: g('.appbar .tab.on', ['color']),
    gw: g('.gw', ['fontSize', 'color']),
    compose: g('.compose-input', ['fontSize', 'color', 'backgroundColor']),
    wsHint: g('.ws-crumb, .ws-empty-hint', ['fontSize', 'color'])
  }
})
console.log('body color:', probe.bodyColor, '| bg:', probe.bodyBg, '| size:', probe.bodySize)
console.log('wordmark:', probe.wordmark)
console.log('tab:', probe.tab)
console.log('gw:', probe.gw)
console.log('compose:', probe.compose)

const rgbToHex = (rgb) => '#' + rgb.match(/\d+/g).slice(0, 3).map(n => (+n).toString(16).padStart(2, '0')).join('')
const fg = rgbToHex(probe.bodyColor), bg = rgbToHex(probe.bodyBg)
const ratio = contrast(fg, bg)
console.log(`正文对比度: ${ratio.toFixed(2)}:1 (${fg} on ${bg})`)

let pass = 0, fail = 0
const C = (name, cond, got) => { cond ? pass++ : fail++; console.log(`${cond ? 'PASS' : 'FAIL'} ${name} → ${got}`) }
C('正文对比度 ≥ 10:1', ratio >= 10, ratio.toFixed(2))
C('正文字号 ≥ 14px', parseFloat(probe.bodySize) >= 14, probe.bodySize)
C('顶栏 tab 字号 ≥ 12px', parseFloat(probe.tab.fontSize) >= 12, probe.tab.fontSize)
C('AI 输入框 ≥ 13px', parseFloat(probe.compose.fontSize) >= 13, probe.compose.fontSize)
C('品牌用展示字体', /Sora/i.test(probe.wordmark.fontFamily), probe.wordmark.fontFamily.split(',')[0])
C('AI 输入框文字亮', lum(rgbToHex(probe.compose.color)) > 0.7, probe.compose.color)

await page.screenshot({ path: '/tmp/ui-v2-workspace.png' })
await page.click('.nav .tab >> text=知识库')
await page.waitForSelector('.kb-cols', { timeout: 20000 })
await page.waitForTimeout(600)
const kb = await page.evaluate(() => {
  const g = (sel) => { const el = document.querySelector(sel); return el ? getComputedStyle(el).fontSize : null }
  return { tname: g('.rv-table-row .tname'), badge: g('.kb-badge'), btn: g('.iconbtn'), synced: g('.kb-synced') }
})
console.log('知识库页字号:', JSON.stringify(kb))
C('知识库表名 ≥ 13px', parseFloat(kb.tname) >= 13, kb.tname)
C('徽标 ≥ 11px', parseFloat(kb.badge) >= 11, kb.badge)
C('按钮 ≥ 12px', parseFloat(kb.btn) >= 12, kb.btn)
await page.screenshot({ path: '/tmp/ui-v2-kb.png' })

await page.click('.nav .tab >> text=工作台')
await page.waitForSelector('.compose-input', { timeout: 10000 })
await page.click('.compose-input')
await page.type('.compose-input', '退货率最高的 10 个产品')
await page.click('.airail .send')
await page.waitForSelector('.sql, .turn-text', { timeout: 60000 }).catch(() => {})
await page.waitForTimeout(2500)
await page.screenshot({ path: '/tmp/ui-v2-chat.png' })

console.log(`\n${pass} passed, ${fail} failed`)
await browser.close()
process.exit(fail ? 1 : 0)
