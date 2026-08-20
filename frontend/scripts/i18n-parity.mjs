// scripts/i18n-parity.mjs
import { zhCN } from '../src/renderer/src/locales/zh-CN.ts'
import { enUS } from '../src/renderer/src/locales/en-US.ts'

function flatten(obj, prefix = '') {
  const out = new Set()
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object') for (const sub of flatten(v, key)) out.add(sub)
    else out.add(key)
  }
  return out
}

const zh = flatten(zhCN)
const en = flatten(enUS)
const missingInEn = [...zh].filter((k) => !en.has(k))
const missingInZh = [...en].filter((k) => !zh.has(k))
const emptyVals = [...zh].filter((k) => {
  const v = k.split('.').reduce((o, p) => (o ? o[p] : undefined), zhCN)
  return v === '' || v == null
})

let ok = true
if (missingInEn.length) { console.error('en-US 缺少 key:', missingInEn); ok = false }
if (missingInZh.length) { console.error('zh-CN 缺少 key:', missingInZh); ok = false }
if (emptyVals.length) { console.error('空 value:', emptyVals); ok = false }
if (ok) console.log(`i18n parity OK: ${zh.size} keys`)
process.exit(ok ? 0 : 1)
