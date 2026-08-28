/** 时间展示统一工具：后端存储一律 UTC（ISO-8601 带偏移），展示转浏览器本地时区。
 *  老数据为本地朴素串（无偏移）→ JS 按本地时区解析，同样正确。 */

export type DtMode = 'full' | 'date' | 'time'

const pad = (n: number): string => String(n).padStart(2, '0')

/** 解析时间串为本地 Date；纯日期串（YYYY-MM-DD）不转时区避免跨日偏移 */
export function parseDt(ts?: string | null): Date | null {
  if (!ts) return null
  if (/^\d{4}-\d{2}-\d{2}$/.test(ts)) return null
  const d = new Date(ts)
  return Number.isNaN(d.getTime()) ? null : d
}

/** 统一格式化：full → YYYY-MM-DD HH:mm:ss；date → YYYY-MM-DD；time → HH:mm:ss */
export function fmtDT(ts?: string | null, mode: DtMode = 'full'): string {
  if (!ts) return '—'
  if (/^\d{4}-\d{2}-\d{2}$/.test(ts)) return ts
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return ts
  const Y = d.getFullYear()
  const M = pad(d.getMonth() + 1)
  const D = pad(d.getDate())
  const h = pad(d.getHours())
  const m = pad(d.getMinutes())
  const s = pad(d.getSeconds())
  if (mode === 'date') return `${Y}-${M}-${D}`
  if (mode === 'time') return `${h}:${m}:${s}`
  return `${Y}-${M}-${D} ${h}:${m}:${s}`
}