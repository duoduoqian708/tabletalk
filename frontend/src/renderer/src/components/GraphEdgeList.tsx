import { useMemo, useState } from 'react'
import type { GraphEdge } from '@renderer/api/types'
import { graphEdgeKey } from './TableRelationGraph2D'
import { useI18n } from '@renderer/store/i18n'

/** 2D 编辑页左侧：关系快速预览列表（本地搜索按表名过滤，点击定位高亮对应边）。 */
export function GraphEdgeList({ edges, activeKey, onPick }: {
  edges: GraphEdge[]
  activeKey: string | null
  onPick: (key: string) => void
}): React.JSX.Element {
  const { t } = useI18n()
  const [q, setQ] = useState('')
  const kw = q.trim().toLowerCase()
  const filtered = useMemo(() => {
    if (!kw) return edges
    return edges.filter((e) => e.from.toLowerCase().includes(kw) || e.to.toLowerCase().includes(kw))
  }, [edges, kw])
  return (
    <div className="ge-list">
      <div className="ge-list-h mono">
        {t('kb.graphEdgeList')} <span className="ge-list-cnt">({filtered.length})</span>
      </div>
      <input className="ge-search mono" placeholder={t('kb.graphEdgeSearch')} value={q}
        onChange={(e) => setQ(e.target.value)} />
      <div className="ge-list-body">
        {filtered.length === 0 && <div className="ge-empty mono">{t('kb.graphEdgeEmpty')}</div>}
        {filtered.map((e) => {
          const key = graphEdgeKey(e)
          const draft = e.status === 'draft'
          return (
            <div key={key} className={`ge-row${activeKey === key ? ' on' : ''}${draft ? ' is-draft' : ''}`}
              onClick={() => onPick(key)}>
              <span className="ge-row-edge mono">
                <b>{e.from}</b>.{e.from_col || '*'}
                <span className="ge-arrow"> → </span>
                <b>{e.to}</b>.{e.to_col || '*'}
              </span>
              <span className="ge-meta mono">
                {e.cardinality === '1:1' ? '1:1' : 'n:1'}
                {draft && <em className="ge-draft">· {t('kb.draft')}</em>}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}