import { useState, type CSSProperties } from 'react'
import type { GraphNode } from '@renderer/store/graph'
import { useGraph } from '@renderer/store/graph'
import { useKnowledge } from '@renderer/store/knowledge'
import { useConnections } from '@renderer/store/connections'
import { useSchema } from '@renderer/store/schema'
import { toastMsg } from '@renderer/utils/toast'

interface Props {
  node: GraphNode
  style: CSSProperties
  onOpenData: () => void
  onAskAi: () => void
  onClose: () => void
}

/** 检查器：选中节点的字段/注释/标签；治理模式下可编辑、确认、驳回。 */
export function NodeInspector({ node, style, onOpenData, onAskAi, onClose }: Props): React.JSX.Element {
  const schema = useSchema((s) => s.data)
  const overview = useKnowledge((s) => s.overview)
  const mode = useGraph((s) => s.mode)
  const currentId = useConnections((s) => s.currentId)
  const { confirmComment, rejectComment, confirmTag, rejectTag, assignTags, saveNote } = useKnowledge()
  const [note, setNote] = useState('')
  const [tagInput, setTagInput] = useState('')
  const [busy, setBusy] = useState(false)

  const columns = (schema?.columns ?? []).filter((c) => c.table === node.name)
  const kv = overview?.tables.find((t) => t.name === node.name)
  const comment = kv?.comment
  const commentStatus = kv?.comment_status ?? ''
  const tags = kv?.tags ?? []
  const govern = mode === 'govern'

  // 治理编辑预填当前注释（节点切换时重置）
  const [prefillKey, setPrefillKey] = useState(node.id)
  if (prefillKey !== node.id) {
    setPrefillKey(node.id)
    setNote(kv?.comment ?? '')
  }

  async function act(fn: () => Promise<void>): Promise<void> {
    if (!currentId) return
    setBusy(true)
    try {
      await fn()
    } catch (e) {
      toastMsg(`操作失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  async function save(): Promise<void> {
    const n = note.trim()
    if (!n || !currentId) return
    await act(() => saveNote(currentId, node.name, n))
    toastMsg('注释已保存')
  }

  async function assign(): Promise<void> {
    const t = tagInput.trim()
    if (!t || !currentId) return
    await act(() => assignTags(currentId, node.name, [t]))
    setTagInput('')
    toastMsg(`标签已分配：${t}`)
  }

  return (
    <div className="g-inspector" style={style} onMouseDown={(e) => e.stopPropagation()}>
      <div className="gi-head">
        <span className="gi-dot" style={{ background: node.color }} />
        <span className="gi-name">{node.name} 检查器</span>
        {govern && <span className="gi-gov">治理</span>}
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

      {comment && (
        <div className={`gi-comment${commentStatus === 'draft' ? ' draft' : ''}`}>
          "{comment}"
          {commentStatus === 'draft' && <span className="gi-status">待确认</span>}
        </div>
      )}

      {govern && (
        <div className="gi-note-edit">
          <textarea
            className="gi-note-ta"
            rows={2}
            placeholder="给这张表写注释…（如：订单主表，customer_id → customers.id）"
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
          <button
            className="gi-btn pri"
            disabled={busy || !note.trim() || note.trim() === (comment ?? '')}
            onClick={() => void save()}
          >
            {comment ? '更新注释' : '保存注释'}
          </button>
        </div>
      )}

      {govern && commentStatus === 'draft' && (
        <div className="gi-act-row">
          <button className="gi-btn ok" disabled={busy} onClick={() => void act(() => confirmComment(currentId!, node.name))}>✓ 确认注释</button>
          <button className="gi-btn no" disabled={busy} onClick={() => void act(() => rejectComment(currentId!, node.name))}>✕ 驳回</button>
        </div>
      )}

      {tags.length > 0 && (
        <div className="gi-tags">
          {tags.map((t) => (
            <span key={t.name} className={`gi-tag${t.status === 'draft' ? ' draft' : ''}`}>
              {t.name}
              {t.status === 'draft' && ' · 待确认'}
              {govern && t.status === 'draft' && (
                <button
                  className="gi-tag-act ok"
                  title="确认标签"
                  disabled={busy}
                  onClick={() => void act(() => confirmTag(currentId!, t.name))}
                >✓</button>
              )}
              {govern && t.status === 'draft' && (
                <button
                  className="gi-tag-act no"
                  title="驳回标签"
                  disabled={busy}
                  onClick={() => void act(() => rejectTag(currentId!, t.name))}
                >✕</button>
              )}
            </span>
          ))}
        </div>
      )}

      {govern && (
        <div className="gi-tag-add">
          <input
            className="gi-tag-input"
            value={tagInput}
            placeholder="添加标签…（如：交易域）"
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void assign() }}
          />
          <button className="gi-btn" disabled={busy || !tagInput.trim()} onClick={() => void assign()}>添加</button>
        </div>
      )}

      <div className="gi-actions">
        <button className="gi-btn pri" onClick={onOpenData}>打开数据 ⚡</button>
        <button className="gi-btn" onClick={onAskAi}>问 AI 这张表</button>
      </div>
    </div>
  )
}
