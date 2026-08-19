import { chromium } from '@playwright/test'
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1480, height: 940 } })
await page.goto('http://127.0.0.1:8765', { waitUntil: 'domcontentloaded' })
await page.waitForSelector('.g-node', { timeout: 40000 })
await page.waitForTimeout(1500)
const layout = await page.evaluate(() => {
  const nodes = [...document.querySelectorAll('.g-node')]
  const pos = nodes.map((n) => {
    const t = n.getAttribute('transform') || ''
    const m = t.match(/translate\(([-\d.]+),([-\d.]+)\)/)
    return m ? { x: parseFloat(m[1]), y: parseFloat(m[2]) } : { x: 0, y: 0 }
  })
  const xs = pos.map((p) => p.x), ys = pos.map((p) => p.y)
  const xSpan = Math.max(...xs) - Math.min(...xs)
  const ySpan = Math.max(...ys) - Math.min(...ys)
  // 重叠检测：任意两节点中心距
  let minDist = Infinity
  for (let i = 0; i < pos.length; i++) for (let j = i + 1; j < pos.length; j++) {
    const d = Math.hypot(pos[i].x - pos[j].x, pos[i].y - pos[j].y)
    if (d < minDist) minDist = d
  }
  return { count: pos.length, xSpan: Math.round(xSpan), ySpan: Math.round(ySpan), minDist: Math.round(minDist), aspect: (xSpan / Math.max(1, ySpan)).toFixed(2) }
})
console.log('节点数:', layout.count, '| X 跨度:', layout.xSpan, '| Y 跨度:', layout.ySpan, '| 最小间距:', layout.minDist, '| 宽高比:', layout.aspect)
console.log(layout.xSpan > 300 && layout.ySpan > 300 ? '→ 布局已散开（二维分布）✓' : '→ 仍是一列 ✗')
console.log(layout.minDist > 100 ? '→ 节点无重叠 ✓' : `→ 存在过近节点（${layout.minDist}px）`)
await page.screenshot({ path: '/tmp/ui-layout.png' })
await browser.close()
