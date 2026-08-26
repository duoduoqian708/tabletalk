import { useEffect, useMemo, useState } from 'react'
import { retrieve, type KbCard } from '@renderer/api/knowledge'
import type { GraphEdge, KbTableView, KnowledgeOverview, RouteResult } from '@renderer/api/types'
import type { GraphNode as Graph3DNode, GraphEdge as Graph3DEdge } from './Graph3D'
import { useConnections } from '@renderer/store/connections'
import { useKnowledge } from '@renderer/store/knowledge'
import { useKbGate } from '@renderer/store/kbgate'
import { useI18n } from '@renderer/store/i18n'
import { getTagColor } from '@renderer/utils/tagColors'
import { toastMsg } from '@renderer/utils/toast'
import { tagColorForTable } from '@renderer/lib/colors'
import { Graph3D } from './Graph3D'
import { TableRelationGraph2D } from './TableRelationGraph2D'
import { KbHistoryDrawer } from './KbHistoryDrawer'
import { trgColumns, trgEdges, trgTables, useTrg2dActions } from '@renderer/hooks/useTrg2d'

/* ═══════════════════════════════════════════════
   知识库主页（审阅/管理）
   顶部「知识库 | 图库」双 Tab：
   - 知识库：三列（左=标签 · 中=按表结构聚合的表块 · 右=选中表详情面板）
   - 图库：全宽关系图谱（展示/编辑双形态）+ 草案边逐条审阅
   审核动作全部行内化（表块/标签/字段/草案边 ✓✕），历史记录抽屉保留
   ═══════════════════════════════════════════════ */

function Tag({ name, status, onConfirm, onReject }: {
  name: string
  status: string
  onConfirm: () => void
  onReject: () => void
}): React.JSX.Element {
  const { t } = useI18n()
  return (
    <span className={`tag-chip ${status}`}>
      {name}
      {status === 'draft' && (
        <span className="tag-acts">
          <button onClick={onConfirm} title={t('kb.confirmTitle')}>✓</button>
          <button onClick={onReject} title={t('kb.rejectTitle')}>✕</button>
        </span>
      )}
    </span>
  )
}

/** 未分类标签占位键（tags.tables 未绑定任何标签的表） */
const UNTAGGED = '__untagged__'

/* ═══════════════════════════════════════════════
   右：选中表详情面板（表信息 / 字段详情 / 相关表 / 存储形态）
   ═══════════════════════════════════════════════ */
