import { useEffect, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useSchema } from '@renderer/store/schema'
import { useResults } from '@renderer/store/results'
import { useUi } from '@renderer/store/ui'
import { runQuery, formatSql } from '@renderer/api/query'
import type { QueryResponse } from '@renderer/api/types'
import {
  chatStream,
  selection,
  setSessionTitle,
  type AiCard,
  type AiEvent,
  type ReportSectionResult,
  type ReasoningEffort
} from '@renderer/api/ai'
import { toastMsg } from '@renderer/utils/toast'
import { useChat, generateTitle, relTime, type TrustLevel, type Turn as ChatTurn, type Conversation } from '@renderer/store/chat'
import { getSettings, type SettingsPublic } from '@renderer/api/settings'

/* ---------- 推理步骤状态机 ---------- */
type StepStatus = string

interface Step {
  id: string
  label: string
  status: StepStatus
  detail: string[]
}

const STEP_DEFS: { id: string; label: string }[] = [
  { id: 'intent', label: '意图分解匹配' },
  { id: 'retrieval', label: '表范围检索' },
  { id: 'sql', label: 'SQL 生成' },
  { id: 'gate', label: '安全评估' }
]

function makeSteps(): Step[] {
  return STEP_DEFS.map((s, i) => ({ ...s, status: i === 0 ? 'running' : ('pending' as StepStatus), detail: [] }))
}

const GATE_LABEL: Record<string, string> = {
  allow: '放行 · 只读',
  review: '需确认 · 写操作',
  block: '拦截'
}

