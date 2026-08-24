import { useEffect, useMemo, useRef, useState } from 'react'
import { tags as fetchTags } from '@renderer/api/knowledge'
import type { TagInfo } from '@renderer/api/types'
import { useI18n } from '@renderer/store/i18n'

/** 标签彩色色板（与星图节点调色板同源；按展示顺序轮流取色，保证相邻标签不同色） */
const TAG_PALETTE = ['#4cc9f0', '#34f5c5', '#ffc46b', '#8b7cf8', '#ff8fb3', '#ffab70']

const DEFAULT_SHOWN = 3

/** 演示用 MOCK 标签：知识库暂无已确认标签时展示，便于预览标签区效果（真实标签确认后自动替换）。 */
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
}

/** 领域标签快捷区：默认展示 3 个已确认标签（不足则展示实际数量），+ 按钮可从全部标签中添加未展示项。 */
export function TagBar({ connId }: Props): React.JSX.Element {
  const { t } = useI18n()
  const [library, setLibrary] = useState<TagInfo[]>([])
  const [shown, setShown] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // 连接变化时加载已确认标签
  useEffect(() => {
    setLibrary([])
    setShown([])
    setOpen(false)
    if (!connId) return
    let alive = true
    void fetchTags(connId)
      .then((lib) => {
        if (!alive) return
        const confirmed = (lib.library || []).filter((tag) => tag.status === 'confirmed')
        // 空库（或加载失败）时用 MOCK 标签预览效果，真实标签确认后自动替换
        const effective = confirmed.length > 0 ? confirmed : MOCK_TAGS
        setLibrary(effective)
        setShown(effective.slice(0, DEFAULT_SHOWN).map((tag) => tag.name))
      })
      .catch(() => {
        if (!alive) return
        setLibrary(MOCK_TAGS)
        setShown(MOCK_TAGS.slice(0, DEFAULT_SHOWN).map((tag) => tag.name))
      })
    return () => { alive = false }
  }, [connId])

  useEffect(() => {
    const onDoc = (e: MouseEvent): void => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  const shownTags = useMemo(() => library.filter((tag) => shown.includes(tag.name)), [library, shown])
  const hiddenTags = useMemo(() => library.filter((tag) => !shown.includes(tag.name)), [library, shown])

  function add(name: string): void {
    setShown((prev) => (prev.includes(name) ? prev : [...prev, name]))
  }

  return (
    <div className="ws-tags" ref={ref}>
      {shownTags.map((tag) => (
        <button
          key={tag.name}
          type="button"
          className="tag-chip"
          style={{ '--tag-c': TAG_PALETTE[library.indexOf(tag) % TAG_PALETTE.length] } as React.CSSProperties}
          title={tag.description || tag.name}
        >
          {tag.name}
        </button>
      ))}
      {library.length === 0 && <span className="ws-tags-none mono">{t('ws.noTags')}</span>}
      {hiddenTags.length > 0 && (
        <div className="ws-tags-add">
          <button
            type="button"
            className={`tag-add${open ? ' open' : ''}`}
            onClick={() => setOpen((o) => !o)}
            title={t('ws.addTagTitle')}
          >
            + {t('ws.addTag')}
          </button>
          {open && (
            <div className="tag-menu">
              {hiddenTags.map((tag) => (
                <button
                  key={tag.name}
                  type="button"
                  className="tag-menu-item"
                  style={{ '--tag-c': TAG_PALETTE[library.indexOf(tag) % TAG_PALETTE.length] } as React.CSSProperties}
                  onClick={() => { add(tag.name); setOpen(false) }}
                >
                  <span className="tag-dot" />
                  {tag.name}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}