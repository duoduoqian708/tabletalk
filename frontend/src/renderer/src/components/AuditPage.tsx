import { useEffect, useMemo, useState, Fragment } from 'react'
import { useConnections } from '@renderer/store/connections'
import { listAudit, auditSummary, type AuditQuery, type AuditSummary } from '@renderer/api/audit'
import type { AuditEntry } from '@renderer/api/types'
import { VerdictBadge } from './VerdictBadge'
import { useI18n } from '@renderer/store/i18n'

type View = 'exception' | 'all' | 'report'
type Range = 'today' | '7d' | '30d' | 'all'
type ExFilter = '' | 'block' | 'review' | 'ai_ddl'

const PAGE = 50

const RANGES: { key: Range; labelKey: string }[] = [
  { key: 'today', labelKey: 'audit.rangeToday' },
  { key: '7d', labelKey: 'audit.range7d' },
  { key: '30d', labelKey: 'audit.range30d' },
  { key: 'all', labelKey: 'audit.rangeAll' },
]

function rangeTs(r: Range): { from_ts?: string; to_ts?: string } {
  if (r === 'all') return {}
  const now = new Date()
  const to = now.toISOString().slice(0, 19)
  const start = new Date(now)
  if (r === 'today') start.setHours(0, 0, 0, 0)
  else if (r === '7d') start.setDate(start.getDate() - 7)
  else start.setDate(start.getDate() - 30)
  return { from_ts: start.toISOString().slice(0, 19), to_ts: to }
}

