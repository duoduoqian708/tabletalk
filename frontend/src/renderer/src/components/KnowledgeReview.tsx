import { useEffect, useMemo, useRef, useState } from 'react'
import { patchTable, fieldHistory, applyFieldHistory, type TableEditInput } from '@renderer/api/knowledge'
import { Dropdown } from './Dropdown'
import type { FieldHistoryItem, KbColumnView, KnowledgeOverview, RouteResult, TagInfo } from '@renderer/api/types'
import type { GraphNode as Graph3DNode, GraphEdge as Graph3DEdge } from './Graph3D'
import { useConnections } from '@renderer/store/connections'
import { useKnowledge } from '@renderer/store/knowledge'
import { useKbGate } from '@renderer/store/kbgate'
import { useI18n } from '@renderer/store/i18n'
import { assignUniqueColors, getTagColor, TAG_COLORS } from '@renderer/utils/tagColors'
import { toastMsg } from '@renderer/utils/toast'
import { tagColorForTable } from '@renderer/lib/colors'
import { syncIncremental } from '@renderer/api/knowledge'
import { fmtDT } from '@renderer/lib/timefmt'
import { Graph3D } from './Graph3D'
import { IconEdit, IconSearch, IconPlus, IconX, IconCheck, IconRefresh, IconLock } from "./ui/icons"
import { TableRelationGraph2D, computeInitialLayout } from './TableRelationGraph2D'
import { GraphEdgeList } from './GraphEdgeList'
import { KbHistoryDrawer } from './KbHistoryDrawer'
import { trgColumns, trgEdges, trgTables, useTrg2dActions } from '@renderer/hooks/useTrg2d'
import { ReviewLens } from './ReviewLens'

/* ═══════════════════════════════════════════════
   知识库主页（当前生效版本）
   顶部「知识库 | 审核 | 图库」模式切换（审核=镜头，仅 pending_review 期间出现）：
   - 知识库：三列（左=标签 · 中=按表结构聚合的表块 · 右=选中表详情面板）
   - 审核：同骨架审核镜头（变更集 + new/del 徽章 + 旧版对比 + 全暂存裁决栏，见 ReviewLens）
   - 图库：全宽关系图谱（展示/编辑双形态）
   右详情面板两块：向量化片段（可编辑覆盖） / 元数据（type 固定 table_schema）
   ═══════════════════════════════════════════════ */

function Tag({ name, status, color, onConfirm, onReject }: {
  name: string
  status: string
  color?: string
  onConfirm: () => void
  onReject: () => void
}): React.JSX.Element {
  const { t } = useI18n()
  return (
    <span className={`tag-chip ${status}`} style={{ '--tag-c': color } as React.CSSProperties}>
      {name}
      {status === 'draft' && (
        <span className="tag-acts">
          <button onClick={onConfirm} title={t('kb.confirmTitle')}><IconCheck size={9} /></button>
          <button onClick={onReject} title={t('kb.rejectTitle')}><IconX size={9} /></button>
        </span>
      )}
    </span>
  )
}

/** 未分类标签占位键（tags.tables 未绑定任何标签的表） */
const UNTAGGED = '__untagged__'

/* ═══════════════════════════════════════════════
   新建标签平台风弹窗（替代 window.prompt）
   名字 + 描述 + 色盘自选；颜色仅存前端 localStorage
   ═══════════════════════════════════════════════ */
