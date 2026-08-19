import { useEffect, useState } from 'react'
import { routeTables } from '@renderer/api/knowledge'
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

export function KnowledgeReview(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { overview, loading, busy, error, load, build, annotateTags, confirmComment, rejectComment, confirmTag, rejectTag, assignTags } = useKnowledge()

  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [routeSel, setRouteSel] = useState<Set<string>>(new Set())
  const [route, setRoute] = useState<RouteResult | null>(null)
  const [adding, setAdding] = useState<string | null>(null)

  useEffect(() => {
    if (currentId) void load(currentId)
  }, [currentId, load])

  if (!currentId) {
    return (
      <div className="review">
        <div className="review-empty">先连接一个数据源。</div>
      </div>
    )
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

  useEffect(() => {
    if (routeSel.size > 0) {
      void routeTables(currentId, [...routeSel]).then(setRoute).catch(() => setRoute(null))
    } else {
      setRoute(null)
    }
  }, [routeSel, currentId])

  const confirmedTags = overview?.tags.library.filter((t) => t.status === 'confirmed') ?? []
  const pending = overview?.tags.library.filter((t) => t.status === 'draft') ?? []

  return (
    <div className="review">
      {error && <div className="review-err mono">{error}</div>}

      {loading && !overview ? (
        <div className="review-loading mono">加载中…</div>
      ) : overview ? (
        <>
          <div className="review-subbar">
            <span className="rv-title">{connName} · 知识审查</span>
            <span className="rv-counts">
              <span>注释草案 {overview.draft_count}</span>
              <span>标签草案 {overview.tag_draft_count}</span>
            </span>
            {overview.embedding_provider === 'hash' ? (
              <span className="emb-state off" title="在 系统设置 → 大模型接入 → 嵌入模型 配置后，语义检索自动启用">
                嵌入模型未配置 · 仅词面+图谱检索
              </span>
            ) : (
              <span className="emb-state on" title="用户配置的嵌入模型已生效">
                语义嵌入已启用
              </span>
            )}
            <span className="spacer" />
            <button className="iconbtn" onClick={() => currentId && build(currentId)} disabled={busy}>
              {busy ? '构建中…' : '重新构建'}
            </button>
            <button className="iconbtn" onClick={() => currentId && annotateTags(currentId)} disabled={busy}>
              {busy ? '生成中…' : 'AI 生成标签'}
            </button>
          </div>
          <div className="review-cols">
          {/* 左：表 + 列 */}
          <section className="rv-panel">
            <div className="rv-panel-head">表 · {overview.tables.length}</div>
            <div className="rv-table-list">
              {overview.tables.map((t) => (
                <div key={t.name} className="rv-table">
                  <div className="rv-table-row" onClick={() => toggleExpand(t.name)}>
                    <span className="caret">{expanded.has(t.name) ? '▾' : '▸'}</span>
                    <span className="tname mono">{t.name}</span>
                    <span className="tcols mono">{t.column_count}</span>
                  </div>
                  {(t.comment_status === 'draft' || t.comment) && (
                    <div className="rv-desc">
                      <span className={`st-dot ${t.comment_status}`} />
                      <span className="desc-text">{t.comment || '（无描述）'}</span>
                      {t.comment_status === 'draft' && (
                        <span className="mini-acts">
                          <button onClick={() => confirmComment(currentId, t.name)}>确认</button>
                          <button onClick={() => rejectComment(currentId, t.name)}>拒绝</button>
                        </span>
                      )}
                    </div>
                  )}
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
                          {(c.status === 'draft') && (
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
          </section>

          {/* 中：标签库 + 路由预览 */}
          <section className="rv-panel">
            <div className="rv-panel-head">领域标签库</div>
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
              {pending.map((t) => (
                <div key={t.name} className="rv-tagrow">
                  <Tag name={t.name} status="draft"
                    onConfirm={() => confirmTag(currentId, t.name)}
                    onReject={() => rejectTag(currentId, t.name)} />
                  <span className="rv-tagdesc mono">{t.description}</span>
                </div>
              ))}
            </div>

            <div className="rv-panel-head" style={{ marginTop: 16 }}>路由预览</div>
            <div className="rv-route">
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
          </section>

          {/* 右：图谱 */}
          <section className="rv-panel">
            <div className="rv-panel-head">外键图谱</div>
            <KnowledgeGraph overview={overview} highlight={route?.tables} />
          </section>
        </div>
        </>
      ) : null}
    </div>
  )
}