function TableDetailPanel({ overview, selName, currentId }: {
  overview: KnowledgeOverview
  selName: string | null
  currentId: string
}): React.JSX.Element {
  const { t } = useI18n()
  const { confirmComment, rejectComment } = useKnowledge()
  const [relExpanded, setRelExpanded] = useState<Set<string>>(new Set())
  const [ddlOpen, setDdlOpen] = useState(false)

  const tbl = useMemo(() => (selName ? overview.tables.find((tb) => tb.name === selName) ?? null : null),
    [overview, selName])

  /* 相关表：沿 graph.edges + llm_draft_edges（draft 合并）找与选中表相邻的边 */
  const related = useMemo(() => {
    if (!selName || !overview) return []
    const drafts: GraphEdge[] = (overview.graph.llm_draft_edges ?? []).map((d) => ({
      from: d.from_table, from_col: d.from_col ?? null,
      to: d.to_table, to_col: d.to_col ?? null,
      kind: 'llm', status: 'draft', reason: d.reason,
    }))
    const out: { edge: GraphEdge; other: KbTableView }[] = []
    for (const e of [...(overview.graph.edges ?? []), ...drafts]) {
      if (e.from === selName || e.to === selName) {
        const otherName = e.from === selName ? e.to : e.from
        const other = overview.tables.find((tb) => tb.name === otherName)
        if (other) out.push({ edge: e, other })
      }
    }
    return out
  }, [overview, selName])

  /* 存储形态：进向量（表级 1 chunk） vs 仅原文存库 */
  const store = useMemo(() => {
    if (!tbl) return null
    const vec: { kind: string; text: string }[] = []
    const header = tbl.comment_status === 'confirmed' && tbl.comment ? tbl.comment : tbl.db_comment
    vec.push({ kind: 'title', text: `表标题：${header || t('kb.noDesc')}` })
    let confirmed = 0
    let struct = 0
    for (const c of tbl.columns) {
      if (c.status === 'confirmed') {
        confirmed++
        const bits = [c.name]
        if (c.comment) bits.push(c.comment)
        if (c.values) bits.push(`${t('kb.colValues')} ${c.values}`)
        if (c.example) bits.push(`${t('kb.colExample')} ${c.example}`)
        vec.push({ kind: 'col', text: bits.join(' · ') })
      } else {
        struct++
      }
    }
    if (struct > 0) vec.push({ kind: 'struct', text: t('kb.structShell', { n: struct }) })
    const raw = [
      { text: t('kb.storeDdl') },
      { text: t('kb.storeTags', { n: tbl.tags.length }) },
      { text: t('kb.storeEdges', { n: related.length }) },
    ]
    return { vec, raw, confirmed, struct }
  }, [tbl, related.length, t])

  if (!tbl) {
    return (
      <div className="tdp tdp-empty">
        <div className="tdp-empty-icon">◧</div>
        <div>{t('kb.detailEmpty')}</div>
      </div>
    )
  }

  const toggleRel = (name: string): void => {
    setRelExpanded((s) => {
      const n = new Set(s)
      if (n.has(name)) n.delete(name)
      else n.add(name)
      return n
    })
  }

  return (
    <div className="tdp" key={tbl.name}>
      {/* ① 表信息 */}
      <section className="tdp-sec">
        <div className="tdp-sec-h mono">{t('kb.detailTable')}</div>
        <div className="tdp-tbl-head">
          <span className={`st-dot ${tbl.comment_status}`} />
          <span className="tdp-tname mono">{tbl.name}</span>
          {tbl.kind === 'view' && <span className="tdp-view-badge">view</span>}
          <span className="tdp-tcount mono">{tbl.column_count}</span>
          {tbl.tags.map((tg) => (
            <span key={tg.name} className="tag-chip confirmed">{tg.name}</span>
          ))}
        </div>
        {tbl.db_comment && <div className="tdp-dbcomment mono" title={t('kb.colDbComment')}>{tbl.db_comment}</div>}
        <div className="tdp-comment">
          <span className="tdp-comment-label">{t('kb.detailTableComment')}</span>
          {tbl.comment_status === 'draft' && (
            <span className="mini-acts">
              <button title={t('kb.confirmTitle')} onClick={() => confirmComment(currentId, tbl.name)}>✓</button>
              <button title={t('kb.rejectTitle')} onClick={() => rejectComment(currentId, tbl.name)}>✕</button>
            </span>
          )}
        </div>
        <div className="tdp-comment-body">{tbl.comment || <span className="kb-none">{t('kb.noDesc')}</span>}</div>
        <details className="tdp-ddl" open={ddlOpen} onToggle={(e) => setDdlOpen((e.currentTarget as HTMLDetailsElement).open)}>
          <summary className="mono">{t('kb.detailDdl')}</summary>
          <pre className="tdp-ddl-pre">{tbl.ddl || t('kb.noDdl')}</pre>
        </details>
      </section>

      {/* ② 字段详情 */}
      <section className="tdp-sec">
        <div className="tdp-sec-h mono">
          {t('kb.detailColumns')}
          <span className="tdp-hint">({tbl.columns.length})</span>
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
                {col.status === 'draft' && (
                  <span className="mini-acts">
                    <button title={t('kb.confirmTitle')} onClick={() => confirmComment(currentId, tbl.name, col.name)}>✓</button>
                    <button title={t('kb.rejectTitle')} onClick={() => rejectComment(currentId, tbl.name, col.name)}>✕</button>
                  </span>
                )}
              </div>
              <div className="tdp-col-comment" title={col.db_comment ? `${t('kb.colDbComment')} ${col.db_comment}` : undefined}>
                {col.comment || (col.db_comment || <span className="kb-none">—</span>)}
              </div>
              {(col.values || col.example) && (
                <div className="tdp-col-meta">
                  {col.values && <span className="cvals" title={col.values}>{t('kb.colValues')}: {col.values}</span>}
                  {col.example && <span className="cexample mono" title={col.example}>{t('kb.colExample')} {col.example}</span>}
                </div>
              )}
            </div>
          ))}
          {tbl.columns.length === 0 && <div className="rv-none mono">{t('kb.noColumns')}</div>}
        </div>
      </section>

      {/* ③ 相关表 */}
      <section className="tdp-sec">
        <div className="tdp-sec-h mono">
          {t('kb.detailRelated')}
          <span className="tdp-hint">({related.length})</span>
        </div>
        {related.length === 0 ? (
          <div className="rv-none mono">{t('kb.relatedNone')}</div>
        ) : (
          related.map(({ edge, other }) => {
            const isFrom = edge.from === tbl.name
            const otherName = other.name
            const expanded = relExpanded.has(otherName)
            const draft = edge.kind === 'llm'
            return (
              <div key={`${edge.from}-${edge.to}-${edge.kind}`} className="tdp-rel">
                <div className="tdp-rel-row" onClick={() => toggleRel(otherName)}>
                  <span className="caret">{expanded ? '▾' : '▸'}</span>
                  <span className="mono">{edge.from}</span>
                  {edge.from_col && <span className="rv-edge-col">.{edge.from_col}</span>}
                  <span className="rv-edge-arrow">{isFrom ? '→' : '↔'} </span>
                  <span className="mono">{edge.to}</span>
                  {edge.to_col && <span className="rv-edge-col">.{edge.to_col}</span>}
                  <span className={`tdp-rel-kind ${edge.kind}`}>{edge.kind}</span>
                  {draft && <span className="tdp-rel-draft">{t('kb.draft')}</span>}
                </div>
                {edge.reason && <div className="tdp-rel-reason">{edge.reason}</div>}
                <div className="tdp-rel-desc">{other.db_comment || other.comment || ''}</div>
                {expanded && (
                  <div className="tdp-rel-cols">
                    {other.columns.map((c) => (
                      <div key={c.name} className="tdp-rel-col mono">
                        <span className="cname">{c.name}</span>
                        <span className="ctype">{c.type}</span>
                        <span className="ccomment">{c.comment || c.db_comment || ''}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          })
        )}
      </section>

      {/* ④ 存储形态：哪些进向量 / 哪些仅原文 */}
      <section className="tdp-sec">
        <div className="tdp-sec-h mono">
          {t('kb.detailStorage')}
          {overview.embedding_provider === 'hash' ? (
            <span className="emb-state off" title={t('kb.embUnconfiguredHint')}>{t('kb.embOff')}</span>
          ) : (
            <span className="emb-state on" title={t('kb.embOnTitle')}>{t('kb.embOn')}</span>
          )}
        </div>
        {store && (
          <>
            <div className="tdp-store-group vec">
              <div className="tdp-store-h">{t('kb.storageVector')} <span className="tdp-hint">(1 chunk/表)</span></div>
              {store.vec.map((r, i) => (
                <div key={i} className={`tdp-store-row ${r.kind}`}>
                  <span className="tdp-store-ic">{r.kind === 'title' ? 'H' : r.kind === 'struct' ? 'S' : '⟨⟩'}</span>
                  <span className="tdp-store-text">{r.text}</span>
                </div>
              ))}
              <div className="tdp-store-note">{t('kb.storeVectorNote', { confirmed: store.confirmed, struct: store.struct })}</div>
            </div>
            <div className="tdp-store-group raw">
              <div className="tdp-store-h">{t('kb.storageRaw')}</div>
              {store.raw.map((r, i) => (
                <div key={i} className="tdp-store-row raw">
                  <span className="tdp-store-ic">▤</span>
                  <span className="tdp-store-text">{r.text}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </section>
    </div>
  )
}

/* ═══════════════════════════════════════════════
   知识库主页
   ═══════════════════════════════════════════════ */
export function KnowledgeReview(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { overview, loading, busy, error, load, buildProgress,
    confirmComment, rejectComment, confirmTag, rejectTag,
    assignTags, saveNote } = useKnowledge()
  const { t } = useI18n()
  const openBuildDialog = useKbGate((s) => s.openBuildDialog)
  const buildPct = buildProgress?.percent ?? null

  const [mode, setMode] = useState<'kb' | 'graph'>('kb')
  const [selTags, setSelTags] = useState<Set<string>>(new Set())
  const [selTable, setSelTable] = useState<string | null>(null)
  const [showDraftOnly, setShowDraftOnly] = useState(false)
  const [route, setRoute] = useState<RouteResult | null>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [kq, setKq] = useState('')
  const [kcards, setKcards] = useState<KbCard[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [editText, setEditText] = useState('')
  /** 图库 Tab 形态：display=3D 展示态（只读）| edit=2D 编辑态（拖线/增删边/持久化布局） */
  const [graphMode, setGraphMode] = useState<'display' | 'edit'>('display')
  /** 历史记录抽屉（审计 origin=kb_build 的构建/重建/放弃/启用留痕） */
  const [historyOpen, setHistoryOpen] = useState(false)
  const trg2dActions = useTrg2dActions(currentId)
  const handleLayoutChange = (layout: Record<string, { x: number; y: number }>): void => {
    trg2dActions.onLayoutChange(layout)
    toastMsg('布局已保存')
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
    () => overview?.graph.edges.map((e) => ({ table: e.from, ref_table: e.to })) ?? [],
    [overview?.graph.edges],
  )

  /* 节点颜色映射：标签色驱动，无标签=基准灰，多标签=混色 */
  const nodeColorMap = useMemo(() => {
    if (!overview) return {}
    const m: Record<string, string> = {}
    for (const tb of overview.tables) m[tb.name] = tagColorForTable(tb)
    return m
  }, [overview])

  async function doSearch(): Promise<void> {
    const q = kq.trim()
    if (!q || !currentId) return
    setSearching(true)
    try {
      const r = await retrieve(currentId, q, 10)
      setKcards(r.cards)
    } catch {
      setKcards([])
    } finally {
      setSearching(false)
    }
  }

  useEffect(() => {
    if (currentId) void load(currentId)
  }, [currentId, load])

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
  const untaggedCount = overview?.tables.filter((tb) => tb.tags.length === 0).length ?? 0

  /* 中间列过滤：选中标签（任一命中）∪ 未分类 + 只看待确认 */
  const filteredTables = useMemo(() => {
    if (!overview) return []
    const wanted = [...selTags].filter((n) => n !== UNTAGGED)
    let ts = overview.tables
    if (wanted.length > 0 || selTags.has(UNTAGGED)) {
      ts = ts.filter((tb) => {
        const matchTag = wanted.length === 0 || tb.tags.some((tg) => wanted.includes(tg.name))
        const matchUntagged = selTags.has(UNTAGGED) && tb.tags.length === 0
        return matchTag || matchUntagged
      })
    }
    if (showDraftOnly) {
      ts = ts.filter((tb) => tb.comment_status === 'draft' || tb.columns.some((c) => c.status === 'draft'))
    }
    return ts
  }, [overview, selTags, showDraftOnly])

  const notBuilt = overview !== null && overview.built === false

  function beginEdit(table: string, comment: string): void {
    setEditing(table)
    setEditText(comment)
  }

  return (
    <div className="review kb-page">
      {error && <div className="review-err mono">{error}</div>}

      {notBuilt ? (
        <div className="kb-not-built">
          <div className="kicker">knowledge base</div>
          <div className="kb-nb-title">{t('kb.notBuilt')}</div>
          <div className="kb-nb-text">
            {t('kb.notBuiltDesc')}
          </div>
          <button className="btn save" disabled={busy} onClick={() => openBuildDialog('init')}>
            {busy ? `${t('kb.building', { n: buildPct ?? 0 })}…` : t('kb.build')}
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
              <button type="button" className={`kb-mode-btn${mode === 'graph' ? ' on' : ''}`} onClick={() => setMode('graph')}>
                {t('kb.tabGraph')}
              </button>
            </div>
            {mode === 'kb' && totalPending > 0 && (
              <span className="kb-topbar-pending mono">{t('kb.statsDraft')} {totalPending}</span>
            )}
            {mode === 'graph' && (
              <span className="kb-topbar-pending mono">{t('kb.statsEdges')} {overview.graph.edges.length}</span>
            )}
            <span className="spacer" />
            {overview.synced_at && (
              <span className="kb-synced mono" title={t('kb.syncedTitle')}>
                {t('kb.lastSync')} {overview.synced_at.replace('T', ' ').slice(5, 16)}
              </span>
            )}
            <button className="iconbtn" onClick={() => setHistoryOpen(true)} title="查看知识库历史记录（构建/重建/放弃/启用）">
              历史记录
            </button>
            <button className="iconbtn rebuild-btn" onClick={() => openBuildDialog('rebuild')} disabled={busy}
                    title={t('kb.rebuildTitle')}>
              {busy ? t('kb.building', { n: buildPct ?? 0 }) : t('kb.rebuildAll')}
            </button>
          </div>

          {mode === 'graph' ? (
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
                <div className="kb-trg2d-wrap">
                  {graphMode === 'display' ? (
                    <Graph3D
                      tables={g3dNodes}
                      foreignKeys={g3dEdges}
                      onSelectNode={() => {}}
                      onOpenData={() => {}}
                      colorOverride={nodeColorMap}
                    />
                  ) : (
                    <TableRelationGraph2D
                      tables={trgTables(overview)}
                      edges={trgEdges(overview)}
                      columnsByTable={trgColumns(overview)}
                      layout={overview.graph.layout}
                      mode="edit"
                      colorMap={nodeColorMap}
                      onAddEdge={(e) => trg2dActions.onAddEdge(e)}
                      onDeleteEdge={(e) => trg2dActions.onDeleteEdge(e)}
                      onConfirmEdge={(e) => trg2dActions.onConfirmEdge(e)}
                      onLayoutChange={handleLayoutChange}
                    />
                  )}
                </div>
                {(overview.graph.llm_draft_edges ?? []).length > 0 && (
                  <div className="kb-drafts">
                    <div className="kb-drafts-h mono">{t('kb.kindGraph')} · {t('kb.draft')}</div>
                    <div className="kb-drafts-list">
                      {(overview.graph.llm_draft_edges ?? []).map((edge, idx) => (
                        <div key={`ge-${idx}-${edge.from_table}-${edge.to_table}`} className={`rv-card rv-graph-edge${edge.status === 'previously_rejected' ? ' previously-rejected' : ''}`}>
                          <div className="rv-card-kind rv-kind-graph">{t('kb.kindGraph')}</div>
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
                            <button className="rv-btn-ok" onClick={() => confirmGraphDraftSafe(edge.from_table)} title={edge.status === 'previously_rejected' ? t('kb.graphEdgeRestoreTitle') : t('kb.confirmTitle')}>✓</button>
                            {edge.status !== 'previously_rejected' && (
                              <button className="rv-btn-no" onClick={() => rejectGraphDraftSafe(edge.from_table)}>✕</button>
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
                  {tagRows.length === 0 && <div className="rv-none mono">{t('kb.noConfirmedTags')}</div>}
                  {tagRows.map((tg) => {
                    const on = selTags.has(tg.name)
                    const color = getTagColor(tg.name)
                    return (
                      <div key={tg.name} className={`kb-tag-row${on ? ' on' : ''}`} onClick={() => toggleTag(tg.name)}>
                        <span style={{ width: 9, height: 9, borderRadius: '50%', background: color, flexShrink: 0 }} />
                        <span className="kb-tag-name" style={{ color: tg.status === 'draft' ? 'var(--amber)' : undefined }}>{tg.name}</span>
                        <span className="kb-tag-cnt mono">{tg.count}</span>
                        {tg.status === 'draft' && (
                          <span className="mini-acts" style={{ marginLeft: 'auto' }}>
                            <button title={t('kb.confirmTitle')} onClick={(e) => { e.stopPropagation(); void confirmTag(currentId, tg.name) }}>✓</button>
                            <button title={t('kb.rejectTitle')} onClick={(e) => { e.stopPropagation(); void rejectTag(currentId, tg.name) }}>✕</button>
                          </span>
                        )}
                        {on && <span className="kb-tag-active mono">✓</span>}
                      </div>
                    )
                  })}
                  <div className={`kb-tag-row untagged${selTags.has(UNTAGGED) ? ' on' : ''}`} onClick={() => toggleTag(UNTAGGED)}>
                    <span style={{ width: 9, height: 9, borderRadius: '50%', border: '1.5px dashed var(--ink-faint)', flexShrink: 0 }} />
                    <span className="kb-tag-name" style={{ color: 'var(--ink-dim)' }}>{t('kb.untagged')}</span>
                    <span className="kb-tag-cnt mono">{untaggedCount}</span>
                  </div>
                  {pendingTags.length > 0 && (
                    <div className="kb-pending-tags">
                      <div className="kb-route-cap mono">{t('kb.pendingGroup')}</div>
                      {pendingTags.map((tg) => (
                        <div key={tg.name} className={`kb-tag-row${selTags.has(tg.name) ? ' on' : ''}`} onClick={() => toggleTag(tg.name)}>
                          <span style={{ width: 9, height: 9, borderRadius: '50%', background: getTagColor(tg.name), flexShrink: 0 }} />
                          <span className="kb-tag-name" style={{ color: 'var(--amber)' }}>{tg.name}</span>
                          <span className="mini-acts" style={{ marginLeft: 'auto' }}>
                            <button title={t('kb.confirmTitle')} onClick={(e) => { e.stopPropagation(); void confirmTag(currentId, tg.name) }}>✓</button>
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                  <button className="kb-tag-add-new" onClick={() => {
                    const name = window.prompt('新建标签名称：')
                    if (name?.trim()) void import('@renderer/api/knowledge').then(({ createTag }) => createTag(currentId, name.trim()).then(() => load(currentId)))
                  }}>＋ 新建标签</button>
                </div>
                {selTags.size > 0 && (
                  <div className="kb-route-preview">
                    <div className="kb-route-cap mono">{t('kb.routePreview')} · {t('kb.filtering', { n: selTags.size })}</div>
                    {route ? (
                      <div className="rv-route-result mono">{t('kb.candTables', { n: route.tables.length })}：{route.tables.join(' · ')}</div>
                    ) : (
                      <div className="rv-none mono">{t('kb.routePreviewHint')}</div>
                    )}
                    <button className="kb-clear-filter" onClick={() => setSelTags(new Set())}>✕ {t('kb.clearFilter')}</button>
                  </div>
                )}
              </section>

              {/* ── 中：按表结构聚合 ── */}
              <section className="kb-panel-mid">
                <div className="kb-mid-head">
                  <div className="kb-search">
                    <input
                      className="rs-input"
                      value={kq}
                      placeholder={t('kb.searchDocsPlaceholder')}
                      onChange={(e) => setKq(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') void doSearch() }}
                    />
                    <button className="rs-btn" disabled={searching || !kq.trim()} onClick={() => void doSearch()}>
                      {searching ? t('kb.searching') : t('kb.search')}
                    </button>
                  </div>
                  <button className={`kb-mid-chip${showDraftOnly ? ' on' : ''}`} onClick={() => setShowDraftOnly((v) => !v)}>{t('kb.onlyDraft')}</button>
                  {selTags.size > 0 && (
                    <button className="kb-mid-chip on" onClick={() => setSelTags(new Set())}>{t('kb.filtering', { n: selTags.size })} ✕</button>
                  )}
                  <span className="spacer" />
                  <span className="kb-count mono">{filteredTables.length} 张表</span>
                </div>

                {kcards ? (
                  <div className="review-results">
                    <div className="rr-h mono">
                      {t('kb.searchResults', { n: kcards.length })}
                      <button className="rr-x" onClick={() => setKcards(null)}>✕</button>
                    </div>
                    {kcards.length === 0 ? (
                      <div className="rr-empty mono">{t('kb.noMatch')}</div>
                    ) : (
                      kcards.map((c) => (
                        <div key={c.table} className="rr-item">
                          <span className="rr-kind mono table">table</span>
                          <span className="rr-title mono">{c.table}</span>
                          <span className="rr-body">{c.text}</span>
                          <span className={`rr-status mono ${(c.payload?.draft_count ?? 0) > 0 ? 'draft' : 'confirmed'}`}>
                            {(c.payload?.draft_count ?? 0) > 0 ? t('kb.draft') : t('kb.confirmed')}
                          </span>
                        </div>
                      ))
                    )}
                  </div>
                ) : (
                  <div className="rv-table-list kb-docs">
                    {filteredTables.length === 0 && (
                      <div className="rv-empty mono">{t('kb.noMatch')}</div>
                    )}
                    {filteredTables.map((tbl) => {
                      return (
                        <div key={tbl.name} className="rv-table" data-tname={tbl.name}>
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
                                  <button className="btn save" onClick={() => {
                                    void saveNote(currentId, tbl.name, editText).then(() => setEditing(null))
                                  }}>{t('common.save')}</button>
                                </div>
                              </div>
                            ) : (
                              <>
                                <span className="desc-text">{tbl.comment || <span className="kb-none">{t('kb.noDesc')}</span>}</span>
                                <span className="mini-acts">
                                  <button onClick={() => beginEdit(tbl.name, tbl.comment)} title={t('kb.editNoteTitle')}>✎</button>
                                  {tbl.comment_status === 'draft' && (
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
                              <Tag key={tg.name} name={tg.name} status={tg.status}
                                onConfirm={() => confirmTag(currentId, tg.name)}
                                onReject={() => rejectTag(currentId, tg.name)} />
                            ))}
                            {adding === tbl.name ? (
                              <select
                                autoFocus
                                className="tag-add"
                                value=""
                                onChange={(e) => {
                                  if (e.target.value) void assignTags(currentId, tbl.name, [...tbl.tags.map((x) => x.name), e.target.value])
                                  setAdding(null)
                                }}
                                onBlur={() => setAdding(null)}
                              >
                                <option value="">…</option>
                                {confirmedTags.filter((c) => !tbl.tags.some((x) => x.name === c.name)).map((c) => (
                                  <option key={c.name} value={c.name}>{c.name}</option>
                                ))}
                              </select>
                            ) : (
                              <button className="tag-add-btn" onClick={() => setAdding(tbl.name)}>＋</button>
                            )}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>

              {/* ── 右：详情面板 ── */}
              <section className="kb-panel-detail">
                <TableDetailPanel overview={overview} selName={selTable} currentId={currentId} />
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

      {/* 历史记录抽屉：审计 origin=kb_build 留痕 */}
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