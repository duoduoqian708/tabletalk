import { getTagColor } from '@renderer/utils/tagColors'

/** 无标签表的基准灰 */
const BASE_UNTAGGED = '#5a6a7e'

/** 将多个十六进制颜色混合（RGB 通道取均值） */
function blendColors(hexes: string[]): string {
  if (hexes.length === 0) return BASE_UNTAGGED
  if (hexes.length === 1) return hexes[0]
  let r = 0, g = 0, b = 0
  for (const h of hexes) {
    const n = parseInt(h.replace('#', ''), 16)
    r += (n >> 16) & 0xff
    g += (n >> 8) & 0xff
    b += n & 0xff
  }
  const n = hexes.length
  return `#${((1 << 24) + ((r / n) << 16) + ((g / n) << 8) + (b / n) | 0).toString(16).slice(1)}`
}

/** 根据表的标签列表计算节点颜色：0 标签=基准灰，1 标签=该色，多标签=混色 */
export function tagColorForTable(
  table: { tags: { name: string }[] },
  colorMap?: Record<string, string>,
): string {
  const tags = table.tags ?? []
  if (tags.length === 0) return BASE_UNTAGGED
  const colors = tags.map((tg) => getTagColor(tg.name, colorMap))
  return blendColors(colors)
}
