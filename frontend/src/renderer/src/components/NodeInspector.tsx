import type { CSSProperties } from 'react'
import type { GraphNode } from '@renderer/store/graph'
import { useKnowledge } from '@renderer/store/knowledge'
import { useSchema } from '@renderer/store/schema'

interface Props {
  node: GraphNode
  style: CSSProperties
  onOpenData: () => void
  onAskAi: () => void
  onClose: () => void
}

/** 检查器：选中节点的字段/注释/标签，浮在节点旁。 */
export function NodeInspector({ node, style, onOpenData, onAskAi, onClose }: Props): React.JSX.Element {
  const schema = useSchema((s) => s.data)
  const overview = useKnowledge((s) => s.overview)
  const columns = (schema?.columns ?? []).filter((c) => c.table === node.name)
  const kv = overview?.tables.find((t) => t.name === node.name)
  const comment = kv?.comment
  const tags = kv?.tags ?? []

  return (
    <div className="g-inspector" style={style} onMouseDown={(e) => e.stopPropagation()}>
      <div className="gi-head">
        <span className="gi-dot" style={{ background: node.color }} />
        <span className="gi-name">{node.name} 检查器</span>
        <button className="gi-x" onClick={onClose} title="关闭">✕</button>
      </div>
      <div className="gi-fields mono">
        {columns.length > 0
          ? columns.map((c) => (
              <span key={c.name} className={`gi-fld${c.pk ? ' pk' : ''}`}>
                {c.name}
                <i>{c.type}</i>
              </span>
            ))
          : '（无字段信息）'}
      </div>
      {comment && <div className="gi-comment">"{comment}"</div>}
      {tags.length > 0 && (
        <div className="gi-tags">
          {tags.map((t) => (
            <span key={t.name} className={`gi-tag${t.status === 'draft' ? ' draft' : ''}`}>
              {t.name}
              {t.status === 'draft' && ' · 待确认'}
            </span>
          ))}
        </div>
      )}
      <div className="gi-actions">
        <button className="gi-btn pri" onClick={onOpenData}>打开数据 ⚡</button>
        <button className="gi-btn" onClick={onAskAi}>问 AI 这张表</button>
      </div>
    </div>
  )
}
