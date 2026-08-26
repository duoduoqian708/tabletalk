import { useEffect, useState } from 'react'
import { tags as fetchTags } from '@renderer/api/knowledge'
import type { TagInfo } from '@renderer/api/types'
import { getTagColor } from '@renderer/utils/tagColors'
import { useI18n } from '@renderer/store/i18n'

const MOCK_TAGS: TagInfo[] = [
  { name: '销售', description: '订单、营收、销售额相关表', status: 'confirmed' },
  { name: '退货', description: '退货与退款相关表', status: 'confirmed' },
  { name: '库存', description: '库存与周转相关表', status: 'confirmed' },
  { name: '客户', description: '客户信息与价值分析', status: 'confirmed' },
  { name: '渠道', description: '营销渠道与投放', status: 'confirmed' },
  { name: '财务', description: '财务与核算', status: 'confirmed' },
]

interface Props {
  connId: string | null
  onSelect?: (tag: string | null) => void
}

/** 工作台领域标签：只读展示已确认标签，点击过滤高亮匹配表。 */
export function TagBar({ connId, onSelect }: Props): React.JSX.Element {
  const { t } = useI18n()
  const [tags, setTags] = useState<TagInfo[]>([])
  const [selected, setSelected] = useState<string | null>(null)

  useEffect(() => {
    setTags([])
    setSelected(null)
    if (!connId) return
    let alive = true
    void fetchTags(connId)
      .then((lib) => {
        if (!alive) return
        const confirmed = (lib.library || []).filter((tag) => tag.status === 'confirmed')
        setTags(confirmed.length > 0 ? confirmed : MOCK_TAGS)
      })
      .catch(() => { if (alive) setTags(MOCK_TAGS) })
    return () => { alive = false }
  }, [connId])

  function toggleSelect(name: string): void {
    const next = selected === name ? null : name
    setSelected(next)
    onSelect?.(next)
  }

  return (
    <div className="ws-tags">
      {tags.map((tag) => (
        <button
          key={tag.name}
          type="button"
          className={`tag-chip${selected === tag.name ? ' selected' : ''}`}
          style={{ '--tag-c': getTagColor(tag.name) } as React.CSSProperties}
          title={tag.description || tag.name}
          onClick={() => toggleSelect(tag.name)}
        >
          {tag.name}
        </button>
      ))}
      {tags.length === 0 && <span className="ws-tags-none mono">{t('ws.noTags')}</span>}
    </div>
  )
}
