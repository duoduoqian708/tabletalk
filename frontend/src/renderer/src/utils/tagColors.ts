/* ═══════════════════════════════════════════════
   标签确定性配色（审查页 / 2D 关系图同源）
   localStorage 自定义色（标签编辑浮层选色）优先，
   色板内置 20 色（黄金角均分色相，两两区分度高）；
   assignUniqueColors 保证"展示期不撞色"（≤20 个标签每色唯一，满则循环）
   ═══════════════════════════════════════════════ */

export const TAG_COLORS = [
  '#e25050', '#50e27a', '#a550e2', '#e2d050', '#50cae2',
  '#e2509f', '#74e250', '#5650e2', '#e28150', '#50e2ac',
  '#d650e2', '#c4e250', '#5099e2', '#e2506e', '#50e25c',
  '#8750e2', '#e2b250', '#50e2dd', '#e250bd', '#93e250',
]

const COLOR_MAP_KEY = 'tabletalk-tag-colors'

function hashTag(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return h
}

function loadColorMap(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(COLOR_MAP_KEY) || '{}')
  } catch {
    return {}
  }
}

function saveColorMap(m: Record<string, string>): void {
  try {
    localStorage.setItem(COLOR_MAP_KEY, JSON.stringify(m))
  } catch { /* ignore */ }
}

/** 标签 → 颜色：自定义色优先，否则哈希色板 */
export function getTagColor(name: string, custom?: Record<string, string>): string {
  const map = custom ?? loadColorMap()
  return map[name] ?? TAG_COLORS[Math.abs(hashTag(name)) % TAG_COLORS.length]
}

/** 展示期不撞色分配：保留已分配色（自定义/既有），只为缺色标签补第一个空闲色板色。
 *   ≤20 标签色色不同，>20 循环复用；同名永远同色（稳定）。 */
export function assignUniqueColors(names: string[], custom?: Record<string, string>): Record<string, string> {
  const map = { ...(custom ?? loadColorMap()) }
  const used = new Set<string>(names.map((n) => map[n]).filter(Boolean))
  let slot = 0
  let changed = false
  for (const name of names) {
    if (map[name]) continue
    changed = true
    let placed: string | null = null
    for (let tryN = 0; tryN < TAG_COLORS.length; tryN++) {
      const c = TAG_COLORS[slot % TAG_COLORS.length]
      slot++
      if (!used.has(c)) { placed = c; break }
    }
    const color = placed ?? TAG_COLORS[(slot - 1) % TAG_COLORS.length]
    map[name] = color
    used.add(color)
  }
  if (changed) saveColorMap(map)
  return map
}
