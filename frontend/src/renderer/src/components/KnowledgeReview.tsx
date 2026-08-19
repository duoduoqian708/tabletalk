import { useEffect, useState } from 'react'
import { retrieve, type KbDoc } from '@renderer/api/knowledge'
import type { RouteResult } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKnowledge } from '@renderer/store/knowledge'
import { KnowledgeGraph } from './KnowledgeGraph'

function Tag({ name, status, onConfirm, onReject }: {
  name: string
  status: string
  onConfirm: () => void
  onReject: () => void
}): React.JSX.Element {
  return (
    <span className={`tag-chip ${status}`}>
      {name}
      {status === 'draft' && (
        <span className="tag-acts">
          <button onClick={onConfirm} title="确认">✓</button>
          <button onClick={onReject} title="拒绝">✕</button>
        </span>
      )}
    </span>
  )
}

const KB_BADGE: Record<string, { text: string; cls: string }> = {
  none: { text: '未构建', cls: 'kb-badge none' },
  building: { text: '构建中', cls: 'kb-badge building' },
  pending_review: { text: '待确认', cls: 'kb-badge pending' },
  ready: { text: '已就绪', cls: 'kb-badge ready' },
}

export function KnowledgeReview(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { overview, loading, busy, error, load, buildTask, annotateTags, confirmComment, rejectComment, confirmTag, rejectTag, assignTags, saveNote, confirmAll } = useKnowledge()

  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [routeSel, setRouteSel] = useState<Set<string>>(new Set())
  const [route, setRoute] = useState<RouteResult | null>(null)
  const [adding, setAdding] = useState<string | null>(null)
  const [tab, setTab] = useState<'docs' | 'tags'>('docs')
  const [kq, setKq] = useState('')
  const [kdocs, setKdocs] = useState<KbDoc[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [buildPct, setBuildPct] = useState<number | null>(null)
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
    return <div className="review"><div className="review-empty">先连接一个数据源。</div></div>
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
          <div className="kb-nb-title">该数据源知识库尚未构建</div>
          <div className="kb-nb-text">
            构建后（含图谱）才能使用此数据源。构建全自动、带进度，完成后可审阅并一键确认启用。
          </div>
          <button className="btn save" disabled={busy} onClick={() => void startBuild()}>
            {busy ? `构建中 ${buildPct ?? 0}%…` : '开始构建'}
          </button>
        </div>
      ) : loading && !overview ? (
        <div className="review-loading mono">加载中…</div>
      ) : overview ? (
        <>
          {/* 顶部：状态 + 操作 */}
          <div className="kb-topbar">
            <span className="mono" style={{ fontWeight: 600, fontSize: 12 }}>{connName} · 知识库</span>
            <span className={badge.cls}>{badge.text}</span>
            {overview.embedding_provider === 'hash' ? (
              <span className="emb-state off" title="在 系统设置 → 大模型接入 → 嵌入模型 配置后，语义检索自动启用">
                嵌入模型未配置 · 仅词面+图谱检索
              </span>
            ) : (
              <span className="emb-state on" title="用户配置的嵌入模型已生效">语义嵌入已启用</span>
            )}
            <span className="spacer" />
            <button className="iconbtn" onClick={() => void startBuild()} disabled={busy} title="重新构建（全自动）">
              {busy ? `构建中 ${buildPct ?? 0}%` : '重新构建'}
            </button>
            <button className="iconbtn" onClick={() => currentId && annotateTags(currentId)} disabled={busy}>
              {busy ? '生成中…' : 'AI 生成标签'}
            </button>
          </div>

          {/* 确认闸横幅：构建完成 → 用户审阅后可一键启用 */}
          {kbStatus === 'pending_review' && (
            <div className="kb-confirm-banner">
              <span className="kb-cb-dot" />
              <span>构建完成 · 请查看左侧内容（可修正），然后确认启用——确认后标签与图谱才参与 AI 路由</span>
              <button className="btn save" disabled={confirming} onClick={() => {
                setConfirming(true)
                void confirmAll(currentId).finally(() => setConfirming(false))
              }}>
                {confirming ? '确认中…' : '一键确认启用'}
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
                  placeholder="检索知识库：输入问题，按语义+图谱召回…"
                  onChange={(e) => setKq(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') void doSearch() }}
                />
                <button className="rs-btn" disabled={searching || !kq.trim()} onClick={() => void doSearch()}>
                  {searching ? '检索中…' : '检索'}
                </button>
              </div>
              {kdocs && (
                <div className="review-results">
                  <div className="rr-h mono">
                    检索结果 · {kdocs.length} 条
                    <button className="rr-x" onClick={() => setKdocs(null)}>✕</button>
                  </div>
                  {kdocs.length === 0 ? (
                    <div className="rr-empty mono">无匹配（未配置嵌入模型时仅词面+图谱邻居检索）</div>
                  ) : (
                    kdocs.map((d) => (
                      <div key={d.id} className="rr-item">
                        <span className={`rr-kind mono ${d.kind}`}>{d.kind}</span>
                        <span className="rr-title mono">{d.title}</span>
                        <span className="rr-body">{d.body}</span>
                        <span className={`rr-status mono ${d.status}`}>{d.status === 'confirmed' ? '已确认' : '草案'}</span>
                      </div>
                    ))
                  )}
                </div>
              )}
              <div className="kb-tabs">
                <button className={`kb-tab${tab === 'docs' ? ' on' : ''}`} onClick={() => setTab('docs')}>文档</button>
                <button className={`kb-tab${tab === 'tags' ? ' on' : ''}`} onClick={() => setTab('tags')}>标签</button>
              </div>

              {tab === 'docs' ? (
                <div className="rv-table-list kb-docs">
                  {overview.tables.map((t) => (
                    <div key={t.name} className="rv-table">
                      <div className="rv-table-row" onClick={() => toggleExpand(t.name)}>
                        <span className="caret">{expanded.has(t.name) ? '▾' : '▸'}</span>
                        <span className="tname mono">{t.name}</span>
                        <span className="tcols mono">{t.column_count}</span>
                        <span className={`st-dot ${t.comment_status}`} />
                      </div>
                      <div className="rv-desc">
                        {editing === t.name ? (
                          <div className="kb-edit">
                            <textarea
                              autoFocus
                              className="kb-edit-ta"
                              value={editText}
                              onChange={(e) => setEditText(e.target.value)}
                              rows={3}
                            />
                            <div className="kb-edit-acts">
                              <button className="btn ghost" onClick={() => setEditing(null)}>取消</button>
                              <button className="btn save" onClick={() => {
                                void saveNote(currentId, t.name, editText).then(() => setEditing(null))
                              }}>保存</button>
                            </div>
                          </div>
                        ) : (
                          <>
                            <span className="desc-text">{t.comment || <span className="kb-none">（无描述，可编辑）</span>}</span>
                            <span className="mini-acts">
                              <button onClick={() => beginEdit(t.name, t.comment)} title="编辑注释">✎</button>
                              {t.comment_status === 'draft' && (
                                <>
                                  <button onClick={() => confirmComment(currentId, t.name)}>确认</button>
                                  <button onClick={() => rejectComment(currentId, t.name)}>拒绝</button>
                                </>
                              )}
                            </span>
                          </>
                        )}
                      </div>
                      <div className="rv-tags">
                        {t.tags.map((tg) => (
                          <Tag key={tg.name} name={tg.name} status={tg.status}
                            onConfirm={() => confirmTag(currentId, tg.name)}
                            onReject={() => rejectTag(currentId, tg.name)} />
                        ))}
                        {adding === t.name ? (
                          <select
                            autoFocus
                            className="tag-add"
                            value=""
                            onChange={(e) => {
                              if (e.target.value) void assignTags(currentId, t.name, [...t.tags.map((x) => x.name), e.target.value])
                              setAdding(null)
                            }}
                            onBlur={() => setAdding(null)}
                          >
                            <option value="">…</option>
                            {confirmedTags.filter((c) => !t.tags.some((x) => x.name === c.name)).map((c) => (
                              <option key={c.name} value={c.name}>{c.name}</option>
                            ))}
                          </select>
                        ) : (
                          <button className="tag-add-btn" onClick={() => setAdding(t.name)}>＋</button>
                        )}
                      </div>
                      {expanded.has(t.name) && (
                        <div className="rv-cols">
                          {overview.columns.filter((c) => c.table === t.name).map((c) => (
                            <div key={c.name} className="rv-col">
                              <span className="cname mono">{c.name}</span>
                              <span className="ctype mono">{c.type}</span>
                              {c.pk && <span className="ckey mono">PK</span>}
                              {c.fk && <span className="ckey mono">FK</span>}
                              <span className="ccomment">{c.comment || ''}</span>
                              {c.status === 'draft' && (
                                <span className="mini-acts">
                                  <button onClick={() => confirmComment(currentId, c.table, c.name)}>确认</button>
                                  <button onClick={() => rejectComment(currentId, c.table, c.name)}>拒绝</button>
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
                  <div className="rv-taggrp-label">已确认（可路由）</div>
                  {confirmedTags.length === 0 && <div className="rv-none mono">尚无确认标签——先确认一些</div>}
                  {confirmedTags.map((t) => (
                    <div key={t.name} className="rv-tagrow">
                      <Tag name={t.name} status="confirmed" onConfirm={() => undefined} onReject={() => rejectTag(currentId, t.name)} />
                      <span className="rv-tagdesc mono">{t.description}</span>
                    </div>
                  ))}
                  <div className="rv-taggrp-label">待确认</div>
                  {pendingTags.map((t) => (
                    <div key={t.name} className="rv-tagrow">
                      <Tag name={t.name} status="draft"
                        onConfirm={() => confirmTag(currentId, t.name)}
                        onReject={() => rejectTag(currentId, t.name)} />
                      <span className="rv-tagdesc mono">{t.description}</span>
                    </div>
                  ))}
                  <div className="rv-taggrp-label" style={{ marginTop: 14 }}>路由预览</div>
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
                    {confirmedTags.length === 0 && <div className="rv-none mono">确认标签后可预览路由</div>}
                  </div>
                  {route && (
                    <div className="rv-route-result mono">
                      候选表 {route.tables.length} 张：{route.tables.join(' · ')}
                    </div>
                  )}
                </div>
              )}

              <div className="kb-stats mono">
                <span>文档 {overview.tables.length + overview.columns.length}</span>
                <span>标签 {overview.tags.library.length}</span>
                <span>关系 {overview.graph.edges.length}</span>
                <span className="kb-stats-draft">待确认 {overview.draft_count + overview.tag_draft_count}</span>
              </div>
            </section>

            {/* ============ 右：图结构预览与编辑（58%） ============ */}
            <section className="kb-right">
              <KnowledgeGraph overview={overview} highlight={route?.tables} />
            </section>
          </div>
        </>
      ) : null}
    </div>
  )
}
