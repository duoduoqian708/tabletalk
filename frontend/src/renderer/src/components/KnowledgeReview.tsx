import { Fragment, useEffect, useState } from 'react'
import { retrieve, type KbCard } from '@renderer/api/knowledge'
import type { RouteResult } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKnowledge } from '@renderer/store/knowledge'
import { useKbGate } from '@renderer/store/kbgate'
import { useI18n } from '@renderer/store/i18n'
import { toastMsg } from '@renderer/utils/toast'
import { getTagColor } from '@renderer/utils/tagColors'
import { GraphEditor } from './GraphEditor'
import { TableRelationGraph2D } from './TableRelationGraph2D'
import { trgColumns, trgEdges, trgTables, useTrg2dActions } from '@renderer/hooks/useTrg2d'

/* ═══════════════════════════════════════════════
   知识库主页（左右布局）
   左：tabs（审阅/文档/标签）+ 搜索 + 列表
   右：关系图谱（GraphEditor 浏览 / TableRelationGraph2D 编辑 双形态切换）
   审核流程在 KbReviewModal（构建完成后弹出的审核弹窗）
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

const KB_BADGE: Record<string, { text: string; cls: string }> = {
  none: { text: 'kb.badgeNone', cls: 'kb-badge none' },
  building: { text: 'kb.badgeBuilding', cls: 'kb-badge building' },
  pending_review: { text: 'kb.badgePending', cls: 'kb-badge pending' },
  ready: { text: 'kb.badgeReady', cls: 'kb-badge ready' },
}

export function KnowledgeReview(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { overview, loading, busy, error, load, buildProgress,
    confirmComment, rejectComment, confirmTag, rejectTag,
    assignTags, saveNote, addEdge, removeEdge, setExcluded } = useKnowledge()
  const { t } = useI18n()
  const openBuildDialog = useKbGate((s) => s.openBuildDialog)
  const buildPct = buildProgress?.percent ?? null

  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [routeSel, setRouteSel] = useState<Set<string>>(new Set())
  const [route, setRoute] = useState<RouteResult | null>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [tab, setTab] = useState<'docs' | 'tags' | 'review'>(() => {
    const draftCount = overview?.draft_count ?? 0
    const tagDraftCount = overview?.tag_draft_count ?? 0
    const graphDraftCount = overview?.graph?.llm_draft_edges?.length ?? 0
    return (draftCount + tagDraftCount + graphDraftCount) > 0 ? 'review' : 'docs'
  })
  const [reviewFilter, setReviewFilter] = useState<'all' | 'comment' | 'tag' | 'graph'>('all')
  const [kq, setKq] = useState('')
  const [kcards, setKcards] = useState<KbCard[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [editText, setEditText] = useState('')
  /** 右栏图谱形态：editor=原 3D 浏览（GraphEditor 不动）| 2d=TableRelationGraph2D 编辑 */
  const [graphMode, setGraphMode] = useState<'editor' | '2d'>('editor')
  const trg2dActions = useTrg2dActions(currentId)

  const totalPending = (overview?.draft_count ?? 0)
    + (overview?.tag_draft_count ?? 0)
    + (overview?.graph?.llm_draft_edges?.length ?? 0)

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
    if (routeSel.size > 0) {
      void import('@renderer/api/knowledge').then(({ routeTables }) =>
        routeTables(currentId ?? '', [...routeSel]).then(setRoute).catch(() => setRoute(null))
      )
    } else {
      setRoute(null)
    }
  }, [routeSel, currentId])

  if (!currentId) {
    return <div className="review"><div className="review-empty">{t('kb.connectFirst')}</div></div>
  }

  const toggleExpand = (t: string): void => {
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(t)) n.delete(t)
      else n.add(t)
      return n
    })
  }

  const toggleRoute = (name: string): void => {
    setRouteSel((s) => {
      const n = new Set(s)
      if (n.has(name)) n.delete(name)
      else n.add(name)
      return n
    })
  }

  const confirmedTags = overview?.tags.library.filter((t) => t.status === 'confirmed') ?? []
  const pendingTags = overview?.tags.library.filter((t) => t.status === 'draft') ?? []
  const kbStatus = overview?.kb_status ?? 'none'
  const badge = KB_BADGE[kbStatus] ?? { text: kbStatus, cls: 'kb-badge' }
  const notBuilt = overview !== null && overview.built === false

  function beginEdit(table: string, comment: string): void {
    setEditing(table)
    setEditText(comment)
  }

  function focusEditNode(name: string): void {
    setExpanded((s) => new Set(s).add(name))
    const t = overview?.tables.find((x) => x.name === name)
    setEditing(name)
    setEditText(t?.comment ?? '')
    requestAnimationFrame(() => {
      const el = document.querySelector(`.rv-table[data-tname="${CSS.escape(name)}"]`)
      el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    })
  }

  async function handleAddEdge(from: string, to: string): Promise<void> {
    if (!currentId) return
    await addEdge(currentId, { from_table: from, to_table: to })
    toastMsg(t('kb.linkedToast', { from, to }))
  }
  async function handleDeleteEdge(edge: { from: string; to: string; kind: string }): Promise<void> {
    if (!currentId) return
    await removeEdge(currentId, { from_table: edge.from, to_table: edge.to, kind: edge.kind })
    toastMsg(t('kb.relDeletedToast', { from: edge.from, to: edge.to }))
  }
  async function handleExclude(name: string): Promise<void> {
    if (!currentId) return
    await setExcluded(currentId, name, true)
    toastMsg(t('kb.excludedToast', { name }))
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
            <span className="mono" style={{ fontWeight: 600, fontSize: 12 }}>{connName} · {t('kb.title')}</span>
            <span className={badge.cls}>{t(badge.text)}</span>
            {overview.embedding_provider === 'hash' ? (
              <span className="emb-state off" title={t('kb.embUnconfiguredHint')}>
                {t('kb.embOff')}
              </span>
            ) : (
              <span className="emb-state on" title={t('kb.embOnTitle')}>{t('kb.embOn')}</span>
            )}
            <span className="spacer" />
            {overview.synced_at && (
              <span className="kb-synced mono" title={t('kb.syncedTitle')}>
                {t('kb.lastSync')} {overview.synced_at.replace('T', ' ').slice(5, 16)}
              </span>
            )}
            <button className="iconbtn" onClick={() => openBuildDialog('rebuild')} disabled={busy}
                    title={t('kb.rebuildTitle')}>
              {busy ? t('kb.building', { n: buildPct ?? 0 }) : t('kb.rebuildAll')}
            </button>
          </div>

          <div className="kb-cols">
            {/* ============ 左：知识库内容区（42%） ============ */}
            <section className="kb-left">
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
              {kcards && (
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
              )}
              <div className="kb-tabs">
                <button className={`kb-tab${tab === 'review' ? ' on' : ''}`} onClick={() => setTab('review')}>
                  {t('kb.reviewTab')}
                  {totalPending > 0 && (
                    <span className="kb-tab-cnt">{totalPending}</span>
                  )}
                </button>
                <button className={`kb-tab${tab === 'docs' ? ' on' : ''}`} onClick={() => setTab('docs')}>{t('kb.statsDocs')}</button>
                <button className={`kb-tab${tab === 'tags' ? ' on' : ''}`} onClick={() => setTab('tags')}>{t('kb.statsTags')}</button>
              </div>

              {tab === 'review' ? (
                /* ============ 审阅队列（多态：注释/标签/关系草案） ============ */
                <div className="kb-review-queue">
                  <div className="rv-filter-bar">
                    {(['all', 'comment', 'tag', 'graph'] as const).map((f) => (
                      <button key={f} className={`rv-filter-btn${reviewFilter === f ? ' on' : ''}`}
                        onClick={() => setReviewFilter(f)}>
                        {t(`kb.filter.${f}`)}
                      </button>
                    ))}
                    <span className="spacer" />
                    <span className="mono" style={{ fontSize: 11, color: 'var(--ink-faint)' }}>
                      {t('kb.remaining', { n: totalPending })}
                    </span>
                  </div>

                  {totalPending === 0 && (
                    <div className="rv-empty">
                      <div className="rv-empty-icon">✓</div>
                      <div>{t('kb.allConfirmed')}</div>
                    </div>
                  )}

                  {reviewFilter !== 'tag' && reviewFilter !== 'graph' && overview.tables
                    .filter((tbl) => tbl.comment_status === 'draft')
                    .map((tbl) => (
                      <div key={`c-${tbl.name}`} className="rv-card rv-comment">
                        <div className="rv-card-kind rv-kind-comment">{t('kb.kindComment')}</div>
                        <div className="rv-card-main">
                          <div className="rv-card-ctx">{t('kb.ctxTable')} <b>{tbl.name}</b></div>
                          <div className="rv-card-body">{tbl.comment}</div>
                        </div>
                        <div className="rv-card-acts">
                          <button className="rv-btn-ok" onClick={() => void confirmComment(currentId, tbl.name)}>✓</button>
                          <button className="rv-btn-no" onClick={() => void rejectComment(currentId, tbl.name)}>✕</button>
                        </div>
                      </div>
                    ))}

                  {reviewFilter !== 'comment' && reviewFilter !== 'tag' && reviewFilter !== 'graph' && pendingTags.map((tg) => (
                    <div key={`t-${tg.name}`} className="rv-card rv-tag">
                      <div className="rv-card-kind rv-kind-tag">{t('kb.kindTag')}</div>
                      <div className="rv-card-main">
                        <div className="rv-card-ctx">{t('kb.ctxTag')}</div>
                        <div className="rv-card-body">
                          <span className="rv-tagname mono">{tg.name}</span>
                          {tg.description && <span className="rv-tagdesc">{tg.description}</span>}
                        </div>
                      </div>
                      <div className="rv-card-acts">
                        <button className="rv-btn-ok" onClick={() => void confirmTag(currentId, tg.name)}>✓</button>
                        <button className="rv-btn-no" onClick={() => void rejectTag(currentId, tg.name)}>✕</button>
                      </div>
                    </div>
                  ))}

                  {reviewFilter !== 'comment' && reviewFilter !== 'tag' && (overview.graph?.llm_draft_edges ?? [])
                    .map((edge, idx) => (
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
                          <button className="rv-btn-ok" onClick={() => void confirmGraphDraftSafe(edge.from_table)} title={edge.status === 'previously_rejected' ? t('kb.graphEdgeRestoreTitle') : t('kb.confirmTitle')}>✓</button>
                          {edge.status !== 'previously_rejected' && (
                            <button className="rv-btn-no" onClick={() => void rejectGraphDraftSafe(edge.from_table)}>✕</button>
                          )}
                        </div>
                      </div>
                    ))}
                </div>

              ) : tab === 'docs' ? (
                /* ============ 按表内容块（v2：表头 + 字段行，含可选值/示例） ============ */
                <div className="rv-table-list kb-docs">
                   {overview.tables.map((tbl) => {
                    const draftCols = tbl.columns.filter((c) => c.status === 'draft').length
                    const draftTotal = draftCols + (tbl.comment_status === 'draft' ? 1 : 0)
                    return (
                    <div key={tbl.name} className="rv-table" data-tname={tbl.name}>
                      <div className="rv-table-row" onClick={() => toggleExpand(tbl.name)}>
                        <span className="caret">{expanded.has(tbl.name) ? '▾' : '▸'}</span>
                        <span className="tname mono">{tbl.name}</span>
                        {draftTotal > 0 && (
                          <span className="rv-draft-cnt mono" title={t('kb.statsDraft')}>{draftTotal}</span>
                        )}
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
                      {expanded.has(tbl.name) && (
                        <div className="rv-cols">
                          {tbl.columns.map((c) => (
                            <Fragment key={c.name}>
                              <div className={`rv-col ${c.status}`}>
                                <span className={`st-dot ${c.status}`} />
                                <span className="cname mono">{c.name}</span>
                                <span className="ctype mono">{c.type}</span>
                                {c.pk && <span className="ckey mono">PK</span>}
                                {c.fk && <span className="ckey fk mono">FK</span>}
                                <span className="ccomment" title={c.db_comment ? `${t('kb.colDbComment')} ${c.db_comment}` : undefined}>
                                  {c.comment || ''}
                                </span>
                                {c.status === 'draft' && (
                                  <span className="mini-acts">
                                    <button title={t('kb.confirmTitle')} onClick={() => confirmComment(currentId, tbl.name, c.name)}>✓</button>
                                    <button title={t('kb.rejectTitle')} onClick={() => rejectComment(currentId, tbl.name, c.name)}>✕</button>
                                  </span>
                                )}
                              </div>
                              {(c.values || c.example) && (
                                <div className="rv-col-meta">
                                  {c.values && <span className="cvals" title={c.values}>{t('kb.colValues')}：{c.values}</span>}
                                  {c.example && <span className="cexample mono" title={c.example}>{t('kb.colExample')} {c.example}</span>}
                                </div>
                              )}
                            </Fragment>
                          ))}
                          {draftCols > 0 && <div className="rv-cols-hint">{t('kb.colRejectHint')}</div>}
                        </div>
                      )}
                    </div>
                    )
                  })}
                </div>
              ) : (
                <div className="rv-taglib">
                  <div className="rv-taggrp-label">{t('kb.confirmedGroup')}</div>
                  {confirmedTags.length === 0 && <div className="rv-none mono">{t('kb.noConfirmedTags')}</div>}
                  {confirmedTags.map((t) => (
                    <div key={t.name} className="rv-tagrow">
                      <Tag name={t.name} status="confirmed" onConfirm={() => undefined} onReject={() => rejectTag(currentId, t.name)} />
                      <span className="rv-tagdesc mono">{t.description}</span>
                    </div>
                  ))}
                  <div className="rv-taggrp-label">{t('kb.pendingGroup')}</div>
                  {pendingTags.map((t) => (
                    <div key={t.name} className="rv-tagrow">
                      <Tag name={t.name} status="draft"
                        onConfirm={() => confirmTag(currentId, t.name)}
                        onReject={() => rejectTag(currentId, t.name)} />
                      <span className="rv-tagdesc mono">{t.description}</span>
                    </div>
                  ))}
                  <div className="rv-taggrp-label" style={{ marginTop: 14 }}>{t('kb.routePreview')}</div>
                  <div className="rv-route-tags">
                    {confirmedTags.map((t) => (
                      <button
                        key={t.name}
                        className={`route-chip${routeSel.has(t.name) ? ' on' : ''}`}
                        onClick={() => toggleRoute(t.name)}
                      >
                        {t.name}
                      </button>
                    ))}
                    {confirmedTags.length === 0 && <div className="rv-none mono">{t('kb.routePreviewHint')}</div>}
                  </div>
                  {route && (
                    <div className="rv-route-result mono">
                      {t('kb.candTables', { n: route.tables.length })}：{route.tables.join(' · ')}
                    </div>
                  )}
                </div>
              )}

              <div className="kb-stats mono">
                <span>{t('kb.statsDocs')} {overview.tables.length + overview.tables.reduce((n, tb) => n + tb.columns.length, 0)}</span>
                <span>{t('kb.statsTags')} {overview.tags.library.length}</span>
                <span>{t('kb.statsEdges')} {overview.graph.edges.length}</span>
                <span className="kb-stats-draft">{t('kb.statsDraft')} {totalPending}</span>
              </div>
            </section>

            {/* ============ 右：关系图谱（58%，浏览/2D 编辑双形态） ============ */}
            <section className="kb-right">
              <div className="kb-right-cap mono">
                {t('kb.graphCap')}
                <span className="kb-rc-hint">{t('kb.graphHint')}</span>
                <span className="spacer" />
                <span className="kb-mode-toggle" role="tablist">
                  <button
                    type="button"
                    className={`kb-mode-btn${graphMode === 'editor' ? ' on' : ''}`}
                    onClick={() => setGraphMode('editor')}
                  >
                    {t('kb.graphModeBrowse')}
                  </button>
                  <button
                    type="button"
                    className={`kb-mode-btn${graphMode === '2d' ? ' on' : ''}`}
                    onClick={() => setGraphMode('2d')}
                  >
                    {t('kb.graphModeEdit2d')}
                  </button>
                </span>
              </div>
              {graphMode === '2d' ? (
                <div className="kb-trg2d-wrap">
                  <TableRelationGraph2D
                    tables={trgTables(overview)}
                    edges={trgEdges(overview)}
                    columnsByTable={trgColumns(overview)}
                    layout={overview.graph.layout}
                    getTagColor={(tag) => getTagColor(tag)}
                    onAddEdge={(e) => trg2dActions.onAddEdge(e)}
                    onDeleteEdge={(e) => trg2dActions.onDeleteEdge(e)}
                    onConfirmEdge={(e) => trg2dActions.onConfirmEdge(e)}
                    onLayoutChange={trg2dActions.onLayoutChange}
                  />
                </div>
              ) : (
                <GraphEditor
                  tables={overview.tables.map((t) => ({
                    name: t.name,
                    row_count: t.column_count,
                    column_count: t.column_count,
                  }))}
                  edges={overview.graph.edges}
                  excluded={overview.graph.excluded ?? []}
                  onAddEdge={(from, to) => void handleAddEdge(from, to)}
                  onDeleteEdge={(e) => void handleDeleteEdge(e)}
                  onExcludeNode={(name) => void handleExclude(name)}
                  onEditNode={(name) => focusEditNode(name)}
                />
              )}
            </section>
          </div>
        </>
      ) : null}
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
