/* ═══════════════════════════════════════════════
   标签确定性配色（审查页 / 2D 关系图同源）
   localStorage 自定义色（标签编辑浮层选色）优先，
   否则按标签名哈希取 8 色板——同名永远同色
   ═══════════════════════════════════════════════ */

export const TAG_COLORS = ['#35d99a', '#63c8ff', '#ffb454', '#b18cff', '#ff6b81', '#2ee6a8', '#f472b6', '#fbbf24']

const COLOR_MAP_KEY = 'tabletalk-tag-colors'

export function hashTag(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return h
}

export function loadColorMap(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(COLOR_MAP_KEY) || '{}')
  } catch {
    return {}
  }
}

export function saveColorMap(m: Record<string, string>): void {
  try {
    localStorage.setItem(COLOR_MAP_KEY, JSON.stringify(m))
  } catch { /* ignore */ }
}

/** 标签 → 颜色：自定义色优先，否则哈希色板 */
export function getTagColor(name: string, custom?: Record<string, string>): string {
  const map = custom ?? loadColorMap()
  return map[name] ?? TAG_COLORS[Math.abs(hashTag(name)) % TAG_COLORS.length]
}