function downloadJsonl(rows: AuditEntry[]): void {
  const text = rows.map((r) => JSON.stringify(r, null, 0)).join('\n')
  const blob = new Blob([text], { type: 'application/x-ndjson' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `audit-${Date.now()}.jsonl`
  a.click()
  URL.revokeObjectURL(url)
}

function StatCard({
  label, value, tone, onClick,
}: { label: string; value: string | number; tone: string; onClick?: () => void }): React.JSX.Element {
  return (
    <button
      type="button"
      className={`stat-card ${tone}`}
      style={{ textAlign: 'left', cursor: onClick ? 'pointer' : 'default' }}
      onClick={onClick}
    >
      <div className="stat-num">{value}</div>
      <div className="stat-lab">{label}</div>
    </button>
  )
}

export function AuditPage(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { t } = useI18n()

  const [view, setView] = useState<View>('exception')
  const [range, setRange] = useState<Range>('all')
  const [summary, setSummary] = useState<AuditSummary | null>(null)
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)

  const [fVerdict, setFVerdict] = useState('')
  const [fOrigin, setFOrigin] = useState('')
  const [fTier, setFTier] = useState('')
  const [sqlSearch, setSqlSearch] = useState('')
  const [exFilter, setExFilter] = useState<ExFilter>('')
  const [expanded, setExpanded] = useState<number | null>(null)

  // 信号区：连接 / 范围变化时聚合
  useEffect(() => {
    if (!currentId) return
    let alive = true
    void auditSummary(connName, rangeTs(range))
      .then((s) => alive && setSummary(s))
      .catch(() => undefined)
    return () => { alive = false }
  }, [currentId, connName, range])

  // 流水：视图 / 过滤 / 范围 / 翻页变化时载入
  useEffect(() => {
    if (!currentId) return
    let alive = true
    setLoading(true); setError(null); setExpanded(null)
    const ts = rangeTs(range)
    void (async () => {
      try {
        let rows: AuditEntry[]
        let cnt: number
        if (view === 'all') {
          const q: AuditQuery = {
            ...ts,
            verdict: fVerdict || undefined,
            origin: fOrigin || undefined,
            tier: fTier || undefined,
            limit: PAGE,
            offset,
          }
          const r = await listAudit(connName, q)
          rows = r.entries; cnt = r.count
        } else {
          // 异常 / 报告：取窗口后客户端过滤、分组、分页
          const r = await listAudit(connName, { ...ts, limit: 1000, offset: 0 })
          rows = r.entries; cnt = r.count
        }
        if (!alive) return
        setEntries(rows); setTotal(cnt)
      } catch (e) {
        if (alive) setError((e as Error).message)
      } finally {
        if (alive) setLoading(false)
      }
    })()
    return () => { alive = false }
  }, [currentId, connName, range, view, fVerdict, fOrigin, fTier, offset])

  // 客户端过滤后的展示集
  const shown = useMemo(() => {
    let list = entries
    if (view === 'exception') {
      list = list.filter((e) => e.verdict === 'block' || e.verdict === 'review')
      if (exFilter === 'block') list = list.filter((e) => e.verdict === 'block')
      else if (exFilter === 'review') list = list.filter((e) => e.verdict === 'review')
      else if (exFilter === 'ai_ddl') list = list.filter((e) => e.origin === 'ai' && e.tier === 'ddl')
    } else if (view === 'all' && sqlSearch.trim()) {
      const q = sqlSearch.trim().toLowerCase()
      list = list.filter((e) => e.sql.toLowerCase().includes(q))
    }
    return list
  }, [entries, view, exFilter, sqlSearch])

  // 报告分组
  const groups = useMemo(() => {
    if (view !== 'report') return []
    const m = new Map<string, AuditEntry[]>()
    for (const e of entries) {
      const key = e.report_id ?? '—'
      if (!m.has(key)) m.set(key, [])
      m.get(key)!.push(e)
    }
    return [...m.entries()].map(([id, rs]) => ({ id, rs }))
  }, [entries, view])

  // 分页（全部流水用后端 count；异常/报告用客户端窗口）
  const pageItems = useMemo(() => {
    if (view === 'all') return shown
    const start = offset
    return shown.slice(start, start + PAGE)
  }, [shown, view, offset])

  const pageTotal = view === 'all' && sqlSearch.trim() ? shown.length : view === 'all' ? total : shown.length
  const pageCount = Math.max(1, Math.ceil(pageTotal / PAGE))
  const curPage = view === 'all' ? Math.floor(offset / PAGE) + 1 : Math.floor(offset / PAGE) + 1

  function resetPage(): void { setOffset(0) }
  function gotoPage(p: number): void {
    const o = (p - 1) * PAGE
    setOffset(Math.min(Math.max(0, o), Math.max(0, pageTotal - 1)))
  }

  function sigCardClick(f: ExFilter): void {
    setView('exception'); setExFilter(f); resetPage()
  }

  const blocked = summary?.by_verdict.block ?? 0
  const review = summary?.by_verdict.review ?? 0
  const aiDdl = summary?.ai_ddl_count ?? 0
  const blockedRate = summary ? Math.round((summary.blocked_rate ?? 0) * 100) : 0
  const aiShare = summary && summary.total > 0
    ? Math.round(((summary.by_origin.ai ?? 0) / summary.total) * 100) : 0

  if (!currentId) {
    return <div className="mpage"><div className="mpage-empty">{t('audit.noConnection')}</div></div>
  }

  return (
    <div className="review kb-page audit-page">
      <div className="audit-head">
        <h1>{t('audit.title')} <span className="db-chip mono">{connName}</span></h1>
        <p>{t('audit.subtitle')}</p>
      </div>

      <div className="au-cols">
        {/* 左：闸门规则（策略区，可滚动参考） */}
        <section className="au-left">
          <div className="au-scroll">
            <p className="rules-note">{t('audit.rulesNote')}</p>
            <div className="tiers">
              <div className="tier read">
                <div className="t-h"><div className="t-ic">⌕</div><div className="t-t">{t('gate.tierRead')}</div><div className="t-st">{t('verdict.allow')}</div></div>
                <div className="t-why">{t('gate.tierReadWhy')}</div>
                <div className="t-kw"><span>SELECT</span><span>SHOW</span><span>EXPLAIN</span><span>PRAGMA</span></div>
                <div className="t-d">{t('gate.tierReadDesc')}</div>
                <div className="t-rule">{t('gate.ruleAutoLimit')}</div>
              </div>
              <div className="tier write">
                <div className="t-h"><div className="t-ic">✎</div><div className="t-t">{t('gate.tierWrite')}</div><div className="t-st">{t('verdict.review')}</div></div>
                <div className="t-why">WRITE · GATE REQUIRED</div>
                <div className="t-kw"><span>INSERT</span><span>UPDATE</span><span>DELETE</span></div>
                <div className="t-d">{t('gate.tierWriteDesc')}</div>
                <div className="t-rule">{t('gate.ruleNoWhere')}</div>
              </div>
              <div className="tier ddl">
                <div className="t-h"><div className="t-ic">▧</div><div className="t-t">{t('gate.tierDdl')}</div><div className="t-st">{t('gate.tierDdlManual')}</div></div>
                <div className="t-why">MANUAL ONLY · AI BLOCKED</div>
                <div className="t-kw"><span>CREATE</span><span>ALTER</span><span>DROP</span><span>TRUNCATE</span></div>
                <div className="t-d">{t('gate.tierDdlDesc')}</div>
                <div className="t-rule">{t('gate.ruleDdlDraft')}</div>
              </div>
            </div>
            <div className="panel">
              <div className="p-h">{t('gate.ruleTitle')}<span className="p-s">app/safety · 纯逻辑 · 单测覆盖</span></div>
              <table className="rule-table">
                <thead><tr><th style={{ width: 210 }}>规则</th><th style={{ width: 90 }}>适用</th><th style={{ width: 80 }}>判定</th><th>说明</th></tr></thead>
                <tbody>
                  <tr><td>{t('gate.ruleReadRow')}</td><td>SELECT</td><td><VerdictBadge v="allow" /></td><td>{t('gate.ruleReadDesc')}</td></tr>
                  <tr><td>{t('gate.ruleNoWhereRow')}</td><td>UPDATE / DELETE</td><td><VerdictBadge v="block" /></td><td>{t('gate.ruleNoWhereDesc')}</td></tr>
                  <tr><td>{t('gate.ruleReviewRow')}</td><td>INSERT / UPDATE / DELETE</td><td><VerdictBadge v="review" /></td><td>{t('gate.ruleReviewDesc')}</td></tr>
                  <tr><td>{t('gate.ruleDdlRow')}</td><td>CREATE / ALTER / DROP / TRUNCATE</td><td><span className="badge manual">MANUAL</span></td><td>{t('gate.ruleDdlDesc')}</td></tr>
                  <tr><td>{t('gate.ruleParseFailRow')}</td><td>任意 SQL</td><td><VerdictBadge v="review" /></td><td>{t('gate.ruleParseFailDesc')}</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>

        {/* 右：审计账本 */}
        <section className="au-right">
          <div className="au-scroll">
      {/* 信号区 */}
      <div className="signal-row">
        <StatCard label={t('audit.blocked')} value={blocked} tone="block" onClick={() => sigCardClick('block')} />
        <StatCard label={t('audit.review')} value={review} tone="review" onClick={() => sigCardClick('review')} />
        <StatCard label={t('audit.aiDdl')} value={aiDdl} tone="ddl" onClick={() => sigCardClick('ai_ddl')} />
        <StatCard label={t('audit.blockRate', { rate: blockedRate })} value={t('audit.aiShare', { share: aiShare })} tone="info" />
      </div>

      {/* 视图切换 + 全局控件 */}
      <div className="audit-bar">
        <div className="seg">
          <button className={`seg-b${view === 'exception' ? ' on' : ''}`} onClick={() => { setView('exception'); resetPage() }}>{t('audit.viewException')}</button>
          <button className={`seg-b${view === 'all' ? ' on' : ''}`} onClick={() => { setView('all'); resetPage() }}>{t('audit.viewAll')}</button>
          <button className={`seg-b${view === 'report' ? ' on' : ''}`} onClick={() => { setView('report'); resetPage() }}>{t('audit.viewReport')}</button>
        </div>
        <div className="spacer" />
        <select className="rs-input" value={range} onChange={(e) => { setRange(e.target.value as Range); resetPage() }}>
          {RANGES.map((r) => <option key={r.key} value={r.key}>{t(r.labelKey)}</option>)}
        </select>
        <button className="rs-btn" onClick={() => downloadJsonl(shown)}>{t('audit.export')}</button>
      </div>

      {/* 过滤条（异常 / 全部流水各有） */}
      {view === 'exception' ? (
        <div className="filter-pills">
          {([['', 'exAll'], ['block', 'audit.blocked'], ['review', 'audit.review'], ['ai_ddl', 'audit.exAiDanger']] as [ExFilter, string][]).map(([k, labKey]) => (
            <button key={k} className={`fp${exFilter === k ? ' on' : ''}`} onClick={() => { setExFilter(k); resetPage() }}>{t(labKey)}</button>
          ))}
        </div>
      ) : view === 'all' ? (
        <div className="audit-filters">
          <select className="rs-input" value={fVerdict} onChange={(e) => { setFVerdict(e.target.value); resetPage() }}>
            <option value="">{t('audit.filterVerdict')}</option>
            <option value="allow">{t('verdict.allow')}</option>
            <option value="review">{t('verdict.review')}</option>
            <option value="block">{t('verdict.block')}</option>
            <option value="executed">{t('verdict.executed')}</option>
          </select>
          <select className="rs-input" value={fOrigin} onChange={(e) => { setFOrigin(e.target.value); resetPage() }}>
            <option value="">{t('audit.filterOrigin')}</option>
            <option value="ai">AI</option>
            <option value="manual">{t('audit.originManual')}</option>
          </select>
          <select className="rs-input" value={fTier} onChange={(e) => { setFTier(e.target.value); resetPage() }}>
            <option value="">{t('audit.filterTier')}</option>
            <option value="read">{t('audit.tierShortRead')}</option>
            <option value="write">{t('audit.tierShortWrite')}</option>
            <option value="ddl">{t('audit.tierShortDdl')}</option>
          </select>
          <input className="rs-input" placeholder={t('audit.searchSql')} value={sqlSearch} onChange={(e) => setSqlSearch(e.target.value)} />
        </div>
      ) : null}

      {/* 状态 */}
      {loading && <div className="mpage-empty mono">{t('common.loading')}</div>}
      {error && <div className="review-err mono">{error}</div>}

      {/* 内容 */}
      {!loading && !error && view === 'report' && (
        <div className="panel">
          {groups.length === 0 && <div className="mpage-empty">{t('audit.noReports')}</div>}
          {groups.map((g) => (
            <div key={g.id} className="rep-group">
              <div className="rep-head">
                <span className="mono">{g.id}</span>
                <span className="rep-n">{t('audit.nRecords', { n: g.rs.length })}</span>
                <span className="rep-mix">{g.rs.slice(0, 6).map((e, i) => <VerdictBadge key={i} v={e.verdict} />)}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {!loading && !error && view !== 'report' && (
        <div className="panel">
          {pageItems.length === 0 && <div className="mpage-empty">{view === 'exception' ? `${t('audit.empty')} 🎉` : t('audit.noRecords')}</div>}
          <table className="audit-table">
            <thead><tr><th>{t('audit.colTime')}</th><th>{t('audit.colVerdict')}</th><th>{t('audit.colTier')}</th><th>{t('audit.colOrigin')}</th><th className="sql">{t('audit.colSql')}</th><th>{t('audit.colDuration')}</th></tr></thead>
            <tbody>
              {pageItems.map((r, i) => {
                const realIdx = view === 'all' ? i : offset + i
                const open = expanded === realIdx
                return (
                  <Fragment key={realIdx}>
                    <tr className={`clickable${open ? ' open' : ''}`} onClick={() => setExpanded(open ? null : realIdx)}>
                      <td className="tme mono">{r.ts}</td>
                      <td><VerdictBadge v={r.verdict} /></td>
                      <td className="ms mono">{r.tier}</td>
                      <td className="ms mono">{r.origin === 'ai' ? 'AI' : t('audit.originManual')}</td>
                      <td className="sql mono">{r.sql}</td>
                      <td className="ms mono">{r.elapsed_ms != null ? `${r.elapsed_ms}ms` : '—'}</td>
                    </tr>
                    {open && (
                      <tr key={`${realIdx}-d`} className="expand-row">
                        <td colSpan={6}>
                          <div className="expand">
                            <div className="exp-sql mono">{r.sql}</div>
                            <div className="exp-meta mono">
                              {t('audit.metaTier')} {r.tier} · {t('audit.metaOrigin')} {r.origin} · {t('audit.metaStatus')} {r.status}
                              {r.report_id ? ` · ${t('audit.reportLabel')} ${r.report_id}` : ''}
                            </div>
                            <div className="exp-note">{t('audit.metaNote')}</div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* 分页 */}
      {!loading && !error && pageTotal > PAGE && (
        <div className="pager">
          <button className="rs-btn" disabled={curPage <= 1} onClick={() => gotoPage(curPage - 1)}>{t('audit.prevPage')}</button>
          <span className="mono">{t('audit.pageInfo', { cur: curPage, total: pageCount })} · {t('audit.totalCount', { n: pageTotal })}</span>
          <button className="rs-btn" disabled={curPage >= pageCount} onClick={() => gotoPage(curPage + 1)}>{t('audit.nextPage')}</button>
        </div>
      )}
          </div>
        </section>
      </div>
    </div>
  )
}