/* 推理步骤：默认收成一行轻量指示，点击展开细节（真实事件驱动，无 mock 播放） */
function ThinkPanel({ steps }: { steps: Step[] }): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const [openStep, setOpenStep] = useState<Set<string>>(new Set())
  const running = steps.some((s) => s.status === 'running')
  const doneCount = steps.filter((s) => s.status === 'done').length

  if (!running && doneCount === 0) return <div className="think-line" />

  const toggleStep = (id: string): void => {
    setOpenStep((prev) => {
      const n = new Set(prev)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  }

  return (
    <div className="think-panel">
      <div className="think-line" onClick={() => setOpen((o) => !o)}>
        {running ? (
          <>
            <span className="spin" />
            <span>思考中…</span>
          </>
        ) : (
          <>
            <span className="ok">✓</span>
            <span>已评估 {doneCount} 步</span>
          </>
        )}
        <span className="spacer" />
        <span className="step-arrow">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="steps">
          {steps.map((s, i) => {
            const isOpen = openStep.has(s.id)
            const first = i === 0
            return (
              <div className={`step ${s.status}`} key={s.id}>
                <div className="step-h" onClick={() => toggleStep(s.id)}>
                  <span className="step-ic">
                    {s.status === 'done' && <span className="ok">✓</span>}
                    {s.status === 'running' && <span className="spin" />}
                    {s.status === 'pending' && <span className="dot" />}
                  </span>
                  <span className="step-label">
                    {first && <span className="tag">AI</span>}
                    {s.label}
                  </span>
                  <span className="step-arrow">▾</span>
                </div>
                <div className={`step-detail${isOpen ? ' open' : ''}`}>
                  {s.detail.length === 0 ? (
                    <div className="sd-empty mono">{s.status === 'running' ? '思考中…' : '—'}</div>
                  ) : (
                    s.detail.map((d, di) => (
                      <div key={di} className="sd-line mono">{d}</div>
                    ))
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

/* ---------- SQL 高亮 ---------- */
function highlightSql(sql: string): string {
  const esc = (s: string): string =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  // 单次交替：注释 | 字符串 | 数字 | 关键字，命中即包 span，避免二次嵌套
  const re =
    /(--[^\n]*)|('[^']*')|(\b\d+(?:\.\d+)?\b)|\b(SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|AS|AND|OR|NOT|IN|BETWEEN|LIKE|IS|NULL|GROUP BY|ORDER BY|LIMIT|OFFSET|DISTINCT|COUNT|SUM|AVG|MIN|MAX|ROUND|ASC|DESC|UPDATE|SET|INSERT|INTO|VALUES|DELETE|CREATE|ALTER|DROP|TABLE|INDEX|PRIMARY|KEY|FOREIGN|REFERENCES|CASE|WHEN|THEN|ELSE|END)\b/gi
  return esc(sql).replace(re, (m, com, str, num) => {
    if (com) return `<span class="k-com">${m}</span>`
    if (str) return `<span class="k-str">${m}</span>`
    if (num) return `<span class="k-num">${m}</span>`
    return `<span class="k-kw">${m}</span>`
  })
}

/* ---------- 卡片徽标 ---------- */
function CardBadge({ card }: { card: AiCard }): React.JSX.Element {
  const m: Record<string, [string, string]> = {
    read: ['allow', 'READ'],
    dml: ['review', 'DML'],
    ddl: ['manual', 'DDL']
  }
  const [cls, label] = m[card.tier] ?? ['manual', card.tier.toUpperCase()]
  const v = card.verdict === 'block' ? 'block' : card.verdict === 'review' ? 'review' : cls
  return <span className={`fst ${v}`}>{label}</span>
}

/* ---------- SQL 编辑器（双层高亮：底层 pre 高亮，上层 textarea 透明可编辑） ---------- */
function SqlEditor({ value, onChange, highlighted }: {
  value: string
  onChange: (v: string) => void
  highlighted: string
}): React.JSX.Element {
  const preRef = useRef<HTMLPreElement>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)

  const syncScroll = (): void => {
    const ta = taRef.current
    const pre = preRef.current
    if (ta && pre) {
      pre.scrollTop = ta.scrollTop
      pre.scrollLeft = ta.scrollLeft
    }
  }

  return (
    <div className="sql-editor">
      <pre
        className="sql-hl"
        aria-hidden="true"
        ref={preRef}
        dangerouslySetInnerHTML={{ __html: highlighted }}
      />
      <textarea
        className="sql-input"
        ref={taRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onScroll={syncScroll}
        spellCheck={false}
        rows={Math.min(12, Math.max(4, value.split('\n').length + 1))}
      />
    </div>
  )
}

/* ---------- SQL 卡片（常驻可编辑编辑器） ---------- */
function SqlCard({ card, question, busy, pending, dialect, onRun, onConfirm, onAnalyze }: {
  card: AiCard
  question: string
  busy: boolean
  /** 安全评估尚未完成：徽标显示"评估中"，按钮禁用 */
  pending: boolean
  dialect: string
  onRun: (sql: string) => void
  onConfirm: (sql: string) => void
  onAnalyze: (kind: 'explain' | 'optimize' | 'risk', sql: string) => void
}): React.JSX.Element {
  const [draft, setDraft] = useState(card.sql)
  const [formatting, setFormatting] = useState(false)

  const cls =
    card.verdict === 'block' ? 'block' : card.verdict === 'review' ? 'review' : card.tier === 'ddl' ? 'manual' : 'allow'
  const blocked = card.verdict === 'block'

  async function handleFormat(): Promise<void> {
    setFormatting(true)
    try {
      const r = await formatSql(draft, dialect)
      setDraft(r.formatted)
    } catch {
      /* 格式化失败静默 */
    } finally {
      setFormatting(false)
    }
  }

  const ANALYZE: { key: 'explain' | 'optimize' | 'risk'; label: string }[] = [
    { key: 'explain', label: '解释' },
    { key: 'optimize', label: '优化' },
    { key: 'risk', label: '风险' }
  ]

  return (
    <div className={`sql ${cls}${pending ? ' pending' : ''}`}>
      <div className="c-h">
        {pending ? <span className="fst eval">评估中</span> : <CardBadge card={card} />}
        <span className="c-question" title={question}>{question}</span>
        <span className="c-acts">
          {!pending && ANALYZE.map((a) => (
            <button key={a.key} className="ca" onClick={() => onAnalyze(a.key, draft)}>{a.label}</button>
          ))}
        </span>
        <span className="st">{card.sub ?? ''}</span>
      </div>

      <SqlEditor
        value={draft}
        onChange={setDraft}
        highlighted={highlightSql(draft)}
      />

      <div className="c-f">
        {card.verdict === 'review' && !card.executed && (
          <div className="risk-panel">
            <div className="rp-title">⚠ 写操作风险评估</div>
            <div className="rp-line mono">影响范围：约 {card.preview_rows ?? '?'} 行</div>
            {card.reason && <div className="rp-line">{card.reason}</div>}
            <div className="rp-line dim">确认时后端将重新评估（防 TOCTOU）· 执行后不可回滚，操作会留痕审计</div>
          </div>
        )}
        {card.executed && (
          <div className="exec-stamp mono">✓ 已执行 · {card.affected ?? 0} 行受影响 · 已写入审计</div>
        )}
        {blocked && <span className="c-p warn">已拦截</span>}
        <span className="spacer" />
        {!pending && !card.executed && (
          <button className="btn gho" disabled={formatting} onClick={() => void handleFormat()}>
            {formatting ? '…' : '格式化'}
          </button>
        )}
        {pending ? (
          <span className="c-p">安全评估中…</span>
        ) : card.executed ? (
          <span className="c-p done">✓ 完成</span>
        ) : card.verdict === 'allow' ? (
          <button className="btn pri" disabled={busy || !draft.trim()} onClick={() => onRun(draft)}>▶ 运行</button>
        ) : card.verdict === 'review' ? (
          <button className="btn warn" disabled={busy || !draft.trim()} onClick={() => onConfirm(draft)}>✓ 确认执行</button>
        ) : blocked ? (
          <span className="c-r">DDL / 拦截 · 不执行</span>
        ) : (
          <span className="c-r">草稿 · 手动执行</span>
        )}
      </div>
    </div>
  )
}

const STEP_MS = 1500      // 每步停留时长

/* ---------- 主组件 ---------- */
export function AiRail({ width, providerName, modelLabel }: { width: number; providerName?: string | null; modelLabel?: string | null }): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connDialect = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.dialect ?? 'sqlite')
  const selectedTable = useSchema((s) => s.selectedTable)
  const selectTable = useSchema((s) => s.selectTable)
  const push = useResults((s) => s.push)
  const setReport = useResults((s) => s.setReport)
  const upsertSection = useResults((s) => s.upsertSection)
  const setNarration = useResults((s) => s.setNarration)
  const { conversations, activeId, newConversation, rename, saveTurns, touch, select, trustLevel, setTrustLevel } = useChat()
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [busy, setBusy] = useState(false)
  const [input, setInput] = useState('')
  const [histOpen, setHistOpen] = useState(false)
  const [sugs, setSugs] = useState<string[]>([])
  const ctxTable = selectedTable // 上下文筹码：当前选中的表
  const [settings, setSettings] = useState<SettingsPublic | null>(null)
  useEffect(() => {
    let alive = true
    void getSettings().then((s) => alive && setSettings(s)).catch(() => undefined)
    return () => { alive = false }
  }, [])
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort>('off')
  // 按对话选模型：null=跟随默认模型（模型在接入侧统一配置）
  const [modelId] = useState<string | null>(null)
  // 图谱"问 AI 这张表"→ 预填输入并聚焦
  const askDraft = useUi((s) => s.askDraft)
  const setAskDraft = useUi((s) => s.setAskDraft)
  useEffect(() => {
    if (askDraft) {
      setInput(askDraft)
      setAskDraft(null)
      requestAnimationFrame(() => inputRef.current?.focus())
    }
  }, [askDraft, setAskDraft])
  // 思考强度控件显隐：以「当前生效模型」(对话级覆盖或默认) 的推理能力为准
  const effectiveModel = settings?.ai_models.find(
    (m) => m.id === (modelId ?? settings.default_ai_model),
  )
  const supportsReasoning = !!effectiveModel?.reasoning
  // 输入框自动增高
  const inputRef = useRef<HTMLTextAreaElement>(null)
  function autoGrow(): void {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(150, el.scrollHeight) + 'px'
  }
  // 澄清挂起：报告中等待用户回答澄清问题时渲染内联输入
  const [clarifyPending, setClarifyPending] = useState<{ q: string; field: string } | null>(null)
  const [clarifyInput, setClarifyInput] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const histRef = useRef<{ role: 'user' | 'assistant'; content: string }[]>([])
  const stepTimers = useRef<ReturnType<typeof setTimeout>[]>([])
  const cardsRef = useRef<AiCard[]>([])
  const loopResultRef = useRef<Record<string, any> | null>(null)
  const userCountRef = useRef(0)
  const firstQRef = useRef('')
  const histDdRef = useRef<HTMLDivElement>(null)
  const streamDoneRef = useRef(false)
  const gateDoneRef = useRef(false)
  const autoRanRef = useRef(false)
  const curQuestionRef = useRef('')

  const activeConv = conversations.find((c) => c.id === activeId) ?? null

  // 点击外部关闭历史对话下拉
  useEffect(() => {
    const onDoc = (e: MouseEvent): void => {
      if (histDdRef.current && !histDdRef.current.contains(e.target as Node)) setHistOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [turns])

  // 数据源切换：恢复该连接的最近对话（无则新建）
  useEffect(() => {
    clearStepTimers()
    setHistOpen(false)
    if (!currentId) {
      setTurns([])
      histRef.current = []
      return
    }
    const { activeId: aid, conversations: convs } = useChat.getState()
    const active = convs.find((c) => c.id === aid)
    if (active && active.connId === currentId) {
      switchTo(active.id, active)
      return
    }
    // 该连接的最近一条对话（按更新时间降序）
    const recent = convs.filter((c) => c.connId === currentId)[0]
    if (recent) {
      switchTo(recent.id, recent)
    } else {
      const id = newConversation(currentId)
      select(id)
      setTurns([])
      histRef.current = []
      userCountRef.current = 0
      firstQRef.current = ''
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentId])

  // 对话变更时持久化轮次
  useEffect(() => {
    if (activeId) saveTurns(activeId, turns)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turns, activeId])

  function switchTo(id: string, conv: Conversation): void {
    clearStepTimers()
    select(id)
    setTurns(conv.turns.map((t) => ({ ...t, running: false })))
    histRef.current = conv.turns
      .filter((t) => t.role === 'user')
      .map((t) => ({ role: 'user' as const, content: t.text ?? '' }))
    userCountRef.current = conv.turns.filter((t) => t.role === 'user').length
    firstQRef.current = conv.turns.find((t) => t.role === 'user')?.text ?? ''
    setHistOpen(false)
  }

  function newChat(): void {
    if (!currentId) return
    clearStepTimers()
    const id = newConversation(currentId)
    select(id)
    setTurns([])
    histRef.current = []
    userCountRef.current = 0
    firstQRef.current = ''
    setHistOpen(false)
    setInput('')
  }

  function clearStepTimers(): void {
    stepTimers.current.forEach((t) => clearTimeout(t))
    stepTimers.current = []
  }

  /** 推进某一步的状态（running/done）。 */
  function setStepStatus(idx: number, status: StepStatus): void {
    setTurns((t) => {
      const n = [...t]
      const last = n[n.length - 1]
      if (last.role !== 'ai' || !last.steps) return n
      last.steps = last.steps.map((s, i) => (i === idx ? { ...s, status } : s))
      return n
    })
  }

  /** 将暂存的卡片挂到当前 turn（安全评估未完成时 pending=true）。
   *  仅当 SQL 生成步已完成（播放器走到）或流已结束时挂载，避免卡片早于步骤出现。 */
  function attachCards(): void {
    const cards = cardsRef.current
    if (cards.length === 0) return
    setTurns((t) => {
      const n = [...t]
      const last = n[n.length - 1]
      if (last.role !== 'ai' || !last.steps) return n
      // 若已挂过同样数量的卡则跳过
      if ((last.cards?.length ?? 0) >= cards.length) return n
      const sqlIdx = STEP_DEFS.length - 2
      const sqlDone = last.steps[sqlIdx]?.status === 'done'
      const gateDone = last.steps[last.steps.length - 1]?.status === 'done'
      if (!sqlDone && !gateDone && last.running) return n   // 播放器还没走到，等
      last.cards = cards.map((c) => ({ ...c }))
      last.pending = !gateDone
      // SQL 生成步（倒数第 2）挂 SQL 首行
      last.steps = last.steps.map((s, i) =>
        i === sqlIdx && !s.detail.some((d) => d.startsWith('生成 SQL'))
          ? { ...s, detail: [...s.detail, `生成 SQL：${cards[cards.length - 1].sql.split('\n')[0]}`] }
          : s
      )
      return n
    })
  }

  /** 自动执行：流结束 + 安全评估完成 + 最后一张卡是读（allow）→ 直接运行出结果。 */
  /** 自动执行：流结束 + 安全评估完成 + 最后一张卡是读（allow）→ 直接运行出结果。 */
  function maybeAutoRun(card: AiCard | undefined): void {
    if (autoRanRef.current) return
    if (!card || card.verdict !== 'allow') return
    if (!streamDoneRef.current || !gateDoneRef.current) return
    if (trustLevel === 'all_confirm') return
    autoRanRef.current = true
    const loopRes = (card as { result?: Record<string, any> }).result
    if (loopRes) {
      loopResultRef.current = null
      handleQueryResult({ ...loopRes, verdict: 'allow' } as QueryResponse, card.sql, curQuestionRef.current)
      return
    }
    void exec(card.sql, false, curQuestionRef.current)
  }

  /** 安全评估完成：取消 pending，挂判定。若流已结束且是读卡 → 自动执行（无需人点）。 */
  function finishGate(): void {
    const card = cardsRef.current[cardsRef.current.length - 1]
    setTurns((t) => {
      const n = [...t]
      const last = n[n.length - 1]
      if (last.role !== 'ai' || !last.steps) return n
      last.pending = false
      const g = last.steps[last.steps.length - 1]
      if (card && !g.detail.some((d) => d.startsWith('判定 →'))) {
        g.detail = [...g.detail, `判定 → ${GATE_LABEL[card.verdict] ?? card.verdict}${card.preview_rows != null ? `（约 ${card.preview_rows} 行）` : ''}`]
      }
      return n
    })
    gateDoneRef.current = true
    maybeAutoRun(card)
  }

  /** 逐步播放：每步 running → 停留 → done → 下一步（detail 由真实 stage/think/sql/gate 事件填充） */
  function playSteps(question: string): void {
    clearStepTimers()
    const sqlIdx = STEP_DEFS.length - 2   // SQL 生成
    const gateIdx = STEP_DEFS.length - 1  // 安全评估

    for (let idx = 0; idx < STEP_DEFS.length; idx++) {
      const stepStart = idx * STEP_MS
      // 步骤开始：mark running（前一步已 done 由前一轮完成）
      stepTimers.current.push(setTimeout(() => setStepStatus(idx, 'running'), stepStart))
      // 步骤完成
      stepTimers.current.push(setTimeout(() => {
        setStepStatus(idx, 'done')
        // SQL 生成完成 → 卡片此刻才出现（安全评估未完成，pending）
        if (idx === sqlIdx) attachCards()
        // 安全评估完成 → 判定落地
        if (idx === gateIdx) finishGate()
      }, stepStart + STEP_MS))
    }
  }

  async function send(inputText?: string, opts?: { mode?: 'query' | 'report' }): Promise<void> {
    const q = (inputText ?? input).trim()
    if (!q || !currentId) return
    const mode = opts?.mode
    setInput('')
    setSugs([])
    if (inputRef.current) inputRef.current.style.height = 'auto'
    setBusy(true)
    const isFirstQ = userCountRef.current === 0
    userCountRef.current += 1
    if (isFirstQ) firstQRef.current = q
    curQuestionRef.current = q
    streamDoneRef.current = false
    gateDoneRef.current = false
    autoRanRef.current = false
    loopResultRef.current = null
    // 上下文联动：当前选中表自动附加到提问（用户已可见上下文筹码，可移除）
    const ctx = ctxTable && !q.includes(`@${ctxTable}`) && !q.includes(ctxTable) ? `（上下文：${ctxTable} 表）` : ''
    histRef.current.push({ role: 'user', content: `${q}${ctx}` })
    const cur = histRef.current
    const reportTitle = q.replace(/[？?。.!！\s]+$/, '').slice(0, 24)
    // 报告模式：turn 标记 isReport，不走 query 模式步骤动画
    if (mode === 'report') {
      setTurns((t) => [...t, { role: 'user', text: q }])
      setTurns((t) => [...t, { role: 'ai', question: q, thinks: [], cards: [], running: true, isReport: true }])
    } else {
      setTurns((t) => [...t, { role: 'user', text: q }])
      setTurns((t) => [...t, { role: 'ai', question: q, thinks: [], cards: [], steps: makeSteps(), running: true }])
    }
    cardsRef.current = []
    if (mode !== 'report') playSteps(q)
    // 提问刷新会话更新时间（查看历史不刷新）
    if (activeId) touch(activeId)

    try {
      await chatStream(
        {
          connection_id: currentId,
          messages: cur.map((m) => ({ role: m.role, content: m.content })),
          table: selectedTable,
          session_id: activeId,
          title: activeConv?.title ?? null,
          mode: mode ?? null,
          reasoning: supportsReasoning ? reasoningEffort : null,
          model_id: modelId ?? undefined,
        },
        (ev: AiEvent) => {
          // ---- 报告模式事件分流 ----
          if (ev.type === 'report_start') {
            setReport({
              reportId: ev.report_id,
              title: reportTitle,
              snapshotTs: ev.snapshot_ts,
              sections: [],
              narration: '',
              refs: []
            })
            return
          }
          if (ev.type === 'clarify') {
            // 澄清：挂起等用户答。把问题挂到当前 turn.clarify + 渲染内联输入
            setClarifyPending({ q: ev.question, field: ev.field })
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.clarify = [...(last.clarify ?? []), ev.question]
              return n
            })
            return
          }
          if (ev.type === 'plan') {
            // 规划完成：在 turn 文案里列章节
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') {
                last.text = `已规划 ${ev.sections.length} 个章节：${ev.sections.map((s) => s.title).join(' / ')}`
              }
              return n
            })
            return
          }
          if (ev.type === 'section') {
            // 章节执行完：流式追加到 results store 的报告视图
            const s: ReportSectionResult = {
              id: ev.id,
              title: ev.title,
              intent: ev.intent,
              result_id: ev.result_id,
              ok: ev.ok,
              reason: ev.reason,
              sql: ev.sql,
              chart: ev.chart,
              rows: ev.rows,
              columns: ev.columns,
              types: ev.types,
              row_count: ev.row_count,
              elapsed_ms: ev.elapsed_ms
            }
            upsertSection(s)
            return
          }
          if (ev.type === 'narration') {
            setNarration(ev.text, ev.refs)
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = '报告已生成，总结与图表见结果区。'
              return n
            })
            toastMsg('报告已生成 · 见结果区')
            return
          }
          if (ev.type === 'report_done') {
            return
          }
          // ---- 查询模式事件（原有） ----
          if (ev.type === 'text') {
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = (last.text ?? '') + ev.content
              return n
            })
          } else if (ev.type === 'think') {
            // 真实 think 事件也挂到步骤详情（与 mock 文案共存）
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') {
                last.thinks = [...(last.thinks ?? []), ev.text]
                if (last.steps) {
                  const cur = last.steps.find((s) => s.status === 'running') ?? last.steps[last.steps.length - 1]
                  cur.detail = [...cur.detail, ev.text]
                }
              }
              return n
            })
          } else if (ev.type === 'stage' && ev.stage === 'intent') {
            // 四步展示：意图分解（真实标签路由结果）
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai' && last.steps) {
                const s = last.steps.find((x) => x.id === 'intent')
                if (s) {
                  s.status = 'done'
                  const v = Array.isArray(ev.value) ? (ev.value as string[]).join(' / ') : String(ev.value ?? '')
                  s.detail = [...s.detail, `意图标签：${v || '无（未命中领域路由）'}`]
                }
              }
              return n
            })
          } else if (ev.type === 'stage' && ev.stage === 'retrieval') {
            // 四步展示：表检索定位（真实候选表清单）
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai' && last.steps) {
                const s = last.steps.find((x) => x.id === 'retrieval')
                const tables = ev.tables ?? []
                if (s) {
                  s.status = 'done'
                  s.detail = [...s.detail, `候选表 ${tables.length} 张：${tables.join(', ') || '（路由未命中，走全量摘要）'}`]
                }
              }
              return n
            })
          } else if (ev.type === 'sql_card') {
            cardsRef.current = [...cardsRef.current, ev.card]
            if (ev.card?.result) loopResultRef.current = ev.card.result
            attachCards()
          } else if (ev.type === 'error') {
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = `⚠ ${ev.message}`
              return n
            })
          } else if (ev.type === 'done') {
            streamDoneRef.current = true
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.running = false
              return n
            })
            // 读卡自动执行（若评估已完成）
            maybeAutoRun(cardsRef.current[cardsRef.current.length - 1])
            // 首问完成 → 异步生成对话标题（替换 ASSISTANT 位置，并回传后端）
            if (isFirstQ && activeId) {
              const convId = activeId
              const q = firstQRef.current
              window.setTimeout(() => {
                const { activeId: aid2, conversations: convs2 } = useChat.getState()
                const c = convs2.find((x) => x.id === aid2)
                // 仅当仍是当前对话且标题未生成时才写入
                if (c && c.id === convId && !c.title) {
                  const t = generateTitle(q)
                  rename(convId, t)
                  void setSessionTitle(convId, t).catch(() => undefined)
                }
              }, 800)
            }
          }
        }
      )
    } catch (e) {
      setTurns((t) => {
        const n = [...t]
        const last = n[n.length - 1]
        if (last.role === 'ai') { last.running = false; last.text = `⚠ ${(e as Error).message}` }
        return n
      })
    } finally {
      setBusy(false)
    }
  }

  /** 澄清回答：把澄清问答作为 system(clarify)+user 消息重传，resume 报告流。 */
  async function answerClarify(): Promise<void> {
    const pending = clarifyPending
    const ans = clarifyInput.trim()
    if (!currentId || !pending || !ans) return
    setClarifyPending(null)
    setClarifyInput('')
    const q = curQuestionRef.current
    // 重传历史：原始问题 → 澄清问 → 本次回答（后端 _extract_clarify_answers 凭 system+name 识别并 replay）
    const msgs: { role: string; content: string; name?: string }[] = [
      { role: 'user', content: q },
      { role: 'system', content: pending.q, name: 'clarify' },
      { role: 'user', content: ans }
    ]
    // 合并进 histRef
    histRef.current.push({ role: 'user', content: pending.q })
    histRef.current.push({ role: 'user', content: ans })
    setBusy(true)
    setTurns((t) => [...t, { role: 'user', text: ans }])
    setTurns((t) => [...t, { role: 'ai', question: q, thinks: [], cards: [], running: true, isReport: true }])
    if (activeId) touch(activeId)
    try {
      await chatStream(
        {
          connection_id: currentId,
          // 保留 name:'clarify'——后端 _extract_clarify_answers 凭 system+name 识别澄清问答并 replay
          messages: msgs.map((m) => ({ role: m.role, content: m.content, name: m.name })),
          table: selectedTable,
          session_id: activeId,
          title: activeConv?.title ?? null,
          mode: 'report'
        },
        (ev: AiEvent) => {
          // 复用 send 的报告事件处理：内联一份精简版（避免回调耦合）
          if (ev.type === 'report_start') {
            setReport({
              reportId: ev.report_id,
              title: q.replace(/[？?。.!！\s]+$/, '').slice(0, 24),
              snapshotTs: ev.snapshot_ts,
              sections: [], narration: '', refs: []
            })
          } else if (ev.type === 'clarify') {
            setClarifyPending({ q: ev.question, field: ev.field })
          } else if (ev.type === 'plan') {
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = `已规划 ${ev.sections.length} 个章节：${ev.sections.map((s) => s.title).join(' / ')}`
              return n
            })
          } else if (ev.type === 'section') {
            const s: ReportSectionResult = {
              id: ev.id, title: ev.title, intent: ev.intent, result_id: ev.result_id,
              ok: ev.ok, reason: ev.reason, sql: ev.sql, chart: ev.chart, rows: ev.rows,
              columns: ev.columns, types: ev.types, row_count: ev.row_count, elapsed_ms: ev.elapsed_ms
            }
            upsertSection(s)
          } else if (ev.type === 'narration') {
            setNarration(ev.text, ev.refs)
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = '报告已生成，总结与图表见结果区。'
              return n
            })
            toastMsg('报告已生成 · 见结果区')
          } else if (ev.type === 'done') {
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.running = false
              return n
            })
          }
        }
      )
    } catch (e) {
      setTurns((t) => {
        const n = [...t]
        const last = n[n.length - 1]
        if (last.role === 'ai') { last.running = false; last.text = `⚠ ${(e as Error).message}` }
        return n
      })
    } finally {
      setBusy(false)
    }
  }

  /** 选中 SQL 解释/优化/风险：结果作为 AI 消息追加（不打断当前步骤流）。 */
  async function analyze(kind: 'explain' | 'optimize' | 'risk', sql: string): Promise<void> {
    if (!currentId) return
    const label = kind === 'explain' ? '解释' : kind === 'optimize' ? '优化' : '风险'
    setTurns((t) => [...t, { role: 'ai', text: `${label}分析中…`, thinks: [`分析 SQL（${label}）`] }])
    try {
      const r = await selection({ connection_id: currentId, sql, kind })
      setTurns((t) => {
        const n = [...t]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = r.text
        return n
      })
    } catch (e) {
      setTurns((t) => {
        const n = [...t]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = `⚠ 分析失败：${(e as Error).message}`
        return n
      })
    }
  }

  async function exec(sql: string, confirm: boolean, question?: string): Promise<void> {
    if (!currentId) return
    setBusy(true)
    try {
      const r = await runQuery({ connectionId: currentId, sql, origin: 'ai', confirm })
      handleQueryResult(r, sql, question)
    } catch (e) {
      setTurns((t) => {
        const n = [...t]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = `⚠ 执行失败：${(e as Error).message}`
        return n
      })
    } finally {
      setBusy(false)
    }
  }

  function handleQueryResult(r: QueryResponse, sql: string, question?: string): void {
    if (r.verdict === 'allow') {
      setSugs(r.suggestions ?? [])
      const types = r.types ?? []
      const numSet = new Set(types.map((t, i) => (t.toLowerCase().includes('int') || t.toLowerCase().includes('real') || t.toLowerCase().includes('numeric') || t.toLowerCase().includes('dec')) ? i : -1).filter((i) => i >= 0))
      const rows = (r.rows ?? []).map((row) => row.map((c, i) => {
        const s = String(c)
        if (!numSet.has(i)) return s
        const n = Number(s)
        // 超出 JS 安全整数范围（或后端已转字符串的超大数）保持字符串，防精度丢失
        return Number.isSafeInteger(n) || (n !== 0 && Math.abs(n) < 1e15) ? n : s
      }))
      push({
        title: question ? `问 · ${question}` : `ai · ${sql.split('\n')[0].slice(0, 42)}`,
        name: sql.slice(0, 20),
        headers: r.columns ?? [],
        types: r.types ?? [],
        rows,
        meta: `${r.truncated ? '截断' : ''}${r.elapsed_ms}ms`.trim()
      })
      toastMsg('结果已发到结果区 · READ')
    } else if (r.verdict === 'review') {
      setTurns((t) => {
        const n = [...t]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = `需确认：将影响约 ${r.preview_rows ?? '?'} 行。点卡片按钮确认执行。`
        return n
      })
    } else if (r.verdict === 'block') {
      setTurns((t) => {
        const n = [...t]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = `已拦截：${r.reason}`
        return n
      })
    } else if (r.verdict === 'executed') {
      toastMsg(`已执行 · ${r.affected_rows ?? 0} 行受影响 · 已写入审计`)
      // 留痕：把最后一张 AI 卡标记为已执行（含影响行数）
      setTurns((t) => {
        const n = [...t]
        for (let i = n.length - 1; i >= 0; i--) {
          const last = n[i]
          if (last.role === 'ai' && last.cards && last.cards.length > 0) {
            const cardsArr = last.cards
            const cards = cardsArr.map((c, ci) =>
              ci === cardsArr.length - 1 ? { ...c, executed: true, affected: r.affected_rows ?? 0 } : c
            )
            n[i] = { ...last, cards }
            break
          }
        }
        return n
      })
    }
  }

  return (
    <aside className="airail" style={{ width }}>
      <div className="airail-head">
        <span className="conv-title" title={activeConv?.title ?? '新对话'}>
          {activeConv?.title ?? '新对话'}
        </span>
        <span className="spacer" />
        <button className="ah-btn" title="新建对话" disabled={busy} onClick={newChat}>＋</button>
        <div className="ah-dd" ref={histDdRef}>
          <button
            className={`ah-btn${histOpen ? ' on' : ''}`}
            title="历史对话"
            onClick={() => setHistOpen((o) => !o)}
          >
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" aria-hidden="true">
              <circle cx="6" cy="6" r="4.6" />
              <path d="M6 3.4 V6 L8.2 7.2" />
            </svg>
          </button>
          {histOpen && (
            <div className="ah-menu">
              <div className="ah-mt mono">历史对话</div>
              {conversations.filter((c) => c.connId === currentId).map((c) => (
                <button
                  key={c.id}
                  className={`ah-mi${c.id === activeId ? ' on' : ''}`}
                  onClick={() => switchTo(c.id, c)}
                >
                  <span className="ah-t">{c.title ?? '未命名对话'}</span>
                  <span className="ah-time mono">{relTime(c.updatedAt)}</span>
                </button>
              ))}
              {conversations.filter((c) => c.connId === currentId).length === 0 && (
                <div className="ah-none mono">暂无历史对话</div>
              )}
            </div>
          )}
        </div>
      </div>

      <div className="airail-scroll" ref={scrollRef}>
        {turns.length === 0 && (
          <div className="airail-empty">
            <div className="e-ic">⌁</div>
            <div className="e-t">ask your database…</div>
            <div className="e-s mono">用一句话描述你想查的数，回车即可</div>
          </div>
        )}

        {turns.map((t, i) =>
          t.role === 'user' ? (
            <div className="m-u" key={i}>{t.text}</div>
          ) : (
            <div className="m-a" key={i}>
              {t.isReport && !t.clarify && (
                <div className={`rpt-pill mono${t.running ? ' live' : ''}`}>
                  <span className="rp-ic">▦</span>报告模式 · {t.text ?? '生成中…'}
                </div>
              )}
              {t.clarify && (
                <div className="clarify-box">
                  <div className="cl-q mono">需要澄清口径（多轮交互）</div>
                  {t.clarify.map((cq, ci) => (
                    <div key={ci} className="cl-line">{cq}</div>
                  ))}
                </div>
              )}
              {t.steps && <ThinkPanel steps={t.steps} />}
              {t.text && !t.isReport && <div className="ai-txt">{t.text}</div>}
              {t.text && t.isReport && t.clarify && null}
              {t.cards && t.cards.map((c, ci) => (
                <SqlCard
                  key={ci}
                  card={c}
                  question={t.question ?? ''}
                  busy={busy}
                  pending={!!t.pending}
                  dialect={connDialect}
                  onRun={(sql) => void exec(sql, false, t.question)}
                  onConfirm={(sql) => void exec(sql, true, t.question)}
                  onAnalyze={(kind, sql) => void analyze(kind, sql)}
                />
              ))}
              {t.running && (
                <div className="think running">
                  <span className="tn">···</span>
                  <span className="tl">
                    <i /><i /><i />
                  </span>
                </div>
              )}
            </div>
          )
        )}
      </div>

      {clarifyPending && (
        <div className="clarify-input-box">
          <div className="cl-prompt mono">需回答才能出报告</div>
          <div className="cl-q-line">{clarifyPending.q}</div>
          <div className="cl-ans">
            <input
              className="a-field"
              value={clarifyInput}
              placeholder="回答这一个问题…"
              onChange={(e) => setClarifyInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void answerClarify() }}
              disabled={busy}
              autoFocus
            />
            <button className="send" disabled={busy || !clarifyInput.trim()} onClick={() => void answerClarify()}>→</button>
          </div>
        </div>
      )}

      <div className="airail-in">
        {ctxTable && (
          <div className="ctx-chips">
            <span className="ctx-chip" title="提问时自动附带该表上下文">
              上下文：{ctxTable}
              <button className="ctx-x" title="移除上下文" onClick={() => selectTable(null)}>✕</button>
            </span>
          </div>
        )}
        {sugs.length > 0 && (
          <div className="sug-strip">
            {sugs.slice(0, 3).map((s, i) => (
              <button key={i} className="sug-chip" disabled={busy} onClick={() => void send(s)} title={s}>
                {s.length > 26 ? `${s.slice(0, 25)}…` : s}
              </button>
            ))}
          </div>
        )}
        <div className="ai-compose">
          <textarea
            ref={inputRef}
            className="compose-input"
            value={input}
            placeholder="问你的数据库…"
            onChange={(e) => { setInput(e.target.value); autoGrow() }}
            onInput={autoGrow}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void send()
              }
            }}
            disabled={busy}
            rows={1}
          />
          <button className="send" disabled={busy || !input.trim()} onClick={() => void send()} title="发送">→</button>
        </div>
        <div className="ai-in-foot">
          <label className="ai-opt" title="会话安全策略">
            <span className="ai-opt-l">安全策略</span>
            <select
              className="ai-sel"
              value={trustLevel}
              onChange={(e) => {
                const v = e.target.value as TrustLevel
                setTrustLevel(v)
                toastMsg(v === 'all_confirm' ? '安全策略：全部人工确认' : '安全策略：读自动 · 写确认')
              }}
            >
              <option value="read_auto">读自动·写确认</option>
              <option value="all_confirm">全部人工确认</option>
            </select>
          </label>
          <button
            type="button"
            className={`ai-toggle${reasoningEffort !== 'off' ? ' on' : ''}`}
            disabled={!supportsReasoning}
            onClick={() => setReasoningEffort(reasoningEffort === 'off' ? 'medium' : 'off')}
            title={supportsReasoning ? '开启思考（仅支持推理的模型可用）' : '当前模型不支持思考'}
          >
            <span className="ai-opt-l">开启思考</span>
            <span className="tg-track"><span className="tg-knob" /></span>
            {!supportsReasoning && <span className="tg-note">不支持</span>}
          </button>
        </div>
      </div>
      <div className="airail-foot">
        <span>provider&nbsp;:&nbsp;<b>{providerName ?? '—'}</b></span>
        <span>模型ID&nbsp;:&nbsp;<b>{modelLabel ?? '—'}</b></span>
      </div>
    </aside>
  )
}
