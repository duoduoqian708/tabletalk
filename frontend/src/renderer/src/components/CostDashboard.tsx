import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend } from 'recharts'
import { USE_MOCK, mockDaily, getMockSessions, getMockCalls, getMockCallDetail, getMockSummary } from './__mocks__/costMock'

/* ── types ── */
interface DailyRow { date: string; calls: number; sessions: number; input_tokens: number; output_tokens: number; total_tokens: number; elapsed_ms_avg: number }
interface SessionRow { session_id: string | null; calls: number; total_tokens: number; input_tokens: number; output_tokens: number; first_ts: string; last_ts: string; skills: string | null; conn_id: string | null }
interface CallRow { id: number; ts: string; conn_id: string | null; skill: string | null; model: string | null; provider: string | null; session_id: string | null; input_tokens: number; output_tokens: number; elapsed_ms: number | null }
interface CallDetail extends CallRow { request_json: unknown; response_json: unknown }
interface Summary { total_calls: number; total_tokens: number; input_tokens: number; output_tokens: number; total_cost_usd: number }

type Period = 'today' | '7d' | '30d'

const API = '/api/v1/cost'

function periodRange(p: Period): { from_ts: string | null; to_ts: string | null } {
  const now = new Date()
  const to = now.toISOString().slice(0, 19)
  if (p === 'today') {
    const from = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString().slice(0, 19)
    return { from_ts: from, to_ts: to }
  }
  const days = p === '7d' ? 7 : 30
  const from = new Date(now.getTime() - days * 86400_000).toISOString().slice(0, 19)
  return { from_ts: from, to_ts: to }
}

const fmt = (n: number) => n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n)
const fmtMs = (ms: number | null) => ms == null ? '-' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
const shortTime = (ts: string) => ts.slice(11, 16) || ts.slice(0, 10)
const h = () => ({ 'X-TableTalk-Token': localStorage.getItem('tt_token') || '' })

const SKILL_COLORS: Record<string, string> = {
  query: 'var(--accent)', write: 'var(--amber)', report: 'var(--blue)',
  preflight: 'var(--violet)', ai_review: 'var(--orange)', embedding: 'var(--green)',
}
const SKILL_BG: Record<string, string> = {
  query: 'var(--accent-dim)', write: 'var(--amber-dim)', report: 'var(--blue-dim)',
  preflight: 'var(--violet-dim)', ai_review: 'var(--orange-dim)', embedding: 'var(--green-dim)',
}

/* ── JSON pretty print ── */
function JsonBlock({ data, label }: { data: unknown; label: string }) {
  if (!data) return <div style={s.jsonEmpty}>无数据</div>
  const str = typeof data === 'string' ? data : JSON.stringify(data, null, 2)
  return (
    <div style={s.jsonBlock}>
      <div style={s.jsonLabel}>{label}</div>
      <pre style={s.jsonPre}>{str}</pre>
    </div>
  )
}

/* ── recharts dual-line chart ── */
const fmtK = (v: number) => v >= 1_000_000 ? `${(v / 1_000_000).toFixed(1)}M` : v >= 1000 ? `${(v / 1000).toFixed(1)}K` : String(v)