function NewTagDialog({ connId, onClose }: {
  connId: string
  onClose: () => void
}): React.JSX.Element {
  const { t } = useI18n()
  const { load } = useKnowledge()
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [color, setColor] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  async function submit(): Promise<void> {
    const nm = name.trim()
    if (!nm || saving) return
    setSaving(true)
    try {
      const { createTag } = await import('@renderer/api/knowledge')
      await createTag(connId, nm, desc.trim(), color ?? '')
      await load(connId)
      onClose()
    } catch (e) {
      toastMsg(`${t('kb.createFail: ')}${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="kb-dialog-mask" onClick={onClose}>
      <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="kb-dialog-title">{t('kb.newTag')}</div>
        <label className="kb-field">
          <span className="kb-field-k mono">{t('kb.tagName')}</span>
          <input className="rs-input" autoFocus value={name} placeholder={t('kb.tagNamePh')}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void submit() }} />
        </label>
        <label className="kb-field">
          <span className="kb-field-k mono">{t('kb.tagDesc')}</span>
          <input className="rs-input" value={desc} placeholder={t('kb.tagDescPh')}
            onChange={(e) => setDesc(e.target.value)} />
        </label>
        <div className="kb-field">
          <span className="kb-field-k mono">{t('kb.tagColor')}</span>
          <div className="tag-swatches">
            <button type="button" className={`tag-sw auto${color === null ? ' on' : ''}`}
              title={t('kb.autoColorTitle')} onClick={() => setColor(null)}>A</button>
            {TAG_COLORS.map((c) => (
              <button key={c} type="button" className={`tag-sw${color === c ? ' on' : ''}`}
                style={{ background: c }} title={c} onClick={() => setColor(c)} />
            ))}
          </div>
        </div>
        <div className="kb-dialog-actions">
          <button className="btn ghost" onClick={onClose}>{t('common.cancel')}</button>
          <button className="btn save" disabled={saving || !name.trim()} onClick={() => void submit()}>
            {saving ? t('kb.saving') : t('kb.create')}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════
   编辑已有标签弹窗：改名（后端同步表绑定）/ 改描述 / 改颜色（后端持久化）
   ═══════════════════════════════════════════════ */
function EditTagDialog({ connId, tag, onClose }: {
  connId: string
  tag: { name: string; description: string; color: string }
  onClose: () => void
}): React.JSX.Element {
  const { t } = useI18n()
  const { load } = useKnowledge()
  const [name, setName] = useState(tag.name)
  const [desc, setDesc] = useState(tag.description)
  const [color, setColor] = useState<string | null>(tag.color || null)
  const [saving, setSaving] = useState(false)

  async function submit(): Promise<void> {
    const nm = name.trim()
    if (!nm || saving) return
    setSaving(true)
    try {
      const { updateTag } = await import('@renderer/api/knowledge')
      await updateTag(connId, tag.name, { newName: nm, description: desc.trim(), color: color ?? '' })
      await load(connId)
      onClose()
    } catch (e) {
      toastMsg(`${t('kb.saveFail: ')}${(e as Error).message}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="kb-dialog-mask" onClick={onClose}>
      <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="kb-dialog-title">{t('kb.editTag')}</div>
        <label className="kb-field">
          <span className="kb-field-k mono">{t('kb.tagName')}</span>
          <input className="rs-input" autoFocus value={name} placeholder={t('kb.tagNamePh')}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void submit() }} />
        </label>
        <label className="kb-field">
          <span className="kb-field-k mono">{t('kb.tagDesc')}</span>
          <input className="rs-input" value={desc} placeholder={t('kb.tagDescPh')}
            onChange={(e) => setDesc(e.target.value)} />
        </label>
        <div className="kb-field">
          <span className="kb-field-k mono">{t('kb.tagColor')}</span>
          <div className="tag-swatches">
            <button type="button" className={`tag-sw auto${color === null ? ' on' : ''}`}
              title={t('kb.autoColorTitle')} onClick={() => setColor(null)}>A</button>
            {TAG_COLORS.map((c) => (
              <button key={c} type="button" className={`tag-sw${color === c ? ' on' : ''}`}
                style={{ background: c }} title={c} onClick={() => setColor(c)} />
            ))}
          </div>
        </div>
        <div className="kb-dialog-actions">
          <button className="btn ghost" onClick={onClose}>{t('common.cancel')}</button>
          <button className="btn save" disabled={saving || !name.trim()} onClick={() => void submit()}>
            {saving ? t('kb.saving') : t('common.save')}
          </button>
        </div>
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════
   右：选中表详情面板 —— 两块（向量化片段 / 元数据）
   ═══════════════════════════════════════════════ */
function TableDetailPanel({ overview, selName, currentId, colorByTag, onOpenGraph }: {
  overview: KnowledgeOverview
  selName: string | null
  currentId: string
  colorByTag: Record<string, string>
  /** 图库跳转：关联关系由图库 Tab 负责，详情面板只留入口 */
  onOpenGraph?: () => void
}): React.JSX.Element {
  const { t } = useI18n()
  const { load, confirmComment, rejectComment } = useKnowledge()
  const [ddlOpen, setDdlOpen] = useState(false)
  /* 编辑状态：向量化片段 / 表注释 / 列知识 */
  const [vecEditing, setVecEditing] = useState(false)
  const [vecDraft, setVecDraft] = useState('')
  const [cmtEditing, setCmtEditing] = useState(false)
  const [cmtDraft, setCmtDraft] = useState('')
  const [colEditing, setColEditing] = useState<string | null>(null)
  const [colDraft, setColDraft] = useState({ comment: '', values: '', example: '' })
  /* 字段版本回溯：历史版本列表（确认时归档，N=3） */
  const [histOpen, setHistOpen] = useState<{ table: string; column: string } | null>(null)
  /* 提案对比弹窗：当前生效值 vs 本轮提案 */
  const [cmpOpen, setCmpOpen] = useState<{ table: string; column: string } | null>(null)
  const [histItems, setHistItems] = useState<FieldHistoryItem[]>([])
  const [histLoading, setHistLoading] = useState(false)

  function openHistory(table: string, column: string): void {
    setHistOpen({ table, column })
    setHistItems([])
    setHistLoading(true)
    void fieldHistory(currentId, table, column)
      .then((r) => setHistItems(r.items))
      .catch(() => setHistItems([]))
      .finally(() => setHistLoading(false))
  }

  async function applyHistory(item: FieldHistoryItem): Promise<void> {
    if (!histOpen) return
    try {
      await applyFieldHistory(currentId, histOpen.table, histOpen.column, item.id)
      setHistOpen(null)
      void load(currentId)
      toastMsg(t('kb.fieldHistoryApply'))
    } catch (e) {
      toastMsg(`${t('kb.revertFail: ')}${(e as Error).message}`)
    }
  }

  const tbl = useMemo(() => (selName ? overview.tables.find((tb) => tb.name === selName) ?? null : null),
    [overview, selName])

  if (!tbl) {
    return (
      <div className="tdp tdp-empty">
        <div className="tdp-empty-icon">◧</div>
        <div>{t('kb.detailEmpty')}</div>
      </div>
    )
  }

  async function saveEdit(input: TableEditInput): Promise<void> {
    try {
      await patchTable(currentId, input)
      setVecEditing(false); setCmtEditing(false); setColEditing(null)
      await load(currentId)
    } catch (e) {
      toastMsg(`${t('kb.saveFail: ')}${(e as Error).message}`)
    }
  }

  const beginVecEdit = (): void => { setVecDraft(tbl.vector_text); setVecEditing(true) }
  const beginCmtEdit = (): void => { setCmtDraft(tbl.comment ?? ''); setCmtEditing(true) }
  const beginColEdit = (c: KbColumnView): void => {
    setColEditing(c.name)
    setColDraft({ comment: c.comment, values: c.values, example: c.example })
  }

  return (
    <div className="tdp" key={tbl.name}>
      {/* ① 向量化片段：进 embedding 的检索文本，可编辑覆盖 */}
      <section className="tdp-sec">
        <div className="tdp-sec-h mono">
          {t('kb.vecChunk')}
          {overview.embedding_provider ? (
            <span className="emb-state on" title={t('kb.embOnTitle')}>{t('kb.embOn')}</span>
          ) : (
            <span className="emb-state off" title={t('kb.embUnconfiguredHint')}>{t('kb.embOff')}</span>
          )}
        </div>
        {vecEditing ? (
          <div className="kb-edit">
            <textarea className="kb-edit-ta vec-ta" autoFocus rows={6}
              value={vecDraft} onChange={(e) => setVecDraft(e.target.value)} />
            <div className="kb-edit-acts">
              <button className="btn ghost" onClick={() => setVecEditing(false)}>{t('common.cancel')}</button>
              <button className="btn save" onClick={() => void saveEdit({ table: tbl.name, vector_text: vecDraft })}>{t('common.save')}</button>
            </div>
          </div>
        ) : (
          <div className="tdp-vec">
            <div className="tdp-vec-head">
              {tbl.vector_override ? (
                <span className="tdp-vec-badge over">{t('kb.vecOverride')}</span>
              ) : tbl.vector_text ? (
                <span className="tdp-vec-badge">{t('kb.vecProfile')}</span>
              ) : (
                <span className="tdp-vec-badge none">{t('kb.vecNeedProfile')}</span>
              )}
              <span className="tdp-hint mono">({t('kb.vecScope')})</span>
              <span className="spacer" />
              {tbl.vector_override && (
                <button className="mini-edit" title={t('kb.vecResetTitle')}
                  onClick={() => void saveEdit({ table: tbl.name, vector_text: '' })}>
                  <IconX size={11} /> {t('kb.vecReset')}
                </button>
              )}
              <button className="mini-edit" onClick={beginVecEdit}><IconEdit size={11} /> {t('kb.edit')}</button>
            </div>
            {tbl.proposed_profile && (
              <div className="tdp-vec-pending mono" title={tbl.proposed_profile}>
                {t('kb.vecPendingProfile')}：{tbl.proposed_profile.length > 80 ? `${tbl.proposed_profile.slice(0, 80)}…` : tbl.proposed_profile}
              </div>
            )}
            <div className="tdp-vec-text">{tbl.vector_text || <span className="kb-none">{t('kb.noDesc')}</span>}</div>
          </div>
        )}
      </section>

      {/* ② 元数据：type 固定 table_schema；知识字段可编辑，schema 镜像只读 */}
      <section className="tdp-sec">
        <div className="tdp-sec-h mono">{t('kb.metadata')}</div>

        <div className="tdp-kv">
          <span className="tdp-kv-k mono">type</span>
          <span className="tdp-kv-v tdp-kv-lock mono">table_schema <span className="tdp-lock-ic" title={t('kb.typeLocked')}><IconLock size={10} /></span></span>
        </div>
        <div className="tdp-kv">
          <span className="tdp-kv-k mono">{t('kb.version')}</span>
          <span className="tdp-kv-v mono">v{overview.version ?? 0}</span>
        </div>
        <div className="tdp-kv">
          <span className="tdp-kv-k mono">{t('kb.tblName')}</span>
          <span className="tdp-kv-v mono">
            {tbl.name}
            {tbl.kind === 'view' && <span className="tdp-view-badge">view</span>}
            <span className="tdp-tcount mono">{tbl.column_count}</span>
          </span>
        </div>

        {/* 标签（独立字段展示，引用式带色） */}
        <div className="tdp-kv">
          <span className="tdp-kv-k mono">{t('kb.statsTags')}</span>
          <span className="tdp-kv-v tdp-kv-tags">
            {tbl.tags.length === 0 ? (
              <span className="kb-none">{t('kb.noDesc')}</span>
            ) : (
              tbl.tags.map((tg) => (
                <span key={tg.name} className="tag-chip confirmed"
                  style={{ '--tag-c': getTagColor(tg.name, colorByTag) } as React.CSSProperties}>
                  {tg.name}
                </span>
              ))
            )}
          </span>
        </div>

        {/* 表注释（知识字段 → 可编辑） */}
        <div className="tdp-kv">
          <span className="tdp-kv-k mono">{t('kb.detailTableComment')}</span>
          <span className="tdp-kv-v">
            {tbl.proposed_comment && (
              <span className="mini-acts">
                <button title={t('kb.applyProposal')} onClick={() => confirmComment(currentId, tbl.name)}><IconCheck size={10} /></button>
                <button title={t('kb.keepCurrent')} onClick={() => rejectComment(currentId, tbl.name)}><IconX size={10} /></button>
              </span>
            )}
            <button className="mini-edit" onClick={beginCmtEdit}><IconEdit size={11} /> {t('kb.edit')}</button>
          </span>
        </div>
        {cmtEditing ? (
          <div className="kb-edit">
            <textarea className="kb-edit-ta" autoFocus rows={3} value={cmtDraft}
              onChange={(e) => setCmtDraft(e.target.value)} />
            <div className="kb-edit-acts">
              <button className="btn ghost" onClick={() => setCmtEditing(false)}>{t('common.cancel')}</button>
              <button className="btn save" onClick={() => void saveEdit({ table: tbl.name, table_comment: cmtDraft })}>{t('common.save')}</button>
            </div>
          </div>
        ) : (
          <div className="tdp-comment-body">{tbl.comment || <span className="kb-none">{t('kb.noDesc')}</span>}</div>
        )}

        {/* DDL（schema 镜像 → 只读） */}
        <details className="tdp-ddl" open={ddlOpen} onToggle={(e) => setDdlOpen((e.currentTarget as HTMLDetailsElement).open)}>
          <summary className="mono">DDL</summary>
          <pre className="tdp-ddl-pre">{tbl.ddl || t('kb.noDdl')}</pre>
        </details>

        {/* 字段：name/type/pk/fk 只读；comment/values/example 知识字段可编辑 */}
        <div className="tdp-fields-h mono">
          {t('kb.detailColumns')} <span className="tdp-hint">({tbl.columns.length})</span>
          {/* pending_review 期间隐藏批量裁决（拆除清单#5：裁决唯一收尾点=审核台账印章栏），编辑保留 */}
          {overview?.kb_status !== 'pending_review' && (tbl.proposed_comment || tbl.columns.some((c) => c.proposed_comment || c.proposed_values || c.proposed_example)) && (
            <span className="mini-acts">
              <button title={t('kb.applyAll')} onClick={() => confirmComment(currentId, tbl.name)}><IconCheck size={10} /> {t('kb.applyAll')}</button>
              <button title={t('kb.keepAll')} onClick={() => rejectComment(currentId, tbl.name)}>⟲ {t('kb.keepAll')}</button>
            </span>
          )}
        </div>
        <div className="tdp-cols">
          {tbl.columns.map((col) => (
            <div key={col.name} className={`tdp-col ${col.status}`}>
              <div className="tdp-col-row">
                <span className={`st-dot ${col.status}`} />
                <span className="tdp-cname mono">{col.name}</span>
                <span className="tdp-ctype mono">{col.type}</span>
                {col.pk && <span className="ckey mono">PK</span>}
                {col.fk && <span className="ckey fk mono">FK</span>}
                {col.is_enum && <span className="ckey enum mono">ENUM</span>}
                <span className="spacer" />
                <button className="mini-edit" onClick={() => beginColEdit(col)}><IconEdit size={11} /></button>
                {(col.proposed_comment || col.proposed_values || col.proposed_example) && (
                  <span className="mini-acts">
                    <button title={t('kb.compareTitle')} onClick={() => setCmpOpen({ table: tbl.name, column: col.name })}>⧉</button>
                    {overview?.kb_status !== 'pending_review' && (
                      <>
                        <button title={t('kb.applyProposal')} onClick={() => confirmComment(currentId, tbl.name, col.name)}><IconCheck size={9} /></button>
                        <button title={t('kb.keepCurrent')} onClick={() => rejectComment(currentId, tbl.name, col.name)}><IconX size={9} /></button>
                      </>
                    )}
                  </span>
                )}
              </div>
              <div className="tdp-col-comment" title={col.db_comment ? `${t('kb.colDbComment')} ${col.db_comment}` : undefined}>
                {col.comment || (col.db_comment || <span className="kb-none">—</span>)}
              </div>
              {colEditing === col.name ? (
                <div className="kb-edit col-edit">
                  <label className="kb-field"><span className="kb-field-k mono">{t('kb.colComment')}</span>
                    <textarea className="rs-input" rows={2} value={colDraft.comment}
                      onChange={(e) => setColDraft((s) => ({ ...s, comment: e.target.value }))} />
                  </label>
                  <label className="kb-field"><span className="kb-field-k mono">{t('kb.colValues')}</span>
                    <input className="rs-input" value={colDraft.values}
                      onChange={(e) => setColDraft((s) => ({ ...s, values: e.target.value }))} />
                  </label>
                  <label className="kb-field"><span className="kb-field-k mono">{t('kb.colExample')}</span>
                    <input className="rs-input" value={colDraft.example}
                      onChange={(e) => setColDraft((s) => ({ ...s, example: e.target.value }))} />
                  </label>
                  <div className="kb-edit-acts">
                    <button className="btn ghost" onClick={() => setColEditing(null)}>{t('common.cancel')}</button>
                    <button className="btn ghost" onClick={() => openHistory(tbl.name, col.name)}>
                      ⟲ {t('kb.fieldHistory')}
                    </button>
                    <button className="btn save" onClick={() => void saveEdit({
                      table: tbl.name,
                      column_comments: [{ name: col.name, ...colDraft }],
                    })}>{t('common.save')}</button>
                  </div>
                </div>
              ) : (
                (col.values || col.example) && (
                  <div className="tdp-col-meta">
                    {col.values && <span className="cvals" title={col.values}>{t('kb.colValues')}: {col.values}</span>}
                    {col.example && <span className="cexample mono" title={col.example}>{t('kb.colExample')} {col.example}</span>}
                  </div>
                )
              )}
            </div>
          ))}
          {tbl.columns.length === 0 && <div className="rv-none mono">{t('kb.noColumns')}</div>}
        </div>

        {/* 关联关系归属图库 Tab：这里只留跳转入口（外键/LLM 草案边/手绘边全在图库） */}
        <div className="tdp-graph-link" onClick={onOpenGraph}>
          <span className="mono">◈ {t('kb.relationsOwner')}</span>
          <span className="tdp-graph-link-go">{t('kb.goGraph')} →</span>
        </div>
      </section>

      {/* 字段版本回溯：历史版本列表（点击应用覆盖当前字段） */}
      {histOpen && (
        <div className="kb-dialog-mask" onClick={() => setHistOpen(null)}>
          <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="kb-dialog-title">{t('kb.fieldHistory')} · <span className="mono">{histOpen.table}.{histOpen.column}</span></div>
            <div className="fh-list">
              {histLoading && <div className="rv-none mono">{t('common.loading')}</div>}
              {!histLoading && histItems.length === 0 && <div className="rv-none mono">{t('kb.fieldHistoryEmpty')}</div>}
              {histItems.map((it) => (
                <div key={it.id} className="fh-row" title={t('kb.fieldHistoryApply')}
                  onClick={() => void applyHistory(it)}>
                  <div className="fh-head mono">
                    <b>v{it.version}</b>
                    <span>{fmtDT(it.batch_ts)}</span>
                    {it.status && <em className="fh-status">{it.status}</em>}
                  </div>
                  {it.comment && <div className="fh-body">{it.comment}</div>}
                  {it.values && <div className="fh-meta mono">{t('kb.colValues')}: {it.values}</div>}
                  {it.example && <div className="fh-meta mono">{t('kb.colExample')}: {it.example}</div>}
                </div>
              ))}
            </div>
            <div className="kb-dialog-actions">
              <button className="btn ghost" onClick={() => setHistOpen(null)}>{t('common.cancel')}</button>
            </div>
          </div>
        </div>

      )}
      {/* 提案对比：当前生效值 vs 本轮提案 */}
      {cmpOpen && (() => {
        const ct = overview.tables.find((tt) => tt.name === cmpOpen.table)
        const col = ct?.columns.find((cc) => cc.name === cmpOpen.column)
        if (!col) return null
        return (
          <div className="kb-dialog-mask" onClick={() => setCmpOpen(null)}>
            <div className="kb-dialog kb-cmp" onClick={(e) => e.stopPropagation()}>
              <div className="kb-dialog-title">{t('kb.compare')} · <span className="mono">{cmpOpen.table}.{cmpOpen.column}</span>
                <span className="tdp-hint"> {t('kb.compareHint')}</span></div>
              <div className="kb-cmp-grid">
                <div className="kb-cmp-side">
                  <div className="kb-cmp-h mono">{t('kb.currentVal')}</div>
                  <div className="kb-cmp-body">{col.comment || <span className="kb-none">—</span>}</div>
                  <div className="kb-cmp-meta">
                    {col.values && <div className="cvals" title={col.values}>{t('kb.colValues')}: {col.values}</div>}
                    {col.example && <div className="cexample mono">{t('kb.colExample')} {col.example}</div>}
                  </div>
                  <button className="btn ghost" onClick={() => { void rejectComment(currentId, cmpOpen.table, cmpOpen.column); setCmpOpen(null) }}>⟲ {t('kb.keepCurrent')}</button>
                </div>
                <div className="kb-cmp-side">
                  <div className="kb-cmp-h mono">{t('kb.proposal')}</div>
                  <div className="kb-cmp-body">{col.proposed_comment || <span className="kb-none">—</span>}</div>
                  <div className="kb-cmp-meta">
                    {col.proposed_values && <div className="cvals" title={col.proposed_values}>{t('kb.colValues')}: {col.proposed_values}</div>}
                    {col.proposed_example && <div className="cexample mono">{t('kb.colExample')} {col.proposed_example}</div>}
                  </div>
                  <button className="btn save" onClick={() => { void confirmComment(currentId, cmpOpen.table, cmpOpen.column); setCmpOpen(null) }}><IconCheck size={10} /> {t('kb.applyProposal')}</button>
                </div>
              </div>
            </div>
          </div>
        )
      })()}

    </div>
  )
}

/* ═══════════════════════════════════════════════
   知识库主页
   ═══════════════════════════════════════════════ */
export function KnowledgeReview(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { overview, loading, busy, error, load, dismissError,
    confirmComment, rejectComment, confirmTag, rejectTag, assignTags } = useKnowledge()
  const { t } = useI18n()
  const openBuildDialog = useKbGate((s) => s.openBuildDialog)

  const [mode, setMode] = useState<'kb' | 'review' | 'graph'>('kb')
  const [selTags, setSelTags] = useState<Set<string>>(new Set())
  const [selTable, setSelTable] = useState<string | null>(null)
  const [showDraftOnly, setShowDraftOnly] = useState(false)
  const [route, setRoute] = useState<RouteResult | null>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [kq, setKq] = useState('')
  const [newTagOpen, setNewTagOpen] = useState(false)
  const [editTag, setEditTag] = useState<TagInfo | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [editText, setEditText] = useState('')
  /** 图库 Tab 形态：display=3D 展示态（只读）| edit=2D 编辑态（拖线/增删边/持久化布局） */
  const [graphMode, setGraphMode] = useState<'display' | 'edit'>('display')
  /** 2D 编辑页：关系预览列表选中的边 key（联动高亮定位） */
  const [hlEdgeKey, setHlEdgeKey] = useState<string | null>(null)
  /** {t('kb.history')}抽屉（审计 origin=kb_build 的构建/重建/放弃/启用留痕） */
  const [historyOpen, setHistoryOpen] = useState(false)
  /** 增量同步任务进度（与构建同一 job 通道，按 kind 区分：构建期间不误显"同步中"） */
  const syncTask = useKnowledge((s) => s.syncTask)
  const syncBusy = useKnowledge((s) => s.busy)
  const buildProgress = useKnowledge((s) => s.buildProgress)
  const syncing = syncBusy && buildProgress !== null && buildProgress.connId === currentId && buildProgress.kind === 'sync'
  const trg2dActions = useTrg2dActions(currentId)
const handleLayoutChange = (layout: Record<string, { x: number; y: number }>): void => {
    trg2dActions.onLayoutChange(layout)
  }
  /** 一键重新铺开：均匀网格布局全量落盘（覆盖旧布局），并刷新 overview 使坐标生效 */
  const handleResetLayout = (): void => {
    if (!overview || !currentId) return
    const fresh = computeInitialLayout(trgTables(overview))
    trg2dActions.onLayoutChange(fresh)
    void load(currentId)
  }

  const totalPending = (overview?.draft_count ?? 0)
    + (overview?.tag_draft_count ?? 0)
    + (overview?.graph?.llm_draft_edges?.length ?? 0)

  /* 图库 Tab Graph3D 数据映射（overview → Graph3D props，禁用双击预览） */
  const g3dNodes: Graph3DNode[] = useMemo(
    () => overview?.tables.map((t) => ({ name: t.name, row_count: t.column_count, column_count: t.column_count, kind: t.kind as 'table' | 'view' })) ?? [],
    [overview?.tables],
  )
  const g3dEdges: Graph3DEdge[] = useMemo(
    // 透传完整 API GraphEdge（含 guard/cols/kinds/cardinality），3D 边弹窗展示详情
    () => overview?.graph.edges ?? [],
    [overview?.graph.edges],
  )

  /* 标签色：后端持久化色优先（引用式，改一处全端同步），缺省哈希色板展示期不撞色 */
  const colorByTag = useMemo(() => {
    const names = (overview?.tags.library ?? []).map((x) => x.name)
    const base = assignUniqueColors(names)
    const fromData: Record<string, string> = {}
    for (const tg of overview?.tags.library ?? []) {
      if (tg.color) fromData[tg.name] = tg.color
    }
    return { ...base, ...fromData }
  }, [overview?.tags.library])

  /* 节点颜色映射：标签色驱动，无标签=基准灰，多标签=混色 */
  const nodeColorMap = useMemo(() => {
    if (!overview) return {}
    const m: Record<string, string> = {}
    for (const tb of overview.tables) m[tb.name] = tagColorForTable(tb, colorByTag)
    return m
  }, [overview, colorByTag])

  useEffect(() => {
    if (currentId) void load(currentId)
  }, [currentId, load])

  // 进页提醒弹窗已删除（2026-09 拆除清单）：pending_review 由审核 Tab + 门禁卡引导，不再自动弹窗
  const reviewWanted = useKbGate((s) => s.reviewWantedConnId)
  const consumeReview = useKbGate((s) => s.consumeReview)
  useEffect(() => {
    if (reviewWanted && reviewWanted === currentId && overview?.kb_status === 'pending_review') {
      consumeReview(currentId)
      setMode('review')
    }
  }, [reviewWanted, currentId, overview?.kb_status, consumeReview])

  // 完成引导（2026-09）：构建完成（kb_status 转入 pending_review）且用户正停在知识库页
  // → toast + 自动跳进审核台账；仅监听"转变"，后进页/刷新不误跳（状态本身不触发）
  const prevKbStatus = useRef<string | null | undefined>(undefined)
  useEffect(() => {
    const cur = overview?.kb_status ?? null
    if (prevKbStatus.current !== undefined && cur === 'pending_review'
        && prevKbStatus.current !== 'pending_review' && mode !== 'review') {
      toastMsg(t('kb.review.buildDoneJump', { n: totalPending }))
      setMode('review')
    }
    prevKbStatus.current = cur
  }, [overview?.kb_status, totalPending, mode, t])

  useEffect(() => {
    if (selTags.size > 0) {
      void import('@renderer/api/knowledge').then(({ routeTables }) =>
        routeTables(currentId ?? '', [...selTags]).then(setRoute).catch(() => setRoute(null))
      )
    } else {
      setRoute(null)
    }
  }, [selTags, currentId])

  if (!currentId) {
    return <div className="review"><div className="review-empty">{t('kb.connectFirst')}</div></div>
  }

  const toggleTag = (name: string): void => {
    setSelTags((s) => {
      const n = new Set(s)
      if (n.has(name)) n.delete(name)
      else n.add(name)
      return n
    })
  }

  /* 标签 → 表 反向索引（tag 名 → 关联表名列表） */
  const tablesByTag = useMemo(() => {
    const m: Record<string, string[]> = {}
    for (const [tbl, names] of Object.entries(overview?.tags.tables ?? {}))
      for (const n of names as string[]) (m[n] ??= []).push(tbl)
    return m
  }, [overview?.tags.tables])

  const confirmedTags = overview?.tags.library.filter((t) => t.status === 'confirmed') ?? []
  const pendingTags = overview?.tags.library.filter((t) => t.status === 'draft') ?? []
  const tagRows = (overview?.tags.library ?? []).map((tg) => ({
    ...tg,
    count: (tablesByTag[tg.name] ?? []).length,
  }))
  const tagCount = Object.fromEntries(tagRows.map((r) => [r.name, r.count]))
  const untaggedCount = overview?.tables.filter((tb) => tb.tags.length === 0).length ?? 0

  /* 中间列过滤：本地表名检索 + 选中标签（任一命中）∪ 未分类 + 只看待确认（三级复合，零网络） */
  const filteredTables = useMemo(() => {
    if (!overview) return []
    const q = kq.trim().toLowerCase()
    const wanted = [...selTags].filter((n) => n !== UNTAGGED)
    let ts = overview.tables
    if (wanted.length > 0 || selTags.has(UNTAGGED)) {
      ts = ts.filter((tb) => {
        const matchTag = wanted.length === 0 || tb.tags.some((tg) => wanted.includes(tg.name))
        const matchUntagged = selTags.has(UNTAGGED) && tb.tags.length === 0
        return matchTag || matchUntagged
      })
    }
    if (q) ts = ts.filter((tb) => tb.name.toLowerCase().includes(q))
    if (showDraftOnly) {
      ts = ts.filter((tb) => tb.proposed_comment || tb.columns.some((c) => c.proposed_comment || c.proposed_values || c.proposed_example))
    }
    return ts
  }, [overview, selTags, showDraftOnly, kq])

  const notBuilt = overview !== null && (overview.built === false || overview.kb_status === 'none')

  function beginEdit(table: string, comment: string): void {
    setEditing(table)
    setEditText(comment)
  }

  function saveListComment(table: string, text: string): void {
    if (!currentId) return
    void patchTable(currentId, { table, table_comment: text })
      .then(() => { setEditing(null); return load(currentId) })
      .catch((e) => toastMsg(`${t('kb.saveFail: ')}${(e as Error).message}`))
  }

  /* 手动增量同步（任务化）：无变化同步返回；有变化接 job 进度（与构建同一通道），完成后刷新 */
  async function doSync(): Promise<void> {
    if (!currentId) return
    try {
      const r = await syncIncremental(currentId)
      if (!r.changed) {
        toastMsg(t('kb.syncNoChange'))
        return
      }
      await syncTask(currentId)
      await load(currentId)
      const st = useKnowledge.getState().overview?.kb_status
      if (st === 'pending_review') toastMsg(t('kb.syncDoneReview'))
      else toastMsg(t('kb.syncDoneGeneric'))
    } catch (e) {
      toastMsg(t('kb.review.applyFail', { msg: (e as Error).message }))
    }
  }

  return (
    <div className="review kb-page">
      {error && (
        <div className="review-err mono">
          <span className="review-err-text">{error}</span>
          <button className="review-err-x" onClick={dismissError} title={t('common.close')}>✕</button>
        </div>
      )}

      {notBuilt ? (
        <div className="kb-not-built">
          <div className="kicker">knowledge base</div>
          <div className="kb-nb-title">{t('kb.notBuilt')}</div>
          <div className="kb-nb-text">
            {t('kb.notBuiltDesc')}
          </div>
          <button className="btn save" disabled={busy} onClick={() => openBuildDialog('init')}>
            {busy ? `${t('kb.buildingLabel')}…` : t('kb.build')}
          </button>
        </div>
      ) : loading && !overview ? (
        <div className="review-loading mono">{t('common.loading')}</div>
      ) : overview ? (
        <>
          {/* 顶部：状态 + 操作 */}
          <div className="kb-topbar">
            <div className="kb-mode-toggle" role="tablist">
              <button type="button" className={`kb-mode-btn${mode === 'kb' ? ' on' : ''}`} onClick={() => setMode('kb')}>
                {t('kb.tabKb')}
              </button>
              {/* 审核 = 中间态（2026-09 用户裁定）：不与知识库/图库并列为常驻 Tab。
                  入口：构建完成自动跳转 / 门禁卡 CTA / 待审 chip（下） */}
              <button type="button" className={`kb-mode-btn${mode === 'graph' ? ' on' : ''}`} onClick={() => setMode('graph')}>
                {t('kb.tabGraph')}
              </button>
            </div>
            {mode !== 'review' && overview.kb_status === 'pending_review' && totalPending > 0 && (
              /* 待审 chip = 台账回头入口（关闭台账后可重新进入） */
              <button type="button" className="kb-topbar-pending mono as-btn" onClick={() => setMode('review')}
                      title={t('kb.review.openLedger')}>
                {t('kb.review.pendingChip', { n: totalPending })} →
              </button>
            )}
            <span className="spacer" />
            {overview.synced_at && (
              <span className="kb-synced mono" title={t('kb.syncedTitle')}>
                {t('kb.lastSync')} {fmtDT(overview.synced_at)}
              </span>
            )}
            <button className="iconbtn" onClick={() => setHistoryOpen(true)} title={t('kb.historyTitle')}>
              {t('kb.history')}
            </button>
            <button className="iconbtn rebuild-btn" onClick={() => openBuildDialog('rebuild')} disabled={busy}
                    title={t('kb.rebuildTitle')}>
              {busy ? t('kb.buildingLabel') : t('kb.rebuildAll')}
            </button>
            <button className="iconbtn" onClick={() => void doSync()} disabled={busy || syncing}
                    title={syncing ? t('kb.syncingTitle') : t('kb.syncTitle')}>
              {syncing ? t('kb.syncingLabel') : t('kb.syncIncr')}
            </button>
          </div>

          {/* 进页提醒弹窗已删除（拆除清单#1）：不再自动弹窗 */}

          {mode === 'review' ? (
            /* ════════ 审核镜头：同骨架变更集 + 全暂存裁决栏（唯一收尾点） ════════ */
            <ReviewLens onClose={() => setMode('kb')} />
          ) : mode === 'graph' ? (
            /* ════════ 图库 Tab：3D/2D 切换 + 草案边审阅 ════════ */
            <div className="kb-graph">
              <div className="kb-right-cap mono">
                {t('kb.graphCap')}
                <span className="kb-rc-hint">{t('kb.graphHint')}</span>
                {(overview.graph.llm_draft_edges?.length ?? 0) > 0 && (
                  <span className="kb-draft-badge mono">{t('kb.kindGraph')} · {t('kb.draft')} {(overview.graph.llm_draft_edges ?? []).length}</span>
                )}
                <span className="spacer" />
                <span className="kb-mode-toggle" role="tablist">
                  <button type="button" className={`kb-mode-btn${graphMode === 'display' ? ' on' : ''}`} onClick={() => setGraphMode('display')}>
                    {t('kb.graphModeBrowse')}
                  </button>
                  <button type="button" className={`kb-mode-btn${graphMode === 'edit' ? ' on' : ''}`} onClick={() => setGraphMode('edit')}>
                    {t('kb.graphModeEdit2d')}
                  </button>
                </span>
              </div>
              <div className="kb-graph-body">
                {graphMode === 'display' ? (
                  <div className="kb-trg2d-wrap">
                    <Graph3D
                      tables={g3dNodes}
                      foreignKeys={g3dEdges}
                      onSelectNode={() => {}}
                      onOpenData={() => {}}
                      colorOverride={nodeColorMap}
                    />
                  </div>
                ) : (
<div className="trg2d-split">
                    <GraphEdgeList edges={trgEdges(overview)} activeKey={hlEdgeKey}
                      onPick={(key) => setHlEdgeKey((cur) => (cur === key ? null : key))} />
                    <div className="kb-trg2d-wrap">
                      <button type="button" className="trg2d-reset" onClick={handleResetLayout}
                        title={t('kb.resetLayoutTitle')}><IconRefresh size={10} /> {t('kb.resetLayout')}</button>
                      <TableRelationGraph2D
                          tables={trgTables(overview)}
                          edges={trgEdges(overview)}
                          columnsByTable={trgColumns(overview)}
                          layout={overview.graph.layout}
                          mode="edit"
                          colorMap={nodeColorMap}
                          highlightKey={hlEdgeKey}
                          onAddEdge={(e) => trg2dActions.onAddEdge(e)}
                          onDeleteEdge={(e) => trg2dActions.onDeleteEdge(e)}
                          onConfirmEdge={(e) => trg2dActions.onConfirmEdge(e)}
                          onLayoutChange={handleLayoutChange}
                          onTableClick={(name) => setSelTable((prev) => (prev === name ? null : name))}
                        />
                      </div>
                  </div>
                )}
                {(overview.graph.llm_draft_edges ?? []).length > 0 && (
                  <div className="kb-drafts">
                    <div className="kb-drafts-h mono">{t('kb.kindGraph')} · {t('kb.draft')}</div>
                    <div className="kb-drafts-list">
                      {(overview.graph.llm_draft_edges ?? []).map((edge, idx) => (
                        <div key={`ge-${idx}-${edge.from_table}-${edge.to_table}`} className={`rv-card rv-graph-edge${edge.status === 'previously_rejected' ? ' previously-rejected' : ''}`}>
                          <div className="rv-card-kind rv-kind-graph">{t('kb.kindGraph')}
                            {edge.diff === 'new' && <em className="rv-edge-badge diff-new">{t('kb.diffNew')}</em>}
                            {edge.diff === 'modified' && <em className="rv-edge-badge diff-mod">{t('kb.diffModified')}</em>}
                          </div>
                          <div className="rv-card-main">
                            <div className="rv-card-ctx">
                              <span className="mono">{edge.from_table}</span>
                              {edge.from_col && <span className="rv-edge-col">.{edge.from_col}</span>}
                              <span className="rv-edge-arrow"> → </span>
                              <span className="mono">{edge.to_table}</span>
                              {edge.to_col && <span className="rv-edge-col">.{edge.to_col}</span>}
                            </div>
                            {edge.status === 'previously_rejected' && (
                              <div className="rv-edge-rejected-hint">{t('kb.graphEdgeRejectedHint')}</div>
                            )}
                            {edge.reason && <div className="rv-card-body rv-edge-reason">{edge.reason}</div>}
                          </div>
                          <div className="rv-card-acts">
                            <button className="rv-btn-ok" onClick={() => confirmGraphDraftSafe(edge.from_table)} title={edge.status === 'previously_rejected' ? t('kb.graphEdgeRestoreTitle') : t('kb.confirmTitle')}><IconCheck size={13} /></button>
                            {edge.status !== 'previously_rejected' && (
                              <button className="rv-btn-no" onClick={() => rejectGraphDraftSafe(edge.from_table)}><IconX size={13} /></button>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          ) : (
            /* ════════ 知识库 Tab：三列（标签 | 表聚合 | 详情面板） ════════ */
            <div className="kb-cols-3">
              {/* ── 左：标签 ── */}
              <section className="kb-panel-tags">
                <div className="kb-panel-h mono">
                  {t('kb.statsTags')}
                  <span className="tdp-hint">({tagRows.length})</span>
                </div>
                <div className="kb-tag-list">
                  {pendingTags.length > 0 && (
                    <div className="kb-pending-tags">
                      <div className="kb-route-cap mono">{t('kb.pendingGroup')} <span className="tdp-hint">({pendingTags.length})</span></div>
                      {pendingTags.map((tg) => (
                        <div key={tg.name} className={`kb-tag-row${selTags.has(tg.name) ? ' on' : ''}`}
                          style={{ '--tag-c': getTagColor(tg.name, colorByTag) } as React.CSSProperties}
                          onClick={() => toggleTag(tg.name)}>
                          <span className="kb-tag-chip">
                            <span className="kb-tag-name">{tg.name}</span>
                          </span>
                          <button className="mini-edit" title={t('kb.editTag')}
                            onClick={(e) => { e.stopPropagation(); setEditTag(tg) }}><IconEdit size={11} /></button>
                          <span className="mini-acts">
                            <button title={t('kb.confirmTitle')} onClick={(e) => { e.stopPropagation(); void confirmTag(currentId, tg.name) }}><IconCheck size={10} /></button>
                            <button title={t('kb.rejectTitle')} onClick={(e) => { e.stopPropagation(); void rejectTag(currentId, tg.name) }}><IconX size={10} /></button>
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="kb-current-tags">
                    <div className="kb-route-cap mono">{t('kb.currentGroup')} <span className="tdp-hint">({confirmedTags.length})</span></div>
                    {confirmedTags.length === 0 && <div className="rv-none mono">{t('kb.noConfirmedTags')}</div>}
                    {confirmedTags.map((tg) => {
                      const on = selTags.has(tg.name)
                      const color = getTagColor(tg.name, colorByTag)
                      return (
                        <div key={tg.name} className={`kb-tag-row${on ? ' on' : ''}`}
                          style={{ '--tag-c': color } as React.CSSProperties}
                          onClick={() => toggleTag(tg.name)}>
                          <span className="kb-tag-chip">
                            <span className="kb-tag-name">{tg.name}</span>
                            <span className="kb-tag-cnt mono">{tagCount[tg.name] ?? 0}</span>
                          </span>
                          <button className="mini-edit" title={t('kb.editTag')}
                            onClick={(e) => { e.stopPropagation(); setEditTag(tg) }}><IconEdit size={11} /></button>
                          <span className="mini-acts">
                            <button title={t('kb.deleteTagTitle')} onClick={(e) => { e.stopPropagation(); void rejectTag(currentId, tg.name) }}><IconX size={10} /></button>
                          </span>
                          {on && <span className="kb-tag-active mono"><IconCheck size={9} /></span>}
                        </div>
                      )
                    })}
                    <div className={`kb-tag-row untagged${selTags.has(UNTAGGED) ? ' on' : ''}`} onClick={() => toggleTag(UNTAGGED)}>
                      <span style={{ width: 9, height: 9, borderRadius: '50%', border: '1.5px dashed var(--ink-faint)', flexShrink: 0 }} />
                      <span className="kb-tag-name" style={{ color: 'var(--ink-dim)' }}>{t('kb.untagged')}</span>
                      <span className="kb-tag-cnt mono">{untaggedCount}</span>
                    </div>
                  </div>
                  <button className="kb-tag-add-new" onClick={() => setNewTagOpen(true)}><IconPlus size={10} /> {t('kb.newTag')}</button>
                </div>
                {selTags.size > 0 && (
                  <div className="kb-route-preview">
                    <div className="kb-route-cap mono">{t('kb.routePreview')} · {t('kb.filtering', { n: selTags.size })}</div>
                    {route ? (
                      <div className="rv-route-result mono">{t('kb.candTables', { n: route.tables.length })}：{route.tables.join(' · ')}</div>
                    ) : (
                      <div className="rv-none mono">{t('kb.routePreviewHint')}</div>
                    )}
                    <button className="kb-clear-filter" onClick={() => setSelTags(new Set())}><IconX size={9} /> {t('kb.clearFilter')}</button>
                  </div>
                )}
              </section>

              {/* ── 中：按表结构聚合（本地表名检索实时过滤） ── */}
              <section className="kb-panel-mid">
                <div className="kb-mid-head">
                  <div className="kb-search">
                    <input
                      className="rs-input"
                      value={kq}
                      placeholder={t('kb.searchTablePlaceholder')}
                      onChange={(e) => setKq(e.target.value)}
                    />
                    <button className="kb-qbtn" disabled={!kq.trim()} title={t('kb.searchTblTitle')} onClick={() => setKq(kq.trim())}><IconSearch size={12} /> {t('kb.search')}</button>
                    <button className={`kb-qbtn${showDraftOnly ? ' on' : ''}`} onClick={() => setShowDraftOnly((v) => !v)}>{t('kb.onlyDraft')}</button>
                    {selTags.size > 0 && (
                      <button className="kb-qbtn on" onClick={() => setSelTags(new Set())}>{t('kb.filtering', { n: selTags.size })} <IconX size={9} /></button>
                    )}
                  </div>
                  <span className="spacer" />
                  <span className="kb-count mono">{t('kb.tableCount', { n: filteredTables.length })}</span>
                </div>

                <div className="rv-table-list kb-docs">
                  {filteredTables.length === 0 && (
                    <div className="rv-empty mono">{t('kb.noMatch')}</div>
                  )}
                  {filteredTables.map((tbl) => {
                    return (
                      <div key={tbl.name} className="rv-table" data-tname={tbl.name}
                        style={{ borderLeftColor: tagColorForTable(tbl, colorByTag) }}>
                        <div className={`rv-table-row${selTable === tbl.name ? ' sel' : ''}`}
                          onClick={() => setSelTable(tbl.name)}>
                          <span className="tname mono">{tbl.name}</span>
                          <span className="tcols mono">{tbl.column_count}</span>
                          <span className={`st-dot ${tbl.comment_status}`} />
                        </div>
                        <div className="rv-desc">
                          {editing === tbl.name ? (
                            <div className="kb-edit">
                              <textarea
                                autoFocus
                                className="kb-edit-ta"
                                value={editText}
                                onChange={(e) => setEditText(e.target.value)}
                                rows={3}
                              />
                              <div className="kb-edit-acts">
                                <button className="btn ghost" onClick={() => setEditing(null)}>{t('common.cancel')}</button>
                                <button className="btn save" onClick={() => saveListComment(tbl.name, editText)}>{t('common.save')}</button>
                              </div>
                            </div>
                          ) : (
                            <>
                              <span className="desc-text">{tbl.comment || <span className="kb-none">{t('kb.noDesc')}</span>}</span>
                              <span className="mini-acts">
                                <button onClick={() => beginEdit(tbl.name, tbl.comment)} title={t('kb.editNoteTitle')}><IconEdit size={11} /></button>
                                {tbl.comment_status === 'draft' && overview?.kb_status !== 'pending_review' && (
                                  <>
                                    <button onClick={() => confirmComment(currentId, tbl.name)}>{t('common.confirm')}</button>
                                    <button onClick={() => rejectComment(currentId, tbl.name)}>{t('kb.reject')}</button>
                                  </>
                                )}
                              </span>
                            </>
                          )}
                        </div>
                        <div className="rv-tags">
                          {tbl.tags.map((tg) => (
                            <Tag key={tg.name} name={tg.name} status={tg.status} color={getTagColor(tg.name, colorByTag)}
                              onConfirm={() => confirmTag(currentId, tg.name)}
                              onReject={() => rejectTag(currentId, tg.name)} />
                          ))}
                          {adding === tbl.name ? (
                            <Dropdown
                              autoFocus
                              className="tag-add"
                              style={{ width: 140 }}
                              value=""
                              placeholder="…"
                              options={[
                                { value: '', label: '…' },
                                ...confirmedTags.filter((c) => !tbl.tags.some((x) => x.name === c.name)).map((c) => ({ value: c.name, label: c.name })),
                              ]}
                              onChange={(v) => {
                                if (v) void assignTags(currentId, tbl.name, [...tbl.tags.map((x) => x.name), v])
                                setAdding(null)
                              }}
                              onClose={() => setAdding(null)}
                            />
                          ) : (
                            <button className="tag-add-btn" onClick={() => setAdding(tbl.name)}><IconPlus size={10} /></button>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </section>

              {/* ── 右：详情面板（两块：向量化片段 / 元数据） ── */}
              <section className="kb-panel-detail">
                <TableDetailPanel overview={overview} selName={selTable} currentId={currentId} colorByTag={colorByTag}
                  onOpenGraph={() => setMode('graph')} />
              </section>
            </div>
          )}

          <div className="kb-stats mono">
            <span>{t('kb.statsDocs')} {overview.tables.length + overview.tables.reduce((n, tb) => n + tb.columns.length, 0)}</span>
            <span>{t('kb.statsTags')} {overview.tags.library.length}</span>
            <span>{t('kb.statsEdges')} {overview.graph.edges.length}</span>
            <span className="kb-stats-draft">{t('kb.statsDraft')} {totalPending}</span>
          </div>
        </>
      ) : null}

      {newTagOpen && currentId && (
        <NewTagDialog connId={currentId} onClose={() => setNewTagOpen(false)} />
      )}

      {editTag && currentId && (
        <EditTagDialog connId={currentId} tag={editTag} onClose={() => setEditTag(null)} />
      )}

      {/* {t('kb.history')}抽屉：审计 origin=kb_build 留痕 */}
      <KbHistoryDrawer
        open={historyOpen}
        connId={currentId}
        connName={connName}
        onClose={() => setHistoryOpen(false)}
      />
    </div>
  )

  /* 局部辅助（必须在 render 内声明以便使用 currentId 等闭包） */
  function confirmGraphDraftSafe(fromTable: string): void {
    if (currentId) void useKnowledge.getState().confirmGraphDraft(currentId, fromTable)
  }
  function rejectGraphDraftSafe(fromTable: string): void {
    if (currentId) void useKnowledge.getState().rejectGraphDraft(currentId, fromTable)
  }
}