/**
 * 图上节点字号可调档位（1..5），localStorage 记忆。
 * 第 1 档 = 当前 3D 星图上的实际字号（13.5px，含常驻增量），此后每档 +1.5px 缓步上升。
 * 档位即真实渲染字号：3D（canvas）每帧重读，2D（SVG）经 CSS 变量 --graph-node-font 实时生效，均不随画布缩放。
 */
export const GRAPH_FONT_LEVELS = [13.5, 15, 16.5, 18, 19.5] as const
const GRAPH_FONT_DEFAULT = 1 // 默认第 1 档 = 当前字号，改动前页面无感知

const KEY = 'tabletalk-graph-font'
const CSS_VAR = '--graph-node-font'

export function getGraphFontLevel(): number {
  try {
    const v = Number(localStorage.getItem(KEY))
    if (Number.isFinite(v) && v >= 1 && v <= GRAPH_FONT_LEVELS.length) return Math.round(v)
  } catch { /* ignore */ }
  return GRAPH_FONT_DEFAULT
}

export function setGraphFontLevel(level: number): void {
  try { localStorage.setItem(KEY, String(level)) } catch { /* ignore */ }
  applyGraphFontDom(level)
}

/** 由档位取基准字号（px） */
export function graphFontBasis(level = getGraphFontLevel()): number {
  return GRAPH_FONT_LEVELS[Math.max(0, Math.min(GRAPH_FONT_LEVELS.length - 1, level - 1))]
}

/** 把当前档位写进 CSS 变量：2D 图 SVG 节点名经 var(--graph-node-font) 实时重绘 */
function applyGraphFontDom(level = getGraphFontLevel()): void {
  try { document.documentElement.style.setProperty(CSS_VAR, `${graphFontBasis(level)}px`) } catch { /* ignore */ }
}

applyGraphFontDom()