function DualLineChart({ data, height = 180 }: { data: DailyRow[]; height?: number }) {
  if (data.length === 0) return null

  const chartData = data.map(d => ({
    date: d.date.slice(5),
    tokens: d.total_tokens,
    sessions: d.sessions,
  }))

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={chartData} margin={{ top: 8, right: 12, left: 0, bottom: 4 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="rgba(160,185,215,0.07)" vertical={false} />
        <XAxis
          dataKey="date"
          tick={{ fontSize: 10, fill: '#5a6a7e', fontFamily: 'var(--font-mono)' }}
          tickLine={false}
          axisLine={{ stroke: 'rgba(160,185,215,0.1)' }}
          interval={data.length > 15 ? Math.ceil(data.length / 8) - 1 : 0}
        />
        <YAxis yAxisId="tok" tick={{ fontSize: 10, fill: '#5a6a7e', fontFamily: 'var(--font-mono)' }}
          tickLine={false} axisLine={false} tickFormatter={fmtK} width={40} />
        <YAxis yAxisId="ses" orientation="right" tick={{ fontSize: 10, fill: '#5a6a7e', fontFamily: 'var(--font-mono)' }}
          tickLine={false} axisLine={false} width={30} />
        <Tooltip
          contentStyle={{
            background: '#131b29', border: '1px solid rgba(160,185,215,0.12)',
            borderRadius: 8, fontSize: 11, color: '#d7e1ec', padding: '8px 12px',
            boxShadow: '0 4px 20px rgba(0,0,0,0.4)',
          }}
          itemStyle={{ color: '#d7e1ec', fontSize: 11 }}
          labelStyle={{ color: '#8fa0b4', fontSize: 10, marginBottom: 4 }}
        />
        <Legend
          iconType="line"
          iconSize={14}
          wrapperStyle={{ fontSize: 10, color: '#8fa0b4', paddingTop: 4 }}
        />
        <Line
          yAxisId="tok" type="monotone" dataKey="tokens" name="Token 总量"
          stroke="#35d99a" strokeWidth={2} dot={{ r: 3, fill: '#0f1520', stroke: '#35d99a', strokeWidth: 1.5 }}
          activeDot={{ r: 5, fill: '#35d99a', stroke: '#0f1520', strokeWidth: 2 }}
        />
        <Line
          yAxisId="ses" type="monotone" dataKey="sessions" name="会话数"
          stroke="#ff6b81" strokeWidth={2} dot={{ r: 3, fill: '#0f1520', stroke: '#ff6b81', strokeWidth: 1.5 }}
          activeDot={{ r: 5, fill: '#ff6b81', stroke: '#0f1520', strokeWidth: 2 }}
        />
      </LineChart>
    </ResponsiveContainer>
  )
}

export function CostDashboard(): React.JSX.Element {
  const [period, setPeriod] = useState<Period>('7d')
  const [daily, setDaily] = useState<DailyRow[]>([])
  const [sessions, setSessions] = useState<SessionRow[]>([])
  const [summary, setSummary] = useState<Summary | null>(null)
  const [selectedSid, setSelectedSid] = useState<string | null>(null)
  const [calls, setCalls] = useState<CallRow[]>([])
  const [selectedCallId, setSelectedCallId] = useState<number | null>(null)
  const [callDetail, setCallDetail] = useState<CallDetail | null>(null)
  const [loadingCalls, setLoadingCalls] = useState(false)
  const [loadingDetail, setLoadingDetail] = useState(false)
  const [search, setSearch] = useState('')
  const selectedCallIdRef = useRef<number | null>(null)

  const range = useMemo(() => periodRange(period), [period])

  const load = useCallback(async () => {
    const days = period === 'today' ? 1 : period === '7d' ? 7 : 30
    if (USE_MOCK) {
      setDaily(mockDaily(days))
      setSessions(getMockSessions())
      setSummary(getMockSummary(days))
      setSelectedSid(null)
      setCalls([])
      setSelectedCallId(null)
      selectedCallIdRef.current = null
      setCallDetail(null)
      return
    }
    const params = new URLSearchParams()
    if (range.from_ts) params.set('from_ts', range.from_ts)
    if (range.to_ts) params.set('to_ts', range.to_ts)
    const qs = params.toString()
    try {
      const [dRes, sRes, sumRes] = await Promise.all([
        fetch(`${API}/daily?${qs}`, { headers: h() }).then(r => r.json()),
        fetch(`${API}/sessions?${qs}`, { headers: h() }).then(r => r.json()),
        fetch(`${API}/summary?${qs}`, { headers: h() }).then(r => r.json()),
      ])
      if (dRes.ok) setDaily(dRes.daily || [])
      if (sRes.ok) setSessions(sRes.sessions || [])
      if (sumRes.ok) setSummary(sumRes)
      setSelectedSid(null)
      setCalls([])
      setSelectedCallId(null)
      selectedCallIdRef.current = null
      setCallDetail(null)
    } catch { /* */ }
  }, [range, period])

  useEffect(() => { void load() }, [load])

  const loadCalls = useCallback(async (sid: string | null) => {
    setSelectedSid(sid)
    setSelectedCallId(null)
    selectedCallIdRef.current = null
    setCallDetail(null)
    if (!sid) { setCalls([]); return }
    if (USE_MOCK) {
      setLoadingCalls(true)
      await new Promise(r => setTimeout(r, 150))
      setCalls(getMockCalls(sid))
      setLoadingCalls(false)
      return
    }
    setLoadingCalls(true)
    try {
      const params = new URLSearchParams()
      if (range.from_ts) params.set('from_ts', range.from_ts)
      if (range.to_ts) params.set('to_ts', range.to_ts)
      const res = await fetch(`${API}/sessions/${encodeURIComponent(sid)}?${params}`, { headers: h() })
      const data = await res.json()
      setCalls(data.ok ? data.calls || [] : [])
    } catch { setCalls([]) }
    setLoadingCalls(false)
  }, [range])

  const toggleCall = useCallback(async (callId: number) => {
    if (selectedCallIdRef.current === callId) {
      selectedCallIdRef.current = null
      setSelectedCallId(null)
      setCallDetail(null)
      return
    }
    selectedCallIdRef.current = callId
    setSelectedCallId(callId)
    if (USE_MOCK) {
      setLoadingDetail(true)
      await new Promise(r => setTimeout(r, 100))
      setCallDetail(getMockCallDetail(callId))
      setLoadingDetail(false)
      return
    }
    setLoadingDetail(true)
    try {
      const res = await fetch(`${API}/calls/${callId}`, { headers: h() })
      const data = await res.json()
      setCallDetail(data.ok ? data.call : null)
    } catch { setCallDetail(null) }
    setLoadingDetail(false)
  }, [])

  const filteredSessions = useMemo(() => {
    if (!search.trim()) return sessions
    const q = search.toLowerCase()
    return sessions.filter(s =>
      (s.session_id || '').toLowerCase().includes(q) ||
      (s.skills || '').toLowerCase().includes(q) ||
      (s.conn_id || '').toLowerCase().includes(q)
    )
  }, [sessions, search])

  const kpis = [
    { label: '调用次数', value: fmt(summary?.total_calls ?? 0), color: 'var(--accent)' },
    { label: '总 Token', value: fmt(summary?.total_tokens ?? 0), color: 'var(--blue)' },
    { label: '输入', value: fmt(summary?.input_tokens ?? 0), color: 'var(--green)' },
    { label: '输出', value: fmt(summary?.output_tokens ?? 0), color: 'var(--violet)' },
  ]

  return (
    <div style={s.root}>
      {/* ── Header: period filter only ── */}
      <div style={s.header}>
        <div style={s.periodBar}>
          {(['today', '7d', '30d'] as Period[]).map(p => (
            <button key={p} onClick={() => setPeriod(p)} style={{
              ...s.periodBtn,
              background: period === p ? 'var(--accent)' : 'transparent',
              color: period === p ? 'var(--accent-ink)' : 'var(--ink-dim)',
            }}>{p === 'today' ? '今天' : p === '7d' ? '1 周' : '1 月'}</button>
          ))}
        </div>
      </div>

      {/* ── Main body: left 60% | right 40% ── */}
      <div style={s.body}>

        {/* ═══ LEFT ═══ */}
        <div style={s.left}>
          {/* KPI strip */}
          <div style={s.kpiGrid}>
            {kpis.map(k => (
              <div key={k.label} style={s.kpiCard}>
                <div style={s.kpiLabel}>{k.label}</div>
                <div style={{ ...s.kpiValue, color: k.color }}>{k.value}</div>
              </div>
            ))}
          </div>

          {/* Line chart */}
          {daily.length > 0 && (
            <div style={s.chartCard}>
              <div style={s.sectionLabel}>每日统计</div>
              <DualLineChart data={daily} height={180} />
            </div>
          )}

          {/* Session list */}
          <div style={s.sessionCard}>
            <div style={s.sessionHeader}>
              <span style={s.sectionLabel}>会话 · {filteredSessions.length}</span>
              <input
                placeholder="搜索会话 ID / 技能..."
                value={search}
                onChange={e => setSearch(e.target.value)}
                style={s.searchInput}
              />
            </div>
            <div style={s.sessionList}>
              {filteredSessions.length === 0 && (
                <div style={s.empty}>暂无数据</div>
              )}
              {filteredSessions.map(ses => {
                const active = ses.session_id === selectedSid
                const skills = ses.skills ? ses.skills.split(', ') : []
                return (
                  <div key={ses.session_id || '__'} onClick={() => loadCalls(ses.session_id)}
                    style={{
                      ...s.sessionRow,
                      background: active ? 'var(--accent-dim)' : 'transparent',
                      borderLeft: active ? '2px solid var(--accent)' : '2px solid transparent',
                    }}
                    onMouseEnter={e => { if (!active) e.currentTarget.style.background = 'var(--void-3)' }}
                    onMouseLeave={e => { if (!active) e.currentTarget.style.background = active ? 'var(--accent-dim)' : 'transparent' }}
                  >
                    <div style={s.sessionRowTop}>
                      <span style={{ ...s.sessionId, color: active ? 'var(--accent-bright)' : 'var(--ink)' }}>
                        {(ses.session_id || '').slice(0, 10)}
                      </span>
                      <span style={s.callBadge}>{ses.calls} 次调用</span>
                    </div>
                    <div style={s.sessionStats}>
                      <span style={{ color: 'var(--green)' }}>输入 {fmt(ses.input_tokens)}</span>
                      <span style={{ color: 'var(--violet)' }}>输出 {fmt(ses.output_tokens)}</span>
                      <span style={{ color: 'var(--blue)' }}>合计 {fmt(ses.total_tokens)}</span>
                    </div>
                    <div style={s.sessionRowMid}>
                      <span>{shortTime(ses.first_ts)} – {shortTime(ses.last_ts)}</span>
                      {ses.conn_id && <span>连接 {ses.conn_id}</span>}
                    </div>
                    {skills.length > 0 && (
                      <div style={s.skillRow}>
                        {skills.map(sk => (
                          <span key={sk} style={{ ...s.skillTag, color: SKILL_COLORS[sk] || 'var(--ink-dim)' }}>{sk}</span>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        </div>

        {/* ═══ RIGHT ═══ */}
        <div style={s.right}>
          {!selectedSid ? (
            <div style={s.emptyFull}>← 选择左侧会话查看 LLM 调用详情</div>
          ) : (
            <div style={s.detailPanel}>
              <div style={s.detailHeader}>
                <span style={s.sectionLabel}>
                  <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent)' }}>{(selectedSid || '').slice(0, 12)}</span>
                  {' · '}{calls.length} 次调用
                </span>
              </div>
              <div style={s.callList}>
                {loadingCalls && (
                  <div style={s.emptyFull}><div style={s.spinner} /></div>
                )}
                {!loadingCalls && calls.length === 0 && (
                  <div style={s.emptyFull}>无 LLM 调用记录</div>
                )}
                {!loadingCalls && calls.map((c, i) => {
                  const isActive = c.id === selectedCallId
                  const skColor = SKILL_COLORS[c.skill || ''] || 'var(--ink-dim)'
                  return (
                    <div key={c.id} onClick={() => toggleCall(c.id)} style={{
                      ...s.callRow,
                      background: isActive ? 'var(--accent-dim)' : 'transparent',
                      borderLeft: isActive ? '2px solid var(--accent)' : '2px solid transparent',
                    }}
                      onMouseEnter={e => { if (!isActive) e.currentTarget.style.background = 'var(--void-3)' }}
                      onMouseLeave={e => { if (!isActive) e.currentTarget.style.background = 'transparent' }}
                    >
                      <div style={s.callRowTop}>
                        <span style={{ ...s.skillBadge, color: skColor, background: SKILL_BG[c.skill || ''] || 'var(--void-4)' }}>
                          {c.skill || '?'}
                        </span>
                        <span style={s.callIdx}>#{i + 1}</span>
                        {c.model && <span style={s.callModel}>{c.model}</span>}
                        <span style={s.callTime}>{shortTime(c.ts)}</span>
                      </div>
                      <div style={s.sessionStats}>
                        <span style={{ color: 'var(--green)' }}>输入 {fmt(c.input_tokens)}</span>
                        <span style={{ color: 'var(--violet)' }}>输出 {fmt(c.output_tokens)}</span>
                        <span style={{ color: 'var(--ink-dim)' }}>合计 {fmt(c.input_tokens + c.output_tokens)}</span>
                        <span style={{ color: 'var(--ink-faint)' }}>耗时 {fmtMs(c.elapsed_ms)}</span>
                      </div>
                      {isActive && (
                        <div style={s.callDetailInline}>
                          {loadingDetail ? (
                            <div style={{ padding: 12, textAlign: 'center' }}><div style={s.spinner} /></div>
                          ) : callDetail ? (
                            <div style={s.jsonSplit}>
                              <JsonBlock data={callDetail.request_json} label="请求" />
                              <JsonBlock data={callDetail.response_json} label="响应" />
                            </div>
                          ) : (
                            <div style={{ padding: 12, color: 'var(--ink-faint)', fontSize: 11 }}>加载失败</div>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/* ── style objects ── */
const s: Record<string, React.CSSProperties> = {
  root: { flex: 1, minWidth: 0, height: '100%', display: 'flex', flexDirection: 'column', overflow: 'hidden' },

  header: { flex: 'none', display: 'flex', alignItems: 'center', padding: '14px 28px 0' },
  periodBar: { display: 'flex', gap: 2, background: 'var(--void-3)', borderRadius: 'var(--r-full)', padding: 3 },
  periodBtn: { padding: '5px 16px', borderRadius: 'var(--r-full)', border: 'none', cursor: 'pointer', fontSize: 12, fontWeight: 500, transition: 'all var(--dur) var(--ease)' },

  body: { flex: 1, display: 'grid', gridTemplateColumns: '3fr 2fr', gap: 12, padding: '16px 28px 28px', overflow: 'hidden', minHeight: 0 },

  left: { display: 'flex', flexDirection: 'column', gap: 10, overflow: 'hidden', minHeight: 0 },
  right: { display: 'flex', flexDirection: 'column', overflow: 'hidden', minHeight: 0 },

  kpiGrid: { display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 8, flex: 'none' },
  kpiCard: { background: 'var(--void-2)', border: '1px solid var(--line)', borderRadius: 'var(--r-md)', padding: '12px 14px', display: 'flex', flexDirection: 'column', gap: 4 },
  kpiLabel: { fontSize: 10, color: 'var(--ink-faint)', letterSpacing: '0.04em', textTransform: 'uppercase' as const },
  kpiValue: { fontSize: 22, fontWeight: 700, fontVariantNumeric: 'tabular-nums', lineHeight: 1 },

  chartCard: { background: 'var(--void-2)', border: '1px solid var(--line)', borderRadius: 'var(--r-md)', padding: '10px 6px 4px', flex: 'none' },
  sectionLabel: { fontSize: 11, fontWeight: 500, color: 'var(--ink-faint)', letterSpacing: '0.04em', textTransform: 'uppercase' as const },

  sessionCard: { flex: 1, background: 'var(--void-2)', border: '1px solid var(--line)', borderRadius: 'var(--r-md)', overflow: 'hidden', display: 'flex', flexDirection: 'column', minHeight: 0 },
  sessionHeader: { padding: '8px 14px', borderBottom: '1px solid var(--line)', display: 'flex', alignItems: 'center', gap: 10, flex: 'none' },
  searchInput: { flex: 1, maxWidth: 200, padding: '4px 10px', fontSize: 11, borderRadius: 'var(--r-full)', border: '1px solid var(--line)', background: 'var(--void-3)', color: 'var(--ink)', outline: 'none' },
  sessionList: { flex: 1, overflow: 'auto', minHeight: 0 },

  sessionRow: { padding: '10px 14px', cursor: 'pointer', transition: 'all var(--dur) var(--ease)', borderBottom: '1px solid var(--line-soft)' },
  sessionRowTop: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 3 },
  sessionId: { fontSize: 12, fontWeight: 500, fontFamily: 'var(--font-mono)', letterSpacing: '0.02em' },
  callBadge: { fontSize: 10, fontWeight: 600, color: 'var(--ink-faint)', background: 'var(--void-4)', padding: '1px 6px', borderRadius: 'var(--r-full)' },
  sessionRowMid: { fontSize: 11, color: 'var(--ink-dim)', display: 'flex', gap: 8, marginBottom: 4 },
  sessionStats: { fontSize: 11, color: 'var(--ink-dim)', display: 'flex', gap: 10, marginBottom: 4 },
  skillRow: { display: 'flex', gap: 4, flexWrap: 'wrap' },
  skillTag: { fontSize: 9, padding: '1px 6px', borderRadius: 'var(--r-full)', background: 'var(--void-4)', fontWeight: 500 },

  empty: { padding: 36, textAlign: 'center', color: 'var(--ink-faint)', fontSize: 12 },
  emptyFull: { flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--ink-faint)', fontSize: 13 },

  detailPanel: { flex: 1, background: 'var(--void-2)', border: '1px solid var(--line)', borderRadius: 'var(--r-md)', overflow: 'hidden', display: 'flex', flexDirection: 'column', minHeight: 0 },
  detailHeader: { padding: '10px 16px', borderBottom: '1px solid var(--line)', flex: 'none' },
  callList: { flex: 1, overflow: 'auto', minHeight: 0 },

  callRow: { padding: '10px 14px', cursor: 'pointer', transition: 'all var(--dur) var(--ease)', borderBottom: '1px solid var(--line-soft)' },
  callRowTop: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 },
  skillBadge: { fontSize: 9, fontWeight: 600, padding: '2px 8px', borderRadius: 'var(--r-full)', letterSpacing: '0.03em' },
  callIdx: { fontSize: 10, color: 'var(--ink-faint)' },
  callTime: { fontSize: 11, color: 'var(--ink-faint)', fontFamily: 'var(--font-mono)', marginLeft: 'auto' },

  callDetailInline: { marginTop: 8, background: 'var(--void-3)', borderRadius: 'var(--r-sm)', overflow: 'hidden' },
  jsonSplit: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 0 },
  jsonBlock: { display: 'flex', flexDirection: 'column' },
  jsonLabel: { fontSize: 9, fontWeight: 600, color: 'var(--ink-faint)', letterSpacing: '0.04em', textTransform: 'uppercase' as const, padding: '6px 10px 2px', background: 'var(--void-4)' },
  jsonPre: { margin: 0, padding: '6px 10px', fontSize: 10, lineHeight: 1.45, fontFamily: 'var(--font-mono)', color: 'var(--ink-dim)', overflow: 'auto', maxHeight: 260, whiteSpace: 'pre-wrap', wordBreak: 'break-all' },
  jsonEmpty: { padding: 12, textAlign: 'center', color: 'var(--ink-faint)', fontSize: 11 },

  spinner: { display: 'inline-block', width: 16, height: 16, border: '2px solid var(--void-5)', borderTopColor: 'var(--accent)', borderRadius: '50%', animation: 'spin .8s linear infinite' },
}
