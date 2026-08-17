import { useEffect, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useSchema } from '@renderer/store/schema'
import { useResults } from '@renderer/store/results'
import { runQuery, formatSql } from '@renderer/api/query'
import type { QueryResponse } from '@renderer/api/types'
import {
  chatStream,
  selection,
  setSessionTitle,
  type AiCard,
  type AiEvent,
  type ReportSectionResult
} from '@renderer/api/ai'
import { toastMsg } from '@renderer/utils/toast'
import { useChat, generateTitle, relTime, type Turn as ChatTurn, type Conversation } from '@renderer/store/chat'
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

/* 推理步骤面板：每步 勾/转圈/待定，可展开看思考输出 */
function StepsPanel({ steps }: { steps: Step[] }): React.JSX.Element {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (id: string): void => {
    setOpen((prev) => {
      const n = new Set(prev)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  }

  return (
    <div className="steps">
      {steps.map((s, i) => {
        const isOpen = open.has(s.id)
        const first = i === 0
        return (
          <div className={`step ${s.status}`} key={s.id}>
            <div className="step-h" onClick={() => toggle(s.id)}>
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
        {card.verdict === 'review' && <span className="c-p">将影响约 {card.preview_rows ?? '?'} 行</span>}
        {blocked && <span className="c-p warn">已拦截</span>}
        <span className="spacer" />
        {!pending && (
          <button className="btn gho" disabled={formatting} onClick={() => void handleFormat()}>
            {formatting ? '…' : '格式化'}
          </button>
        )}
        {pending ? (
          <span className="c-p">安全评估中…</span>
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

/* ---------- 思考文案（按问题类型，模拟真实推理逐行输出） ---------- */
type ThinkMode = 'returns' | 'dml' | 'delete' | 'ddl' | 'clv' | 'inventory' | 'default'

function thinkMode(q: string): ThinkMode {
  const s = q.toLowerCase()
  if (/退货|退款|return/.test(s)) return 'returns'
  if (/提价|涨价|price|更新|改价/.test(s)) return 'dml'
  if (/删|delete/.test(s)) return 'delete'
  if (/索引|建表|删表|alter|create|ddl/.test(s)) return 'ddl'
  if (/客户|clv|价值|revenue/.test(s)) return 'clv'
  if (/库存|积压|周转/.test(s)) return 'inventory'
  return 'default'
}

const THINK_LINES: Record<string, Record<ThinkMode, string[]>> = {
  intent: {
    returns: ['理解问题意图：统计商品退货表现', '意图分类 → 聚合分析查询', '匹配领域标签：商品、订单'],
    dml: ['理解问题意图：数据修改', '意图分类 → 写操作', '匹配领域标签：商品'],
    delete: ['理解问题意图：删除数据', '意图分类 → 高风险写操作', '匹配领域标签：订单'],
    ddl: ['理解问题意图：结构变更', '意图分类 → DDL 草稿', '匹配领域标签：—'],
    clv: ['理解问题意图：客户价值分析', '意图分类 → 聚合分析查询', '匹配领域标签：客户'],
    inventory: ['理解问题意图：库存分析', '意图分类 → 聚合分析查询', '匹配领域标签：库存'],
    default: ['理解问题意图：数据查询', '意图分类 → 只读查询', '匹配领域标签：—']
  },
  retrieval: {
    returns: ['检索打标表：products、order_items', 'FK 扩展 → orders、returns', '候选子图 4 张表'],
    dml: ['检索打标表：products', '单表更新，无子图扩展', '候选表 1 张'],
    delete: ['检索打标表：orders', 'FK 扩展 → order_items、payments', '候选子图 3 张表'],
    ddl: ['结构变更无需表检索', '跳过候选子图构建', '直接进入草稿生成'],
    clv: ['检索打标表：customers、orders', 'FK 扩展 → order_items', '候选子图 3 张表'],
    inventory: ['检索打标表：inventory、products', 'FK 扩展 → categories', '候选子图 3 张表'],
    default: ['检索打标表：orders', '无 FK 扩展', '候选表 1 张']
  },
  sql: {
    returns: ['按商品聚合退货占比', 'JOIN products ↔ order_items ↔ orders ↔ returns', 'ORDER BY return_rate DESC LIMIT 10'],
    dml: ['构造 UPDATE 语句', 'SET price = price * 1.1', 'WHERE stock = 0（带条件）'],
    delete: ['构造 DELETE 语句', 'WHERE 条件缺失', '需拦截：无 WHERE 全表删除'],
    ddl: ['生成 CREATE INDEX 草稿', 'DDL 仅草稿，不执行', '—'],
    clv: ['按客户聚合订单与营收', 'JOIN customers ↔ orders ↔ order_items', 'ORDER BY revenue DESC LIMIT 6'],
    inventory: ['按库存周转聚合', 'JOIN inventory ↔ products ↔ categories', 'ORDER BY last_moved_at ASC LIMIT 6'],
    default: ['构造 SELECT 查询', '取 orders 全字段', 'ORDER BY created_at DESC LIMIT 50']
  },
  gate: {
    returns: ['安全闸门评估中…', '检查语句类型：SELECT', '判定生成中…'],
    dml: ['安全闸门评估中…', '检查语句类型：UPDATE', '判定生成中…'],
    delete: ['安全闸门评估中…', '检查语句类型：DELETE · 无 WHERE', '判定生成中…'],
    ddl: ['安全闸门评估中…', '检查语句类型：DDL', '判定生成中…'],
    clv: ['安全闸门评估中…', '检查语句类型：SELECT', '判定生成中…'],
    inventory: ['安全闸门评估中…', '检查语句类型：SELECT', '判定生成中…'],
    default: ['安全闸门评估中…', '检查语句类型：SELECT', '判定生成中…']
  }
}

const STEP_MS = 1500      // 每步停留时长（思考需要时间）
const LINE_MS = 340       // 思考文案逐行浮现间隔

/* ---------- 主组件 ---------- */
export function AiRail({ width, provider }: { width: number; provider?: string | null }): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const connDialect = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.dialect ?? 'sqlite')
  const selectedTable = useSchema((s) => s.selectedTable)
  const push = useResults((s) => s.push)
  const setReport = useResults((s) => s.setReport)
  const upsertSection = useResults((s) => s.upsertSection)
  const setNarration = useResults((s) => s.setNarration)
  const { conversations, activeId, newConversation, rename, saveTurns, touch, select } = useChat()
  const [turns, setTurns] = useState<ChatTurn[]>([])
  const [busy, setBusy] = useState(false)
  const [input, setInput] = useState('')
  const [histOpen, setHistOpen] = useState(false)
  const [settings, setSettings] = useState<SettingsPublic | null>(null)
  useEffect(() => {
    let alive = true
    void getSettings().then((s) => alive && setSettings(s)).catch(() => undefined)
    return () => { alive = false }
  }, [])
  // 默认模型是否支持推理 → 决定是否显示「推理思考」开关
  const defaultModel = settings?.ai_models.find((m) => m.id === settings.default_ai_model)
  const supportsReasoning = !!defaultModel?.reasoning
  const [wantThink, setWantThink] = useState(true)
  // 按对话选模型：null=跟随默认模型；否则用选中的 ai_models id
  const [modelId, setModelId] = useState<string | null>(null)
  // 报告模式：点击「报告」按钮下一句发送 mode=report（绕过意图分类）
  const [forceReport, setForceReport] = useState(false)
  // 澄清挂起：报告中等待用户回答澄清问题时渲染内联输入
  const [clarifyPending, setClarifyPending] = useState<{ q: string; field: string } | null>(null)
  const [clarifyInput, setClarifyInput] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const histRef = useRef<{ role: 'user' | 'assistant'; content: string }[]>([])
  const stepTimers = useRef<ReturnType<typeof setTimeout>[]>([])
  const cardsRef = useRef<AiCard[]>([])
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

  /** 向某一步追加一行思考输出。 */
  function appendStepDetail(idx: number, line: string): void {
    setTurns((t) => {
      const n = [...t]
      const last = n[n.length - 1]
      if (last.role !== 'ai' || !last.steps) return n
      last.steps = last.steps.map((s, i) => (i === idx ? { ...s, detail: [...s.detail, line] } : s))
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
  function maybeAutoRun(card: AiCard | undefined): void {
    if (autoRanRef.current) return
    if (!card || card.verdict !== 'allow') return
    if (!streamDoneRef.current || !gateDoneRef.current) return
    autoRanRef.current = true
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

  /** 逐步播放：每步 running → 逐行浮现思考 → 停留 → done → 下一步。 */
  function playSteps(question: string): void {
    clearStepTimers()
    const mode = thinkMode(question)
    const allLines = THINK_LINES
    const sqlIdx = STEP_DEFS.length - 2   // SQL 生成
    const gateIdx = STEP_DEFS.length - 1  // 安全评估

    for (let idx = 0; idx < STEP_DEFS.length; idx++) {
      const stepStart = idx * STEP_MS
      const lines = allLines[STEP_DEFS[idx].id][mode]

      // 步骤开始：mark running（前一步已 done 由前一轮完成）
      stepTimers.current.push(setTimeout(() => setStepStatus(idx, 'running'), stepStart))
      // 思考文案逐行浮现
      lines.forEach((ln, li) => {
        stepTimers.current.push(setTimeout(() => appendStepDetail(idx, ln), stepStart + 300 + li * LINE_MS))
      })
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

  async function send(opts?: { mode?: 'query' | 'report' }): Promise<void> {
    const q = input.trim()
    if (!q || !currentId) return
    const mode = opts?.mode
    setInput('')
    if (mode === 'report') setForceReport(false)
    setBusy(true)
    const isFirstQ = userCountRef.current === 0
    userCountRef.current += 1
    if (isFirstQ) firstQRef.current = q
    curQuestionRef.current = q
    streamDoneRef.current = false
    gateDoneRef.current = false
    autoRanRef.current = false
    histRef.current.push({ role: 'user', content: q })
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
          reasoning: supportsReasoning ? wantThink : null,
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
          } else if (ev.type === 'sql_card') {
            cardsRef.current = [...cardsRef.current, ev.card]
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
    // 重传历史：原始问题 → 澄清问 → 本次回答（后端 _extract_clarify_answers 凭此 replay）
    const msgs = [
      { role: 'user', content: q },
      { role: 'system', content: pending.q, name: 'clarify' },
      { role: 'user', content: ans }
    ] as const
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
          messages: msgs.map((m) => ({ role: m.role, content: m.content })),
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
      toastMsg(`已执行 · ${r.affected_rows ?? 0} 行受影响`)
    }
  }

  return (
    <aside className="airail" style={{ width }}>
      <div className="airail-head">
        <span className="conv-title" title={activeConv?.title ?? '新对话'}>
          {activeConv?.title ?? '新对话'}
        </span>
        <span className="meta mono">{connName} · model: {provider ?? 'mock'}</span>
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
            <div className="e-s mono">试试：退货率最高的 10 个商品</div>
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
              {t.steps && <StepsPanel steps={t.steps} />}
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
        <div className="ai-in-row">
          <select
            className="a-field model-sel"
            value={modelId ?? ''}
            disabled={busy}
            title="为本次对话选择模型（空=默认）"
            onChange={(e) => setModelId(e.target.value || null)}
          >
            <option value="">· 默认模型 ·</option>
            {(settings?.ai_models ?? []).map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}（{m.provider}）
              </option>
            ))}
          </select>
          <button
            className={`rpt-btn${forceReport ? ' on' : ''}`}
            title="以报告模式发送（绕过意图分类，出分析报告）"
            disabled={busy}
            onClick={() => {
              setForceReport((v) => !v)
              toastMsg(forceReport ? '已切回查询模式' : '下一句将以报告模式发送')
            }}
          >
            报告{forceReport ? ' ✓' : ''}
          </button>
          {supportsReasoning && (
            <button
              className={`rpt-btn think${wantThink ? ' on' : ''}`}
              title={wantThink ? '推理思考开启：模型先思考再回答' : '推理思考关闭'}
              disabled={busy}
              onClick={() => setWantThink((v) => !v)}
            >
              推理{wantThink ? ' ✓' : ''}
            </button>
          )}
          <input
            className="a-field"
            value={input}
            placeholder={forceReport ? '描述想要的分析报告…（报告模式）' : 'ask your database…'}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void send(forceReport ? { mode: 'report' } : undefined) }}
            disabled={busy}
          />
          <button className="send" disabled={busy || !input.trim()} onClick={() => void send(forceReport ? { mode: 'report' } : undefined)}>→</button>
        </div>
      </div>
    </aside>
  )
}
