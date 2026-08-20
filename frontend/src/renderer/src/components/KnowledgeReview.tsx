import { useEffect, useState } from 'react'
import { retrieve, type KbDoc } from '@renderer/api/knowledge'
import type { RouteResult } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKnowledge } from '@renderer/store/knowledge'
import { useI18n } from '@renderer/store/i18n'
import { toastMsg } from '@renderer/utils/toast'
import { GraphEditor } from './GraphEditor'

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

function EnumMeaningInput({ value, onSave }: { value: string; onSave: (v: string) => void }): React.JSX.Element {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(value)
  if (!editing) {
    return (
      <span className="rv-enum-meaning" onClick={() => { setText(value); setEditing(true) }}>
        {value || <span className="kb-none">—</span>}
      </span>
    )
  }
  return (
    <input
      className="rv-enum-meaning-input"
      value={text}
      autoFocus
      onChange={(e) => setText(e.target.value)}
      onBlur={() => { setEditing(false); if (text !== value) onSave(text) }}
      onKeyDown={(e) => { if (e.key === 'Enter') { setEditing(false); if (text !== value) onSave(text) } if (e.key === 'Escape') setEditing(false) }}
    />
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
  const { overview, loading, busy, error, load, buildTask, annotateTags, annotateEnums, confirmComment, rejectComment, confirmTag, rejectTag, confirmEnum, rejectEnum, saveEnum, assignTags, saveNote, confirmAll, addEdge, removeEdge, setExcluded } = useKnowledge()
  const { t } = useI18n()

  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [routeSel, setRouteSel] = useState<Set<string>>(new Set())
  const [route, setRoute] = useState<RouteResult | null>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [tab, setTab] = useState<'docs' | 'tags' | 'review'>(() => {
    const draftCount = overview?.draft_count ?? 0
    const tagDraftCount = overview?.tag_draft_count ?? 0
    const enumDraftCount = overview?.enum_draft_count ?? 0
    return (draftCount + tagDraftCount + enumDraftCount) > 0 ? 'review' : 'docs'
  })
  const [reviewFilter, setReviewFilter] = useState<'all' | 'comment' | 'tag' | 'enum'>('all')
  const [kq, setKq] = useState('')
  const [kdocs, setKdocs] = useState<KbDoc[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [buildPct, setBuildPct] = useState<number | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)
  const [editText, setEditText] = useState('')
  const [confirming, setConfirming] = useState(false)

  async function doSearch(): Promise<void> {
    const q = kq.trim()
    if (!q || !currentId) return
    setSearching(true)
    try {
      const r = await retrieve(currentId, q, 10)
      setKdocs(r.docs)
    } catch {
      setKdocs([])
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

  async function startBuild(): Promise<void> {
    if (!currentId) return
    setBuildPct(0)
    await buildTask(currentId, (p) => setBuildPct(p))
    setBuildPct(null)
  }

  async function doSync(): Promise<void> {
    if (!currentId) return
    setSyncing(true)
    try {
      const { syncKb } = await import('@renderer/api/knowledge')
      const r = await syncKb(currentId)
      if (r.changed) {
        toastMsg(t('kb.syncedToast', { added: r.tables_added, changed: r.tables_changed, removed: r.tables_removed }))
      } else {
        toastMsg(r.message ?? t('kb.noChange'))
      }
      await load(currentId)
    } catch (e) {
      toastMsg(t('kb.syncFail', { msg: (e as Error).message }))
    } finally {
      setSyncing(false)
    }
  }

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
          <button className="btn save" disabled={busy} onClick={() => void startBuild()}>
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
            <button className="iconbtn" onClick={() => void doSync()} disabled={busy} title={t('kb.syncTitle')}>
              {syncing ? t('kb.syncing') : t('kb.checkUpdate')}
            </button>
            <button className="iconbtn" onClick={() => void startBuild()} disabled={busy} title={t('kb.rebuildTitle')}>
              {busy ? t('kb.building', { n: buildPct ?? 0 }) : t('kb.rebuild')}
            </button>
            <button className="iconbtn" onClick={() => currentId && annotateTags(currentId)} disabled={busy}>
              {busy ? t('kb.genBusy') : t('kb.genTags')}
            </button>
            <button className="iconbtn" onClick={() => currentId && annotateEnums(currentId)} disabled={busy}>
              {busy ? t('kb.genBusy') : t('kb.genEnums')}
            </button>
          </div>

          {/* 确认闸横幅：构建完成 → 用户审阅后可一键启用 */}
          {kbStatus === 'pending_review' && (
            <div className="kb-confirm-banner">
              <span className="kb-cb-dot" />
              <span>{t('kb.confirmBanner')}</span>
              <button className="btn save" disabled={confirming} onClick={() => {
                setConfirming(true)
                void confirmAll(currentId).finally(() => setConfirming(false))
              }}>
                {confirming ? t('kb.confirming') : t('kb.confirmAll')}
              </button>
            </div>
          )}

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
              {kdocs && (
                <div className="review-results">
                  <div className="rr-h mono">
                    {t('kb.searchResults', { n: kdocs.length })}
                    <button className="rr-x" onClick={() => setKdocs(null)}>✕</button>
                  </div>
                  {kdocs.length === 0 ? (
                    <div className="rr-empty mono">{t('kb.noMatch')}</div>
                  ) : (
                    kdocs.map((d) => (
                      <div key={d.id} className="rr-item">
                        <span className={`rr-kind mono ${d.kind}`}>{d.kind}</span>
                        <span className="rr-title mono">{d.title}</span>
                        <span className="rr-body">{d.body}</span>
                        <span className={`rr-status mono ${d.status}`}>{d.status === 'confirmed' ? t('kb.confirmed') : t('kb.draft')}</span>
                      </div>
                    ))
                  )}
                </div>
              )}
              <div className="kb-tabs">
                <button className={`kb-tab${tab === 'review' ? ' on' : ''}`} onClick={() => setTab('review')}>
                  {t('kb.reviewTab')}
                  {(overview.draft_count + overview.tag_draft_count + overview.enum_draft_count) > 0 && (
                    <span className="kb-tab-cnt">{overview.draft_count + overview.tag_draft_count + overview.enum_draft_count}</span>
                  )}
                </button>
                <button className={`kb-tab${tab === 'docs' ? ' on' : ''}`} onClick={() => setTab('docs')}>{t('kb.statsDocs')}</button>
                <button className={`kb-tab${tab === 'tags' ? ' on' : ''}`} onClick={() => setTab('tags')}>{t('kb.statsTags')}</button>
              </div>

              {tab === 'review' ? (
                /* ============ 审阅队列（多态：注释/标签/枚举） ============ */
                <div className="kb-review-queue">
                  <div className="rv-filter-bar">
                    {(['all', 'comment', 'tag', 'enum'] as const).map((f) => (
                      <button key={f} className={`rv-filter-btn${reviewFilter === f ? ' on' : ''}`}
                        onClick={() => setReviewFilter(f)}>
                        {t(`kb.filter.${f}`)}
                      </button>
                    ))}
                    <span className="spacer" />
                    <span className="mono" style={{ fontSize: 11, color: 'var(--ink-faint)' }}>
                      {t('kb.remaining', { n: overview.draft_count + overview.tag_draft_count + overview.enum_draft_count })}
                    </span>
                  </div>

                  {/* 无待确认 */}
                  {(overview.draft_count + overview.tag_draft_count + overview.enum_draft_count) === 0 && (
                    <div className="rv-empty">
                      <div className="rv-empty-icon">✓</div>
                      <div>{t('kb.allConfirmed')}</div>
                    </div>
                  )}

                  {/* 注释 draft */}
                  {reviewFilter !== 'tag' && reviewFilter !== 'enum' && overview.tables
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

                  {/* 枚举 draft */}
                  {reviewFilter !== 'comment' && reviewFilter !== 'tag' && (overview.enums ?? [])
                    .filter((e) => e.entries.some((x) => x.status === 'draft'))
                    .map((e) => (
                      <div key={`e-${e.table}-${e.column}`} className="rv-card rv-enum">
                        <div className="rv-card-kind rv-kind-enum">{t('kb.kindEnum')}</div>
                        <div className="rv-card-main">
                          <div className="rv-card-ctx">{t('kb.ctxTable')} <b>{e.table}</b> · {t('kb.ctxColumn')} <b>{e.column}</b></div>
                          <div className="rv-enum-list">
                            {e.entries.filter((x) => x.status === 'draft').map((entry) => (
                              <div key={entry.value} className="rv-enum-row">
                                <span className="rv-enum-val mono">{entry.value}</span>
                                <span className="rv-enum-arrow">→</span>
                                <EnumMeaningInput
                                  value={entry.meaning}
                                  onSave={(meaning) => void saveEnum(currentId, e.table, e.column, entry.value, meaning)}
                                />
                              </div>
                            ))}
                          </div>
                        </div>
                        <div className="rv-card-acts">
                          <button className="rv-btn-ok" onClick={() => void confirmEnum(currentId, e.table, e.column)}>✓</button>
                          <button className="rv-btn-no" onClick={() => void rejectEnum(currentId, e.table, e.column)}>✕</button>
                        </div>
                      </div>
                    ))}

                  {/* 标签 draft */}
                  {reviewFilter !== 'comment' && reviewFilter !== 'enum' && pendingTags.map((tg) => (
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
                </div>

              ) : tab === 'docs' ? (
                <div className="rv-table-list kb-docs">
                   {overview.tables.map((tbl) => (
                    <div key={tbl.name} className="rv-table" data-tname={tbl.name}>
                      <div className="rv-table-row" onClick={() => toggleExpand(tbl.name)}>
                        <span className="caret">{expanded.has(tbl.name) ? '▾' : '▸'}</span>
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
                      {expanded.has(tbl.name) && (
                        <div className="rv-cols">
                          {overview.columns.filter((c) => c.table === tbl.name).map((c) => (
                            <div key={c.name} className="rv-col">
                              <span className="cname mono">{c.name}</span>
                              <span className="ctype mono">{c.type}</span>
                              {c.pk && <span className="ckey mono">PK</span>}
                              {c.fk && <span className="ckey mono">FK</span>}
                              <span className="ccomment">{c.comment || ''}</span>
                              {c.status === 'draft' && (
                                <span className="mini-acts">
                                  <button onClick={() => confirmComment(currentId, c.table, c.name)}>{t('common.confirm')}</button>
                                  <button onClick={() => rejectComment(currentId, c.table, c.name)}>{t('kb.reject')}</button>
                                </span>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
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
                <span>{t('kb.statsDocs')} {overview.tables.length + overview.columns.length}</span>
                <span>{t('kb.statsTags')} {overview.tags.library.length}</span>
                <span>{t('kb.statsEdges')} {overview.graph.edges.length}</span>
                <span className="kb-stats-draft">{t('kb.statsDraft')} {overview.draft_count + overview.tag_draft_count + overview.enum_draft_count}</span>
              </div>
            </section>

            {/* ============ 右：可编辑图结构（58%） ============ */}
            <section className="kb-right">
              <div className="kb-right-cap mono">
                {t('kb.graphCap')}
                <span className="kb-rc-hint">{t('kb.graphHint')}</span>
              </div>
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
            </section>
          </div>
        </>
      ) : null}
    </div>
  )
}
