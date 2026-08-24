import { useEffect, useMemo, useState } from 'react'
import * as kbApi from '@renderer/api/knowledge'
import type { ReviewTable } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKbGate } from '@renderer/store/kbgate'
import { useKnowledge } from '@renderer/store/knowledge'
import { toastMsg } from '@renderer/utils/toast'

/* ═══════════════════════════════════════════════
   知识库审核弹窗（三栏审核 + 底部确认大按钮）
   不再由 kb_status===pending_review 自动弹出，
   经 kbgate.reviewOpen 受控开启（浮卡「去审查」/常驻胶囊）；
   确认提交成功自动关闭 → 进入知识库主页（左右布局）
   ═══════════════════════════════════════════════ */

/* ── 8 色标签色板 ── */
const TAG_COLORS = ['#35d99a', '#63c8ff', '#ffb454', '#b18cff', '#ff6b81', '#2ee6a8', '#f472b6', '#fbbf24']
const GRAY = '#5a6a7e'

function bandColor(hex: string): string {
  const n = parseInt(hex.slice(1), 16)
  return `rgb(${Math.round(((n >> 16) & 255) * 0.62)}, ${Math.round(((n >> 8) & 255) * 0.62)}, ${Math.round((n & 255) * 0.62)})`
}
function tintBg(hex: string): string {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, 0.14)`
}
function loadColorMap(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem('tabletalk-tag-colors') || '{}')
  } catch {
    return {}
  }
}
function saveColorMap(m: Record<string, string>): void {
  try {
    localStorage.setItem('tabletalk-tag-colors', JSON.stringify(m))
  } catch { /* ignore */ }
}

export function KbReviewModal(): React.JSX.Element | null {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { overview, loading, busy, load, confirmComment, rejectComment,
    confirmTag, rejectTag, saveEnum, saveNote, confirmAll, confirmGraphDraft, rejectGraphDraft } = useKnowledge()

  /* ── UI state ── */
  const [selTag, setSelTag] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [openCard, setOpenCard] = useState<string | null>(null)
  const [relSrc, setRelSrc] = useState<'all' | 'fk' | 'llm' | 'user'>('all')
  const [relSt, setRelSt] = useState<'all' | 'draft' | 'confirmed'>('all')
  const [noteEditing, setNoteEditing] = useState<string | null>(null)
  const [noteText, setNoteText] = useState('')
  const [creating, setCreating] = useState(false)
  const [newTagName, setNewTagName] = useState('')
  const [editingTag, setEditingTag] = useState<string | null>(null)
  const [editAnchor, setEditAnchor] = useState<{ x: number; y: number }>({ x: 0, y: 0 })
  const [editName, setEditName] = useState('')
  const [editDesc, setEditDesc] = useState('')
  const [editColor, setEditColor] = useState<string | null>(null)
  const [delArm, setDelArm] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [showFinalConfirm, setShowFinalConfirm] = useState(false)
  const [colorMap, setColorMap] = useState<Record<string, string>>(() => loadColorMap())
  useEffect(() => { saveColorMap(colorMap) }, [colorMap])

  useEffect(() => { if (currentId) void load(currentId) }, [currentId, load])

  const tagColor = (name: string): string =>
    colorMap[name] ?? TAG_COLORS[Math.abs(hashStr(name)) % TAG_COLORS.length]

  const reviewOpen = useKbGate((s) => s.reviewOpen)
  const closeReview = useKbGate((s) => s.closeReview)

  const tagRows = (overview?.tags.library ?? []).map(tg => ({
    ...tg,
    color: tagColor(tg.name),
    count: (overview?.tags.tables[tg.name] ?? []).length,
  }))

  const untaggedCount = (() => {
    const tagged = new Set<string>()
    for (const names of Object.values(overview?.tags.tables ?? {})) for (const n of names) tagged.add(n)
    return (overview?.tables ?? []).filter(tb => !tagged.has(tb.name)).length
  })()

  const filteredTables = useMemo(() => {
    if (!overview) return []
    let ts: ReviewTable[] = overview.tables
    if (selTag) {
      const members = new Set(overview.tags.tables[selTag] ?? [])
      ts = ts.filter(tb => members.has(tb.name))
    }
    if (search.trim()) {
      const q = search.toLowerCase()
      ts = ts.filter(tb => tb.name.toLowerCase().includes(q) || (tb.comment || '').toLowerCase().includes(q))
    }
    return ts
  }, [overview, selTag, search])

  const allRelations = useMemo(() => {
    if (!overview) return []
    const edges = (overview.graph.edges ?? []).map(e => ({
      key: `${e.from}>${e.to}>${e.kind}`,
      from: e.from, fcol: e.from_col ?? '', to: e.to, tcol: e.to_col ?? '',
      src: (e.kind === 'fk' ? 'fk' : e.kind === 'user' ? 'user' : 'llm') as 'fk' | 'llm' | 'user',
      status: 'confirmed' as const,
      reason: e.kind === 'overlap' ? '值分布重叠推断' : '',
    }))
    const drafts = (overview.graph.llm_draft_edges ?? []).map(e => ({
      key: `d-${e.from_table}>${e.to_table}`,
      from: e.from_table, fcol: e.from_col ?? '', to: e.to_table, tcol: e.to_col ?? '',
      src: 'llm' as const,
      status: e.status === 'previously_rejected' ? ('rejected' as const) : ('draft' as const),
      reason: e.reason ?? '',
    }))
    return [...edges, ...drafts]
  }, [overview])

  const filteredRels = useMemo(() => allRelations.filter(r =>
    (relSrc === 'all' || r.src === relSrc) &&
    (relSt === 'all' || r.status === relSt)), [allRelations, relSrc, relSt])

  const totalPending = (overview?.draft_count ?? 0) + (overview?.tag_draft_count ?? 0)
    + (overview?.enum_draft_count ?? 0) + (overview?.graph?.llm_draft_edges?.length ?? 0)

  /* ── 动作 ── */
  async function doCreateTag(): Promise<void> {
    if (!currentId || !newTagName.trim()) return
    try {
      await kbApi.createTag(currentId, newTagName.trim())
      setNewTagName('')
      setCreating(false)
      await load(currentId)
    } catch (e) {
      toastMsg(`创建失败：${(e as Error).message}`)
    }
  }

  function openEditor(name: string, el: HTMLElement): void {
    const rect = el.getBoundingClientRect()
    const W = 256
    let x = rect.right + 8
    if (x + W > window.innerWidth - 10) x = Math.max(10, rect.left - W - 8)
    const y = Math.min(Math.max(10, rect.top - 6), window.innerHeight - 330)
    setEditAnchor({ x, y })
    const tg = overview?.tags.library.find(v => v.name === name)
    setEditName(name)
    setEditDesc(tg?.description ?? '')
    setEditColor(tagColor(name))
    setDelArm(false)
    setEditingTag(name)
  }

  async function saveTagEdit(): Promise<void> {
    if (!currentId || !editingTag) return
    const nn = editName.trim()
    if (!nn) { toastMsg('标签名不能为空'); return }
    const oldColor = tagColor(editingTag)
    const needApi = nn !== editingTag || editDesc.trim() !== (overview?.tags.library.find(v => v.name === editingTag)?.description ?? '')
    try {
      if (needApi) await kbApi.updateTag(currentId, editingTag, { newName: nn !== editingTag ? nn : undefined, description: editDesc.trim() })
      if (editColor !== oldColor) setColorMap(m => ({ ...m, [nn]: editColor! }))
      if (selTag === editingTag) setSelTag(nn)
      setEditingTag(null)
      if (needApi) await load(currentId)
    } catch (e) {
      toastMsg(`更新失败：${(e as Error).message}`)
    }
  }

  async function doDeleteTag(): Promise<void> {
    if (!currentId || !editingTag) return
    if (!delArm) { setDelArm(true); return }
    try {
      await rejectTag(currentId, editingTag)
      setSelTag(null)
      setEditingTag(null)
    } catch (e) {
      toastMsg(`删除失败：${(e as Error).message}`)
    }
  }

  async function doConfirmAll(): Promise<void> {
    if (!currentId) return
    setConfirming(true)
    try {
      await confirmAll(currentId)
      toastMsg('知识库已确认并启用')
      closeReview()
    } finally {
      setConfirming(false)
    }
  }

  const primaryBand = (tbl: ReviewTable): string => {
    for (const tg of tbl.tags) {
      if (overview?.tags.library.some(v => v.name === tg.name)) return bandColor(tagColor(tg.name))
    }
    return GRAY
  }

  /* 受控开关：reviewOpen 未开启（或数据未就绪）时不渲染 —— 必须在所有 hooks 之后返回 */
  if (!currentId || !reviewOpen || !overview) return null

  return (
    <div className="krm-mask">
      <div className="krm-window">
        {/* ═══ 弹窗头 ═══ */}
        <div className="krm-head">
          <span className="krm-title">知识库审阅</span>
          <span className="krm-conn">{connName}</span>
          <span className="krm-pending">{totalPending} 项待确认</span>
          <span className="krm-spacer" />
          <span className="krm-hint">确认后知识库将正式启用</span>
          <button className="krm-close" onClick={closeReview} title="稍后再审">✕</button>
        </div>

        {/* ═══ 三栏主体 ═══ */}
        <div className="krm-body">
          {/* 左：标签库 */}
          <aside className="krm-col krm-tags">
            <div className="krm-col-cap">标签库</div>
            {creating ? (
              <div className="krm-create-row">
                <input autoFocus value={newTagName} onChange={e => setNewTagName(e.target.value)}
                  placeholder="标签名称…" maxLength={16}
                  onKeyDown={e => { if (e.key === 'Enter') void doCreateTag(); if (e.key === 'Escape') setCreating(false) }} />
                <button onClick={() => void doCreateTag()}>✓</button>
                <button onClick={() => { setCreating(false); setNewTagName('') }}>✕</button>
              </div>
            ) : (
              <button className="krm-newtag" onClick={() => setCreating(true)}>＋ 新建标签</button>
            )}
            <div className="krm-taglist">
              {tagRows.map(tg => {
                const on = selTag === tg.name
                return (
                  <div key={tg.name} className="krm-tag" style={{ background: on ? 'var(--accent-dim)' : undefined, borderLeftColor: on ? tg.color : 'transparent' }}
                    onClick={() => setSelTag(on ? null : tg.name)}>
                    <span style={{ width: 9, height: 9, borderRadius: '50%', background: tg.color, flexShrink: 0 }} />
                    <span className="krm-tag-name" style={{ color: on ? 'var(--accent-soft)' : tg.status === 'draft' ? 'var(--amber)' : 'var(--ink)' }}>{tg.name}</span>
                    {tg.status === 'draft' && (
                      <button className="krm-quick-ok" onClick={e => { e.stopPropagation(); void confirmTag(currentId, tg.name) }}>✓</button>
                    )}
                    <span className="krm-tag-cnt">{tg.count}</span>
                    <button className="krm-tag-more" onClick={e => { e.stopPropagation(); openEditor(tg.name, e.currentTarget.parentElement as HTMLElement) }}>⋯</button>
                  </div>
                )
              })}
              <div className="krm-divider" />
              <div className="krm-tag" onClick={() => setSelTag(selTag === '__untagged__' ? null : '__untagged__')}
                style={{ background: selTag === '__untagged__' ? 'var(--void-3)' : undefined }}>
                <span style={{ width: 9, height: 9, borderRadius: '50%', border: '1.5px dashed var(--ink-faint)', flexShrink: 0 }} />
                <span className="krm-tag-name" style={{ color: 'var(--ink-dim)' }}>未分类</span>
                <span className="krm-tag-cnt">{untaggedCount}</span>
              </div>
            </div>
          </aside>

          {/* 中：表卡片 */}
          <section className="krm-col krm-cards">
            <div className="krm-card-head">
              <div className="krm-search">
                <input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索表名 / 描述…" />
              </div>
              {selTag && (
                <button className="krm-chip" onClick={() => setSelTag(null)}>{selTag} ✕</button>
              )}
              <span style={{ flex: 1 }} />
              <span className="krm-count">{filteredTables.length} 张表</span>
            </div>
            <div className="krm-cardlist">
              {filteredTables.length === 0 && <div className="krm-empty">没有匹配的表</div>}
              {filteredTables.map(tbl => {
                const open = openCard === tbl.name
                const draftCols = overview.columns.filter(c => c.table === tbl.name && c.status === 'draft').length
                const tableEnums = (overview.enums ?? []).filter(e => e.table === tbl.name)
                return (
                  <div key={tbl.name} className="krm-card" style={{ borderLeft: `3px solid ${primaryBand(tbl)}`, borderColor: open ? 'rgba(46,230,168,0.25)' : undefined }}>
                    <div className="krm-card-head-row" onClick={() => setOpenCard(open ? null : tbl.name)}>
                      <span className="krm-chev" style={{ transform: open ? 'rotate(90deg)' : 'rotate(0)', color: open ? 'var(--accent)' : undefined }}>▶</span>
                      <span className="krm-cname">{tbl.name}</span>
                      <span className="krm-cdesc" title={tbl.comment}>{tbl.comment || '暂无描述'}</span>
                      {tbl.tags.map(tg => {
                        const c = tagColor(tg.name)
                        return <span key={tg.name} className="krm-pill" style={{ color: c, background: tintBg(c) }}>{tg.name}</span>
                      })}
                      {draftCols > 0
                        ? <span className="krm-badge warn">{draftCols} 字段待确认</span>
                        : <span className="krm-badge ok">✓ 已确认</span>}
                    </div>
                    {open && (
                      <div className="krm-card-body">
                        <div>
                          <div className="krm-sec">列属性</div>
                          <div className="krm-cols">
                            {overview.columns.filter(c => c.table === tbl.name).map(col => (
                              <div key={col.name} className="krm-col-row" style={{ opacity: col.status === 'rejected' ? 0.45 : 1 }}>
                                <span className="krm-cn">{col.name}</span>
                                <span className="krm-ct">{col.type}</span>
                                <span style={{ color: col.pk ? 'var(--amber)' : 'transparent', fontWeight: 700, fontSize: 9 }}>{col.pk ? 'PK' : '·'}</span>
                                <span style={{ color: col.fk ? 'var(--accent)' : 'transparent', fontWeight: 700, fontSize: 9 }}>{col.fk ? 'FK' : '·'}</span>
                                <span className="krm-cc" style={{ color: col.status === 'draft' ? 'var(--amber)' : col.status === 'rejected' ? 'var(--ink-faint)' : 'var(--ink-dim)' }}>{col.comment || '—'}</span>
                                <span style={{ display: 'flex', gap: 3 }}>
                                  {col.status !== 'confirmed' && (
                                    <button className="krm-mini ok" onClick={() => void confirmComment(currentId, col.table, col.name)}>✓</button>
                                  )}
                                  {col.status !== 'rejected' && col.status !== 'none' && (
                                    <button className="krm-mini no" onClick={() => void rejectComment(currentId, col.table, col.name)}>✕</button>
                                  )}
                                </span>
                              </div>
                            ))}
                          </div>
                        </div>
                        {tableEnums.length > 0 && (
                          <div>
                            <div className="krm-sec">枚举字典</div>
                            {tableEnums.map(en => (
                              <div key={en.column} className="krm-enum">
                                <div className="krm-enum-head">{tbl.name}.{en.column}</div>
                                {en.entries.map(entry => (
                                  <EnumRow key={entry.value} connId={currentId} table={tbl.name} column={en.column}
                                    entry={entry} onSave={(m) => void saveEnum(currentId, tbl.name, en.column, entry.value, m)} />
                                ))}
                              </div>
                            ))}
                          </div>
                        )}
                        <div>
                          <div className="krm-sec">用户备注</div>
                          {noteEditing === tbl.name ? (
                            <div>
                              <textarea autoFocus value={noteText} rows={3} onChange={e => setNoteText(e.target.value)} className="krm-note-ta" />
                              <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end', marginTop: 6 }}>
                                <button className="krm-btn" onClick={() => setNoteEditing(null)}>取消</button>
                                <button className="krm-btn primary" onClick={() => { void saveNote(currentId, tbl.name, noteText); setNoteEditing(null) }}>保存备注</button>
                              </div>
                            </div>
                          ) : (
                            <div className="krm-note" onClick={() => { setNoteEditing(tbl.name); setNoteText(tbl.comment || '') }}>
                              {tbl.comment || <span style={{ fontStyle: 'italic' }}>点击添加备注…</span>}
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </section>

          {/* 右：关系 */}
          <aside className="krm-col krm-rels">
            <div className="krm-col-cap">关系 · {filteredRels.length}</div>
            <div className="krm-rel-filters">
              {(['all', 'fk', 'llm', 'user'] as const).map(s => (
                <button key={s} onClick={() => setRelSrc(s)} className={`krm-fchip${relSrc === s ? ' on' : ''}`}>
                  {s === 'all' ? '全部' : s === 'fk' ? 'FK' : s === 'llm' ? 'AI 发现' : '用户添加'}
                </button>
              ))}
              <span style={{ width: '100%', height: 3 }} />
              {(['all', 'draft', 'confirmed'] as const).map(s => (
                <button key={s} onClick={() => setRelSt(s)} className={`krm-fchip${relSt === s ? ' on' : ''}`}>
                  {s === 'all' ? '全部状态' : s === 'draft' ? '待确认' : '已确认'}
                </button>
              ))}
            </div>
            <div className="krm-rellist">
              {filteredRels.length === 0 && <div className="krm-empty">没有匹配的关系</div>}
              {filteredRels.map(r => {
                const isDraft = r.status === 'draft'
                const srcStyle = r.src === 'fk' ? 'fk' : r.src === 'llm' ? 'llm' : 'user'
                return (
                  <div key={r.key} className={`krm-rel${isDraft ? ' draft' : ''}`} style={{ opacity: r.status === 'rejected' ? 0.42 : 1 }}>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 4, flexWrap: 'wrap' }}>
                      <span className="krm-rname">{r.from}</span>
                      {r.fcol && <span className="krm-rcol">.{r.fcol}</span>}
                      <span className="krm-rarrow">→</span>
                      <span className="krm-rname">{r.to}</span>
                      {r.tcol && <span className="krm-rcol">.{r.tcol}</span>}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 5 }}>
                      <span className={`krm-rsrc ${srcStyle}`}>{srcStyle === 'fk' ? 'FK' : srcStyle === 'llm' ? 'AI 发现' : '用户添加'}</span>
                      {r.reason && <span className="krm-rreason" title={r.reason}>{r.reason}</span>}
                      {isDraft ? (
                        <span style={{ display: 'flex', gap: 3, marginLeft: 'auto' }}>
                          <button className="krm-mini ok" onClick={() => void confirmGraphDraft(currentId, r.from)}>✓</button>
                          <button className="krm-mini no" onClick={() => void rejectGraphDraft(currentId, r.from)}>✕</button>
                        </span>
                      ) : r.status === 'confirmed' ? (
                        <span style={{ marginLeft: 'auto', fontSize: 10, color: 'var(--green)' }}>✓</span>
                      ) : null}
                    </div>
                  </div>
                )
              })}
            </div>
          </aside>
        </div>

        {/* ═══ 底部：明显确认大按钮 ═══ */}
        <div className="krm-foot">
          <div className="krm-foot-info">
            <span className="krm-foot-dot" />
            审阅后点击确认，知识库将正式启用
          </div>
          <span className="krm-spacer" />
          <button className="krm-btn" onClick={() => load(currentId)} disabled={busy || loading}>刷新</button>
          <button className="krm-confirm" disabled={confirming} onClick={() => setShowFinalConfirm(true)}>
            {confirming ? '确认中…' : `✓ 确认并启用知识库（${totalPending}）`}
          </button>
        </div>
      </div>

      {/* 最终确认弹窗：确认后不可逆，清空所有待确认项 */}
      {showFinalConfirm && (
        <div className="krm-mask" style={{ background: 'rgba(5,7,11,0.6)', padding: 0, zIndex: 1400 }}>
          <div className="krm-final">
            <div className="krm-final-title">确认启用知识库？</div>
            <div className="krm-final-desc">
              将把所有待确认项（注释 / 标签 / 枚举 / 关系）一次性确认，知识库正式启用并参与 AI 路由。此操作不可撤销。
            </div>
            <div className="krm-final-acts">
              <button className="krm-btn" onClick={() => setShowFinalConfirm(false)}>再想想</button>
              <button className="krm-confirm" disabled={confirming} onClick={() => { setShowFinalConfirm(false); void doConfirmAll() }}>
                {confirming ? '确认中…' : '✓ 确认并启用'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 标签编辑浮层 */}
      {editingTag && (
        <>
          <div style={{ position: 'fixed', inset: 0, zIndex: 1290 }} onClick={() => setEditingTag(null)} />
          <div className="krm-editor" style={{ left: editAnchor.x, top: editAnchor.y }} onClick={e => e.stopPropagation()}>
            <div className="krm-editor-head">编辑标签
              <button onClick={() => setEditingTag(null)}>✕</button>
            </div>
            <div className="krm-editor-body">
              <label>名称</label>
              <input value={editName} onChange={e => setEditName(e.target.value)} maxLength={16} />
              <label>描述</label>
              <textarea value={editDesc} rows={2} onChange={e => setEditDesc(e.target.value)} />
              <label>颜色</label>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {TAG_COLORS.map(c => (
                  <div key={c} onClick={() => setEditColor(c)} style={{
                    width: 22, height: 22, borderRadius: '50%', background: c, cursor: 'pointer',
                    border: editColor === c ? '2px solid var(--ink-strong)' : '2px solid transparent',
                  }} />
                ))}
              </div>
              <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
                <button className="krm-del" style={delArm ? { background: 'var(--red)', color: '#fff', borderColor: 'var(--red)' } : undefined}
                  onClick={() => void doDeleteTag()}>
                  {delArm ? '再点一次确认删除' : '删除标签'}
                </button>
                <button className="krm-save" onClick={() => void saveTagEdit()}>保存</button>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

/* ── 枚举行 ── */
function EnumRow({ connId, table, column, entry, onSave }: {
  connId: string
  table: string
  column: string
  entry: { value: string; meaning: string; status: string }
  onSave: (meaning: string) => void
}): React.JSX.Element {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(entry.meaning)
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11.5, padding: '2px 0' }}>
      <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--orange)', fontWeight: 500, minWidth: 76 }}>{entry.value}</span>
      {editing ? (
        <input autoFocus value={text} onChange={e => setText(e.target.value)}
          onBlur={() => { setEditing(false); if (text !== entry.meaning) onSave(text) }}
          onKeyDown={e => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur(); if (e.key === 'Escape') { setText(entry.meaning); setEditing(false) } }}
          style={{ background: 'var(--void-3)', border: '1px solid var(--accent)', borderRadius: 4, color: 'var(--ink)', fontSize: 11.5, padding: '1px 6px', flex: 1, outline: 'none' }} />
      ) : (
        <span onClick={() => { setText(entry.meaning); setEditing(true) }}
          style={{ color: entry.status === 'draft' ? 'var(--amber)' : 'var(--ink-dim)', cursor: 'text', flex: 1 }}>
          {entry.meaning || '—'}
        </span>
      )}
    </div>
  )
}

function hashStr(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
  return h
}
