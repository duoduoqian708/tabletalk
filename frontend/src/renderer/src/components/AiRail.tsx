import React, { Suspense, useEffect, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useSchema } from '@renderer/store/schema'
import { useResults } from '@renderer/store/results'
import { useUi } from '@renderer/store/ui'
import { useAuditSignal } from '@renderer/store/auditSignal'
import { runQuery, cancelDml, formatSql } from '@renderer/api/query'
import type { QueryResponse } from '@renderer/api/types'
import {
  chatStream,
  selection,
  setSessionTitle,
  type AiCard,
  type AiEvent,
  type Manifest,
  type ReportSectionResult
} from '@renderer/api/ai'
import { toastMsg } from '@renderer/utils/toast'
import { fmtDT } from '@renderer/lib/timefmt'
import { useChat, generateTitle, relTime, type Turn as ChatTurn, type Conversation } from '@renderer/store/chat'
import { useKbGate } from '@renderer/store/kbgate'
import { getSettings, type SettingsPublic } from '@renderer/api/settings'
import { listSkills } from '@renderer/api/skills'
import { useI18n } from '@renderer/store/i18n'
import { getRuntime } from '@renderer/api/client'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
const CmEditor = React.lazy(() => import('./CmEditor').then((m) => ({ default: m.CmEditor })))

/* ---------- AI 文本的 MD 渲染（压平空行；react-markdown 默认不渲染原始 HTML，天然安全） ---------- */
function AiMd({ text }: { text: string }): React.JSX.Element {
  // 模型爱输出 \n\n\n… 段落间隔；压成最多一个空行，避免「空行刷屏」
  const src = text.replace(/\n{3,}/g, '\n\n')
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{src}</ReactMarkdown>
}

/* ---------- 推理步骤状态机 ---------- */
type StepStatus = string

interface Step {
  id: string
  label: string
  status: StepStatus
  detail: string[]
}

const STEP_DEFS: { id: string; label: string }[] = [
  { id: 'intent', label: 'ws.stepIntent' },
  { id: 'retrieval', label: 'ws.stepRetrieval' },
  { id: 'sql', label: 'ws.stepSql' },
  { id: 'gate', label: 'ws.stepGate' }
]

function makeSteps(): Step[] {
  return STEP_DEFS.map((s, i) => ({ ...s, status: i === 0 ? 'running' : ('pending' as StepStatus), detail: [] }))
}

const GATE_LABEL: Record<string, string> = {
  allow: 'ws.gateAllow',
  review: 'ws.gateReview',
  block: 'ws.gateBlock'
}

/* 推理步骤：默认收成一行轻量指示，点击展开细节（真实事件驱动，无 mock 播放） */
function ThinkPanel({ steps }: { steps: Step[] }): React.JSX.Element {
  const { t } = useI18n()
  const [open, setOpen] = useState(true)
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
            <span>{t('ws.thinking')}</span>
          </>
        ) : (
          <>
            <span className="ok">✓</span>
            <span>{t('ws.evalSteps', { n: doneCount })}</span>
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
                    {t(s.label)}
                  </span>
                  <span className="step-arrow">▾</span>
                </div>
                <div className={`step-detail${isOpen ? ' open' : ''}`}>
                  {s.detail.length === 0 ? (
                    <div className="sd-empty mono">{s.status === 'running' ? t('ws.thinking') : '—'}</div>
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

function BlockRenderer({ block }: { block: import('@renderer/api/ai').Block }): React.JSX.Element {
  const { t } = useI18n()
  if (block.kind === 'sql_editor') {
    return <div className="blk-code mono"><div className="blk-label">SQL</div><pre>{block.sql}</pre></div>
  }
  if (block.kind === 'table') {
    return <div className="blk-table"><div className="blk-label">{block.title || t('aiRail.blockTable')}</div><div className="mono" style={{ fontSize: '11px' }}>{block.columns.join(' | ')} — {t('aiRail.blockRows', { n: block.rows.length })}</div></div>
  }
  if (block.kind === 'chart') {
    return <div className="blk-chart"><div className="blk-label">{t('aiRail.blockChart', { type: block.chartType })}</div><div className="mono" style={{ fontSize: '11px' }}>{block.title || ''} — {t('aiRail.blockPlaceholder')}</div></div>
  }
  if (block.kind === 'confirm') {
    return <div className="blk-confirm"><span className="mono">{block.prompt}</span><button className="btn pri" style={{ marginLeft: 8 }}>{block.confirmLabel || t('common.confirm')}</button><button className="btn gho" style={{ marginLeft: 6 }}>{block.cancelLabel || t('common.cancel')}</button></div>
  }
  if (block.kind === 'choice') {
    return <div className="blk-choice"><div className="mono">{block.prompt}</div><div style={{ marginTop: 6 }}>{block.options.map((o) => <label key={o.value} className="mono" style={{ marginRight: 12 }}><input type={block.multiple ? 'checkbox' : 'radio'} name="choice" value={o.value} /> {o.label}</label>)}</div></div>
  }
  return <div className="blk-text mono">{(block as { text: string }).text}</div>
}

function SubtaskPanel({ subtasks, scene }: { subtasks: import('@renderer/store/chat').Subtask[]; scene?: string }): React.JSX.Element {
  const { t } = useI18n()
  const [open, setOpen] = useState(true)
  if (!subtasks || subtasks.length === 0) return <></>
  const running = subtasks.some((s) => s.status === 'running')
  const doneCount = subtasks.filter((s) => s.status === 'done').length
  return (
    <div className="think-panel">
      <div className="think-line" onClick={() => setOpen((o) => !o)}>
        {running ? <><span className="spin" /><span>{scene ? `${scene} · ${t('ws.thinking')}` : t('ws.thinking')}</span></> : <><span className="ok">✓</span><span>{t('ws.evalSteps', { n: doneCount })}</span></>}
        <span className="spacer" />
        <span className="step-arrow">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="steps">
          {subtasks.map((s) => (
            <div className={`step ${s.status}`} key={s.id}>
              <div className="step-h">
                <span className="step-ic">{s.status === 'done' && <span className="ok">✓</span>}{s.status === 'running' && <span className="spin" />}{s.status === 'error' && <span className="ok">✗</span>}</span>
                <span className="step-label">{s.label || s.tool}</span>
                <span className="mono" style={{ fontSize: '10px', opacity: 0.6 }}>{s.tool}</span>
              </div>
              <div className="step-detail open">
                {s.details.length === 0 && s.blocks.length === 0 ? <div className="sd-empty mono">{s.status === 'running' ? t('ws.thinking') : '—'}</div> : null}
                {s.details.map((d, di) => <div key={di} className="sd-line mono">{d}</div>)}
                {s.blocks.map((b, bi) => <BlockRenderer key={bi} block={b} />)}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/* ---------- 出网清单（B1） ---------- */
function ManifestView({ manifest }: { manifest: Manifest }): React.JSX.Element {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const human = (() => {
    const nTables = manifest.tables.length
    const kb = manifest.kb_docs
    const hist = manifest.history_turns
    const rows = manifest.include_data ? t('aiRail.withRows') : t('aiRail.noRows')
    const modeMap: Record<string, string> = { strict: t('aiRail.modeStrict'), standard: t('aiRail.modeStandard'), open: t('aiRail.modeOpen') }
    const mode = modeMap[manifest.mode] ?? manifest.mode
    return t('aiRail.manifestSummary', { nTables, kb, hist, rows, mode })
  })()
  return (
    <div className={`manifest ${open ? 'open' : ''}`}>
      <div className="manifest-head" onClick={() => setOpen((o) => !o)}>
        <span className="manifest-ic">◈</span>
        <span className="manifest-title">{t('aiRail.manifestTitle')}</span>
        <span className="manifest-human mono">{human}</span>
        <span className="spacer" />
        <span className="manifest-arrow">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="manifest-body mono">
          <div className="manifest-row"><span>tables</span><span>{manifest.tables.length ? manifest.tables.join(', ') : '—'}</span></div>
          <div className="manifest-row"><span>kb_docs</span><span>{manifest.kb_docs}</span></div>
          <div className="manifest-row"><span>history</span><span>{manifest.history_turns}</span></div>
          <div className="manifest-row"><span>include_data</span><span>{String(manifest.include_data)}</span></div>
          <div className="manifest-row"><span>mode</span><span>{manifest.mode}</span></div>
          <div className="manifest-row"><span>model</span><span>{manifest.model || '—'} ({manifest.provider})</span></div>
          <div className="manifest-row"><span>ts</span><span>{fmtDT(manifest.ts)}</span></div>
          {manifest.redactions.length > 0 && <div className="manifest-row"><span>redactions</span><span>{manifest.redactions.join(', ')}</span></div>}
        </div>
      )}
    </div>
  )
}

/* ---------- 爆炸半径（A3） ---------- */
function BlastView({ blast }: { blast: import('@renderer/api/ai').Blast }): React.JSX.Element {
  const { t } = useI18n()
  const [open, setOpen] = useState(true)
  if (!blast) return <></>
  const hasCascade = blast.cascade.length > 0
  return (
    <div className={`blast ${open ? 'open' : ''}`}>
      <div className="blast-head" onClick={() => setOpen((o) => !o)}>
        <span className="blast-ic">◎</span>
        <span className="blast-title">{t('aiRail.blastTitle')}</span>
        <span className="blast-human mono">
          {blast.direct.map((d) => `${d.table}${d.estimated_rows != null ? t('aiRail.estRows', { n: d.estimated_rows }) : ''}`).join(', ')}
          {hasCascade ? ` → ${blast.cascade.map((c) => c.table).join(', ')}` : t('aiRail.noCascade')}
        </span>
        <span className="spacer" />
        <span className="blast-arrow">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="blast-body">
          <div className="blast-sec">
            <div className="blast-label mono">direct</div>
            <div className="blast-chips">
              {blast.direct.map((d) => (
                <span key={d.table} className="blast-chip mono">
                  {d.table}
                  {d.estimated_rows != null && <span className="blast-est">≈{d.estimated_rows}</span>}
                </span>
              ))}
            </div>
          </div>
          {hasCascade ? (
            <div className="blast-sec">
              <div className="blast-label mono">{t('aiRail.cascadeLabel')}</div>
              <div className="blast-path">
                {blast.cascade.map((c, idx) => (
                  <span key={c.table} className="blast-hop">
                    {idx > 0 && <span className="blast-arrow2">→</span>}
                    <span className={`blast-chip small ${c.has_fk ? 'fk' : 'no-fk'}`}>{c.table}</span>
                    {c.fk && <span className="blast-fk mono">{c.fk}</span>}
                  </span>
                ))}
              </div>
            </div>
          ) : (
            <div className="blast-sec"><span className="blast-empty mono">{t('aiRail.noFkCascade')}</span></div>
          )}
          {blast.constraints.length > 0 && (
            <div className="blast-sec">
              <div className="blast-label mono">constraints</div>
              <div className="blast-constraints mono">{blast.constraints.join(' · ')}</div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function RollbackView({ rollback }: { rollback: any }): React.JSX.Element {
  const { t } = useI18n()
  const [open, setOpen] = useState(true)
  if (!rollback) return <></>
  return (
    <div className={`rollback ${open ? 'open' : ''}`}>
      <div className="rollback-head" onClick={() => setOpen((o) => !o)}>
        <span className="rollback-ic">↩</span>
        <span className="rollback-title">{t('aiRail.rollbackTitle')}</span>
        <span className="rollback-note mono">{rollback.note}</span>
        <span className="spacer" />
        <span className="rollback-arrow">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="rollback-body mono">
          {rollback.backup_sql && (
            <div className="rollback-sec">
              <div className="rollback-label">{t('aiRail.backupExport')}</div>
              <pre className="rollback-code">{rollback.backup_sql}</pre>
              <button className="mini-btn" onClick={() => navigator.clipboard.writeText(rollback.backup_sql)}>{t('aiRail.copy')}</button>
            </div>
          )}
          <div className="rollback-sec">
            <div className="rollback-label">{t('aiRail.rollbackSql')}</div>
            <pre className="rollback-code">{rollback.rollback_sql}</pre>
            <button className="mini-btn" onClick={() => navigator.clipboard.writeText(rollback.rollback_sql)}>{t('aiRail.copy')}</button>
          </div>
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
void highlightSql
void SqlEditor

/* ---------- SQL 卡片（常驻可编辑编辑器） ---------- */
function SqlCard({ card, question, busy, pending, dialect, connectionId, sessionId, onRun, onConfirm, onAnalyze }: {
  card: AiCard
  question: string
  busy: boolean
  /** 安全评估尚未完成：徽标显示"评估中"，按钮禁用 */
  pending: boolean
  dialect: string
  connectionId: string | null
  sessionId: string | null
  onRun: (sql: string) => void
  onConfirm: (sql: string, card?: AiCard) => void
  onAnalyze: (kind: 'explain' | 'optimize' | 'risk', sql: string) => void
}): React.JSX.Element {
  const { t } = useI18n()
  const [draft, setDraft] = useState(card.sql)
  const [formatting, setFormatting] = useState(false)
  // S3：可选追加项改写中（轻量 LLM 调用）
  const [optionBusy, setOptionBusy] = useState<string | null>(null)
  // 闸门判定落地瞬间 → 一次性护盾闪光
  const [flash, setFlash] = useState(false)
  const wasPending = useRef(pending)
  useEffect(() => {
    if (wasPending.current && !pending) {
      setFlash(true)
      const t = window.setTimeout(() => setFlash(false), 800)
      return () => window.clearTimeout(t)
    }
    wasPending.current = pending
  }, [pending])

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
    { key: 'explain', label: 'ws.analyzeExplain' },
    { key: 'optimize', label: 'ws.analyzeOptimize' },
    { key: 'risk', label: 'ws.analyzeRisk' }
  ]
  // C4 SQL 折叠（默认折叠，记忆在 localStorage）
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem('tabletalk-sql-fold') !== '0' } catch { return true }
  })
  const toggleFold = (): void => {
    const v = !collapsed
    setCollapsed(v)
    try { localStorage.setItem('tabletalk-sql-fold', v ? '1' : '0') } catch {}
  }
  // C5 可追溯：表引用（优先 card 显式 tables，其次 blast，其次 sql 解析）
  const traceTables: string[] = (() => {
    const fromCard = (card as unknown as { tables?: string[] }).tables
    if (Array.isArray(fromCard) && fromCard.length) return fromCard
    if (card.blast?.direct?.length) return card.blast.direct.map((d) => d.table)
    // 回退：从 sql 粗提取 FROM/JOIN 后的表名
    const m = draft.match(/\b(?:FROM|JOIN)\s+["'`]?(\w+)["'`]?/gi)
    if (m) return m.map((s) => s.split(/\s+/).pop()?.replace(/["'`]/g, "") ?? "").filter(Boolean).slice(0, 4)
    return []
  })()
  // C6 保存为常用问题
  const [saved, setSaved] = useState(false)
  // S3：点选项 → 轻量改写接口 → 替换当前 SQL（用户确认后执行）
  async function applyOption(opt: { id?: string; label: string; hint?: string }): Promise<void> {
    if (!connectionId || optionBusy) return
    const rt = getRuntime()
    if (!rt?.token) return
    setOptionBusy(opt.id ?? opt.label)
    try {
      const r = await fetch(`/api/v1/ai/sql-option`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': rt.token },
        body: JSON.stringify({ connection_id: connectionId, sql: draft, option: opt }),
      })
      const j = await r.json().catch(() => null)
      if (r.ok && j?.sql) setDraft(j.sql)
    } catch {
      /* 改写失败静默：SQL 不变 */
    } finally {
      setOptionBusy(null)
    }
  }
  const handleSave = async (): Promise<void> => {
    if (!connectionId) return
    const rt = getRuntime()
    if (!rt?.token) return
    try {
      const r = await fetch(`/api/v1/questions/${connectionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': rt.token },
        body: JSON.stringify({ question, sql: draft, tables: traceTables }),
      })
      if (r.ok) setSaved(true)
    } catch {}
  }
  // C3 追问链（本地规则生成 2-3 个）
  const followUps: string[] = (() => {
    const tbl = traceTables[0] || ''
    const arr: string[] = []
    if (tbl) arr.push(t('aiRail.followWeekly', { tbl }))
    arr.push(t('aiRail.followRecent7d'))
    arr.push(t('aiRail.followYoY'))
    return arr.slice(0, 3)
  })()

  return (
    <div className={`sql ${cls}${pending ? ' pending' : ''} ${collapsed ? 'folded' : ''}`}>
      {flash && <span className="gate-flash" />}
      <div className="c-h">
        {pending ? <span className="fst eval">{t('ws.evaluating')}</span> : <CardBadge card={card} />}
        <span className="c-question" title={question}>{question}</span>
        <span className="c-acts">
          {!pending && ANALYZE.map((a) => (
            <button key={a.key} className="ca" onClick={() => onAnalyze(a.key, draft)}>{a.label}</button>
          ))}
        </span>
        <span className="st">{card.sub ?? ''}</span>
        <button className="fold-toggle mono" onClick={toggleFold} title={collapsed ? t('aiRail.unfoldSqlTitle') : t('aiRail.foldSqlTitle')}>
          {collapsed ? t('aiRail.showQuery') : t('aiRail.collapse')}
        </button>
      </div>

      {!collapsed && (
        <Suspense fallback={<div className="cm-loading mono">{t('aiRail.loadingEditor')}</div>}>
          <CmEditor
            value={draft}
            onChange={setDraft}
            connectionId={connectionId}
            dialect={dialect}
          />
        </Suspense>
      )}
      {collapsed && (
        <div className="sql-folded mono" onClick={toggleFold} title={t('aiRail.clickToExpand')}>
          {t('aiRail.showQueryPrefix')}{draft.split('\n')[0].slice(0, 42)}…
        </div>
      )}
      {/* C5 可追溯 */}
      {traceTables.length > 0 && (
        <div className="trace-row">
          <span className="trace-label mono">{t('aiRail.refTables')}</span>
          <span className="trace-chips">
            {traceTables.map((tbl) => (
              <button key={tbl} className="trace-chip mono" title={t('aiRail.locateInGraph')} onClick={() => {
                // 触发星图定位：通过全局事件或直接操作？简化：派发自定义事件
                window.dispatchEvent(new CustomEvent('tabletalk:locate', { detail: { table: tbl } }))
              }}>{tbl}</button>
            ))}
          </span>
        </div>
      )}
      {/* S3：可选追加项（LLM 建议，用户点一下 → 轻量改写当前 SQL） */}
      {card.options && card.options.length > 0 && !card.executed && !blocked && (
        <div className="option-row">
          <span className="trace-label mono">{t('aiRail.sqlOptions')}</span>
          <span className="trace-chips">
            {card.options.map((opt) => (
              <button key={opt.id ?? opt.label} className={`option-chip mono${optionBusy === (opt.id ?? opt.label) ? ' busy' : ''}`}
                disabled={!!optionBusy} onClick={() => void applyOption(opt)}>
                {optionBusy === (opt.id ?? opt.label) ? '…' : `+ ${opt.label}`}
              </button>
            ))}
          </span>
        </div>
      )}

      <div className="c-f">
        {card.question_library && (
          <div className="risk-panel ql-hit">
            <div className="rp-title">{t('ws.qlHit')}</div>
            <div className="rp-line mono">{t('ws.qlDesc')}</div>
            <div className="rp-line">
              <button className="btn pri" disabled={busy || !draft.trim()} onClick={() => onRun(draft)}>{t('ws.qlExec')}</button>
              <span className="rp-hint mono">{t('ws.confirmExpiredTip')}</span>
            </div>
          </div>
        )}
        {card.needs_confirm && !card.executed && !card.question_library && (
          <div className="risk-panel">
            <div className="rp-title">{t('ws.riskTitle')}</div>
            <div className="rp-line mono">{card.expires_in != null && card.expires_in <= 0 ? t('ws.confirmExpired') : t('ws.confirmExecWithRows', { n: card.preview_rows ?? '?' })}</div>
            {card.blast && <BlastView blast={card.blast as unknown as import('@renderer/api/ai').Blast} />}
            {(card as unknown as { rollback: any }).rollback && <RollbackView rollback={(card as unknown as { rollback: any }).rollback} />}
            {(card.reasons && card.reasons.length > 0 ? card.reasons : card.reason ? [{ rule_id: 'legacy', message: card.reason, message_en: card.reason, objects: [] }] : []).map((r, idx) => {
              const { locale } = useI18n.getState()
              const tr = t(`gate.rule.${(r as any).rule_id}`)
              const msg = tr !== `gate.rule.${(r as any).rule_id}` ? tr : (locale === 'en-US' && (r as any).message_en ? (r as any).message_en : (r as any).message)
              const objs = (r as any).objects as string[] | undefined
              return (
                <div key={idx} className="rp-line rp-reason">
                  <span className="rp-rule mono">{(r as any).rule_id}</span>
                  <span className="rp-msg">{msg}</span>
                  {objs && objs.length > 0 && <span className="rp-objs mono">· {objs.join(', ')}</span>}
                </div>
              )
            })}
            <div className="rp-line">
              <button className="mini-btn" onClick={async () => {
                const rt = getRuntime()
                if (!rt?.token || !connectionId) return
                const r = await fetch(`/api/v1/approvals`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': rt.token }, body: JSON.stringify({ connection_id: connectionId, sql: draft }) })
                if (r.ok) alert(t('aiRail.sentToApproval'))
                else {
                  const j = await r.json().catch(()=>({detail:'failed'}))
                  alert(j.detail || t('aiRail.toApprovalFail'))
                }
              }}>{t('aiRail.toApproval')}</button>
            </div>
            <div className="rp-line dim">{card.expires_in != null && card.expires_in <= 0 ? t('ws.confirmExpiredTip') : t('ws.riskNote')}</div>
          </div>
        )}
        {card.verdict === 'review' && !card.executed && !card.needs_confirm && !card.question_library && (
          <div className="risk-panel">
            <div className="rp-title">{t('ws.riskTitle')}</div>
            <div className="rp-line mono">{t('ws.riskScope', { n: card.preview_rows ?? '?' })}</div>
            {card.blast && <BlastView blast={card.blast as unknown as import('@renderer/api/ai').Blast} />}
            {(card as unknown as { rollback: any }).rollback && <RollbackView rollback={(card as unknown as { rollback: any }).rollback} />}
            {(card.reasons && card.reasons.length > 0 ? card.reasons : card.reason ? [{ rule_id: 'legacy', message: card.reason, message_en: card.reason, objects: [] }] : []).map((r, idx) => {
              const { locale } = useI18n.getState()
              const tr = t(`gate.rule.${(r as any).rule_id}`)
              const msg = tr !== `gate.rule.${(r as any).rule_id}` ? tr : (locale === 'en-US' && (r as any).message_en ? (r as any).message_en : (r as any).message)
              const objs = (r as any).objects as string[] | undefined
              return (
                <div key={idx} className="rp-line rp-reason">
                  <span className="rp-rule mono">{(r as any).rule_id}</span>
                  <span className="rp-msg">{msg}</span>
                  {objs && objs.length > 0 && <span className="rp-objs mono">· {objs.join(', ')}</span>}
                </div>
              )
            })}
            <div className="rp-line">
              <button className="mini-btn" onClick={async () => {
                const rt = getRuntime()
                if (!rt?.token || !connectionId) return
                const r = await fetch(`/api/v1/approvals`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': rt.token }, body: JSON.stringify({ connection_id: connectionId, sql: draft }) })
                if (r.ok) alert(t('aiRail.sentToApproval'))
                else {
                  const j = await r.json().catch(()=>({detail:'failed'}))
                  alert(j.detail || t('aiRail.toApprovalFail'))
                }
              }}>{t('aiRail.toApproval')}</button>
            </div>
            <div className="rp-line dim">{t('ws.riskNote')}</div>
          </div>
        )}
        {card.executed && (
          <div className="exec-stamp mono">{t('ws.execStamp', { n: card.affected ?? 0 })}</div>
        )}
        {blocked && (
          <div className="risk-panel block">
            <div className="rp-title">{t('ws.blocked')}</div>
            {(card.reasons && card.reasons.length > 0 ? card.reasons : card.reason ? [{ rule_id: 'legacy', message: card.reason, message_en: card.reason, objects: [] }] : []).map((r, idx) => {
              const { locale } = useI18n.getState()
              const tr = t(`gate.rule.${(r as any).rule_id}`)
              const msg = tr !== `gate.rule.${(r as any).rule_id}` ? tr : (locale === 'en-US' && (r as any).message_en ? (r as any).message_en : (r as any).message)
              const objs = (r as any).objects as string[] | undefined
              return (
                <div key={idx} className="rp-line rp-reason">
                  <span className="rp-rule mono">{(r as any).rule_id}</span>
                  <span className="rp-msg">{msg}</span>
                  {objs && objs.length > 0 && <span className="rp-objs mono">· {objs.join(', ')}</span>}
                </div>
              )
            })}
          </div>
        )}
        <span className="spacer" />
        {!pending && !card.executed && (
          <button className="btn gho" disabled={formatting} onClick={() => void handleFormat()}>
            {formatting ? '…' : t('ws.format')}
          </button>
        )}
        {pending ? (
          <span className="c-p">{t('ws.gateEvaluating')}</span>
        ) : card.executed ? (
          <span className="c-p done">{t('ws.done')}</span>
        ) : card.verdict === 'allow' ? (
          <button className="btn pri" disabled={busy || !draft.trim()} onClick={() => onRun(draft)}>{t('ws.run')}</button>
        ) : card.verdict === 'review' ? (
          card.needs_confirm ? (
            card.expires_in != null && card.expires_in <= 0 ? (
              <span className="c-r">{t('ws.confirmExpired')}</span>
            ) : (
              <>
                <button className="btn warn" disabled={busy || !draft.trim()} onClick={() => onConfirm(draft, card)}>{card.preview_rows != null ? t('ws.confirmExecWithRows', { n: card.preview_rows }) : t('ws.confirmExec')}</button>
                <button className="btn gho" disabled={busy} onClick={async () => { if (!card.confirm_token || !sessionId) return; try { await cancelDml(sessionId, card.confirm_token); toastMsg(t('ws.confirmCancelled')) } catch {} }}>{t('ws.confirmCancel')}</button>
              </>
            )
          ) : (
            <button className="btn warn" disabled={busy || !draft.trim()} onClick={() => onConfirm(draft, card)}>{t('ws.confirmExec')}</button>
          )
        ) : blocked ? (
          <span className="c-r">{t('ws.ddlBlock')}</span>
        ) : (
          <span className="c-r">{t('ws.draftManual')}</span>
        )}
      </div>
      {/* C3 追问链 + C6 保存 */}
      <div className="card-foot">
        <div className="follow-ups">
          {followUps.map((q, idx) => (
            <button key={idx} className="follow-chip" onClick={() => window.dispatchEvent(new CustomEvent('tabletalk:followup', { detail: { question: q } }))}>{q}</button>
          ))}
        </div>
        <button className={`save-chip ${saved ? 'saved' : ''}`} onClick={() => void handleSave()} disabled={saved}>
          {saved ? t('aiRail.savedChip') : t('aiRail.saveQuestion')}
        </button>
      </div>
    </div>
  )
}

/* 欢迎区推荐问题（演示库场景；点击直接发送） */
const SUGGESTIONS: string[] = [
  'ws.sug1',
  'ws.sug2',
  'ws.sug3',
  'ws.sug4'
]

/**
 * WS2（T2.4）：C2 建议只引用 enabled 技能涉及的能力——纯函数，便于单测。
 * 每种建议归属一个技能（query 为地板常开）；disabled 的技能不参与生成。
 * confirmed：已确认领域标签；tableForTag：标签→代表表；enabledIds：启用技能 id 集。
 */
function buildSuggestions(
  confirmed: string[],
  tableForTag: Record<string, string>,
  t: (key: string, vars?: Record<string, string | number>) => string,
  enabledIds: Set<string>
): string[] {
  const sugs: string[] = []
  // 数据查询建议（query 地板；默认涵盖现有"按月统计"生成）
  if (enabledIds.has('query')) {
    for (const tg of confirmed.slice(0, 5)) {
      const tbl = tableForTag[tg] || ''
      sugs.push(tbl ? t('aiRail.sugMonthlyCount', { tag: tg, tbl }) : t('aiRail.sugQueryTag', { tag: tg }))
    }
  }
  if (enabledIds.has('schema') && confirmed.length) {
    sugs.push(t('aiRail.sugSchemaList'))
  }
  if (enabledIds.has('report') && confirmed.length) {
    sugs.push(t('aiRail.sugReportOn', { tag: confirmed[0] }))
  }
  // query 常开兜底：补齐到至少 3 条
  if (enabledIds.has('query') && confirmed.length) {
    let i = 0
    while (sugs.length < 3) {
      sugs.push(t('aiRail.sugTrendOf', { tag: confirmed[i % confirmed.length] }))
      i++
    }
  }
  return sugs.slice(0, 5)
}

/* ---------- 主组件 ---------- */
export function AiRail(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const { t } = useI18n()
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
  const [secPolicyOpen, setSecPolicyOpen] = useState(false)
  const [sugs, setSugs] = useState<string[]>([])
  // 对话面板：底部命令中心聚焦/发送时向上弹出
  const [panelOpen, setPanelOpen] = useState(false)
  useEffect(() => {
    if (!panelOpen) return
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setPanelOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [panelOpen])
  const ctxTable = selectedTable // 上下文筹码：当前选中的表
  const [settings, setSettings] = useState<SettingsPublic | null>(null)
  useEffect(() => {
    let alive = true
    void getSettings().then((s) => alive && setSettings(s)).catch(() => undefined)
    return () => { alive = false }
  }, [])
  // 按对话选模型：跟随默认模型（可通过设置切换）
  const modelId = settings?.default_ai_model ?? null
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
  // C2 猜你想问：基于已确认领域标签 + enabled 技能本地生成 3-5 个（零模型调用）
  const [dynamicSugs, setDynamicSugs] = useState<string[] | null>(null)
  // WS2（T2.4）：启用技能集合（query/refusal 为地板，后端保证恒 enabled）
  const [enabledSkills, setEnabledSkills] = useState<Set<string>>(new Set())
  useEffect(() => {
    let alive = true
    void listSkills()
      .then((cat) => alive && setEnabledSkills(new Set(cat.skills.filter((s) => s.enabled).map((s) => s.id))))
      .catch(() => undefined)
    return () => { alive = false }
  }, [])
  useEffect(() => {
    if (!currentId) { setDynamicSugs(null); return }
    let alive = true
    void (async () => {
      try {
        const rt = getRuntime()
        if (!rt?.token) return
        const r = await fetch(`/api/v1/knowledge/${currentId}/tags`, { headers: { 'X-TableTalk-Token': rt.token } })
        if (!r.ok) return
        const j = await r.json() as { library: { name: string; status: string }[]; tables: Record<string, string[]> }
        const confirmed = j.library.filter((t) => t.status === 'confirmed').map((t) => t.name)
        if (confirmed.length === 0 || !alive) { setDynamicSugs(null); return }
        // 为每个标签找一张代表表
        const tableForTag: Record<string, string> = {}
        for (const [tbl, tags] of Object.entries(j.tables)) {
          for (const tg of tags as string[]) {
            if (confirmed.includes(tg) && !tableForTag[tg]) tableForTag[tg] = tbl
          }
        }
        const sugs = buildSuggestions(confirmed, tableForTag, useI18n.getState().t, enabledSkills)
        if (alive) setDynamicSugs(sugs)
      } catch {}
    })()
    return () => { alive = false }
  }, [currentId, enabledSkills])
  // 输入框自动增高
  const inputRef = useRef<HTMLTextAreaElement>(null)
  function autoGrow(): void {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(180, Math.max(56, el.scrollHeight)) + 'px'
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
  const secPolicyDdRef = useRef<HTMLDivElement>(null)
  const streamDoneRef = useRef(false)
  const gateDoneRef = useRef(false)
  const autoRanRef = useRef(false)
  const curQuestionRef = useRef('')

  const activeConv = conversations.find((c) => c.id === activeId) ?? null

  // 点击外部关闭下拉（历史对话 / 安全策略）
  useEffect(() => {
    const onDoc = (e: MouseEvent): void => {
      if (histDdRef.current && !histDdRef.current.contains(e.target as Node)) setHistOpen(false)
      if (secPolicyDdRef.current && !secPolicyDdRef.current.contains(e.target as Node)) setSecPolicyOpen(false)
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

  /** T3.5 切库换 session：取得当前连接对应的会话 id。
   * 活动会话若属于其它连接（切库竞态 / 异常状态）→ 立即开新会话，绝不发混合上下文请求（后端另有 409 双保险）。 */
  function resolveSessionForConn(connId: string): string {
    const { activeId: aid, conversations: convs } = useChat.getState()
    const act = convs.find((c) => c.id === aid)
    if (act && act.connId === connId && aid) return aid
    const id = newConversation(connId)
    setTurns([])
    histRef.current = []
    userCountRef.current = 0
    firstQRef.current = ''
    return id
  }

  function clearStepTimers(): void {
    stepTimers.current.forEach((t) => clearTimeout(t))
    stepTimers.current = []
  }

  /** 将暂存的卡片挂到当前 turn（安全评估未完成时 pending=true）。
   *  仅当 SQL 生成步已完成（播放器走到）或流已结束时挂载，避免卡片早于步骤出现。 */
  function attachCards(): void {
    const cards = cardsRef.current
    if (cards.length === 0) return
    setTurns((tt) => {
      const n = [...tt]
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
        i === sqlIdx && !s.detail.some((d) => d.startsWith(t('ws.genSql')))
          ? { ...s, detail: [...s.detail, `${t('ws.genSql')}${cards[cards.length - 1].sql.split('\n')[0]}`] }
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
    setTurns((tt) => {
      const n = [...tt]
      const last = n[n.length - 1]
      if (last.role !== 'ai' || !last.steps) return n
      last.pending = false
      const g = last.steps[last.steps.length - 1]
      if (card && !g.detail.some((d) => d.startsWith(t('ws.verdictPrefix')))) {
        const glKey = GATE_LABEL[card.verdict]
        const glText = glKey ? t(glKey) : card.verdict
        g.detail = [...g.detail, `${t('ws.verdictPrefix')} ${glText}${card.preview_rows != null ? t('ws.verdictRows', { n: card.preview_rows }) : ''}`]
      }
      return n
    })
    gateDoneRef.current = true
    maybeAutoRun(card)
  }

  async function send(inputText?: string, opts?: { mode?: 'query' | 'report' }): Promise<void> {
    const q = (inputText ?? input).trim()
    if (!q || !currentId) return
    const mode = opts?.mode
    setInput('')
    setSugs([])
    if (inputRef.current) inputRef.current.style.height = 'auto'
    setBusy(true)
    setPanelOpen(true)
    const isFirstQ = userCountRef.current === 0
    userCountRef.current += 1
    if (isFirstQ) firstQRef.current = q
    curQuestionRef.current = q
    // T3.5：请求恒锁当前连接的会话 id；若活动会话属其它连接 → resolveSessionForConn 已开新会话
    const reqSid = resolveSessionForConn(currentId)
    const convForReq = useChat.getState().conversations.find((c) => c.id === reqSid)
    streamDoneRef.current = false
    gateDoneRef.current = false
    autoRanRef.current = false
    loopResultRef.current = null
    // 上下文联动：当前选中表自动附加到提问（用户已可见上下文筹码，可移除）
    const ctx = ctxTable && !q.includes(`@${ctxTable}`) && !q.includes(ctxTable) ? t('aiRail.ctxSuffix', { tbl: ctxTable }) : ''
    histRef.current.push({ role: 'user', content: `${q}${ctx}` })
    const cur = histRef.current
    const reportTitle = q.replace(/[？?。.!！\s]+$/, '').slice(0, 24)
    // 报告模式：turn 标记 isReport，不走 query 模式步骤动画
    if (mode === 'report') {
      setTurns((t) => [...t, { role: 'user', text: q }])
      setTurns((t) => [...t, { role: 'ai', question: q, thinks: [], cards: [], running: true, isReport: true, sessionId: reqSid }])
    } else {
      setTurns((t) => [...t, { role: 'user', text: q }])
      setTurns((t) => [...t, { role: 'ai', question: q, thinks: [], cards: [], steps: makeSteps(), subtasks: [], running: true, sessionId: reqSid }])
    }
    cardsRef.current = []
    // 提问刷新会话更新时间（查看历史不刷新）
    if (reqSid) touch(reqSid)

    try {
      await chatStream(
        {
          connection_id: currentId,
          messages: cur.map((m) => ({ role: m.role, content: m.content })),
          table: selectedTable,
          session_id: reqSid,
          title: convForReq?.title ?? null,
          mode: mode ?? null,
          model_id: modelId ?? undefined,
        },
        (ev: AiEvent) => {
          if ((ev as unknown as { type: string }).type === 'manifest') {
            const m = (ev as unknown as { manifest: Manifest }).manifest
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last?.role === 'ai') (last as unknown as { manifest: Manifest }).manifest = m
              return n
            })
            return
          }
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
            setTurns((tt) => {
              const n = [...tt]
              const last = n[n.length - 1]
              if (last.role === 'ai') {
                last.text = `${t('ws.planSections', { n: ev.sections.length })}${ev.sections.map((s) => s.title).join(' / ')}`
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
            setTurns((tt) => {
              const n = [...tt]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = t('ws.reportReady')
              return n
            })
            toastMsg(t('ws.reportToast'))
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
            setTurns((tt) => {
              const n = [...tt]
              const last = n[n.length - 1]
              if (last.role === 'ai' && last.steps) {
                const s = last.steps.find((x) => x.id === 'intent')
                if (s) {
                  s.status = 'done'
                  const v = Array.isArray(ev.value) ? (ev.value as string[]).join(' / ') : String(ev.value ?? '')
                  s.detail = [...s.detail, `${t('ws.intentTag')}${v || t('ws.intentNone')}`]
                }
              }
              return n
            })
          } else if (ev.type === 'stage' && ev.stage === 'retrieval') {
            // 四步展示：表检索定位（真实候选表清单：标签路由 × 向量召回 → FK 扩展）
            setTurns((tt) => {
              const n = [...tt]
              const last = n[n.length - 1]
              if (last.role === 'ai' && last.steps) {
                const s = last.steps.find((x) => x.id === 'retrieval')
                const tables = ev.tables ?? []
                const vecN = Array.isArray(ev.vec_tables) ? (ev.vec_tables as string[]).length : 0
                if (s) {
                  s.status = 'done'
                  s.detail = [
                    ...s.detail,
                    `${t('ws.candTables', { n: tables.length })}${tables.join(', ') || t('ws.candMiss')}${vecN > 0 ? t('ws.vecRecall', { n: vecN }) : ''}`
                  ]
                }
              }
              return n
            })
          } else if ((ev as unknown as { type: string }).type === 'scene_start') {
            const se = ev as unknown as { scene: string }
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.scene = se.scene
              return n
            })
          } else if ((ev as unknown as { type: string }).type === 'subtask_start') {
            const se = ev as unknown as { id: string; tool: string; label: string }
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') {
                last.subtasks = [...(last.subtasks ?? []), { id: se.id, tool: se.tool, label: se.label || se.tool, status: 'running', details: [], blocks: [] }]
              }
              return n
            })
          } else if ((ev as unknown as { type: string }).type === 'subtask_progress') {
            const se = ev as unknown as { id: string; delta: string }
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai' && last.subtasks) {
                const it = last.subtasks.find((s) => s.id === se.id)
                if (it) it.details = [...it.details, se.delta]
              }
              return n
            })
          } else if ((ev as unknown as { type: string }).type === 'subtask_done') {
            const se = ev as unknown as { id: string; status: string; detail?: string }
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai' && last.subtasks) {
                const it = last.subtasks.find((s) => s.id === se.id)
                if (it) {
                  it.status = se.status === 'error' ? 'error' : 'done'
                  if (se.detail) it.details = [...it.details, se.detail]
                }
              }
              return n
            })
          } else if ((ev as unknown as { type: string }).type === 'block') {
            const se = ev as unknown as { id: string; block: import('@renderer/api/ai').Block }
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai' && last.subtasks) {
                const it = last.subtasks.find((s) => s.id === se.id)
                if (it) it.blocks = [...it.blocks, se.block]
                else {
                  last.subtasks = [...(last.subtasks ?? []), { id: se.id, tool: 'block', label: 'block', status: 'done', details: [], blocks: [se.block] }]
                }
              }
              return n
            })
          } else if (ev.type === 'sql_card') {
            cardsRef.current = [...cardsRef.current, ev.card]
            if (ev.card?.result) loopResultRef.current = ev.card.result
            attachCards()
            finishGate()
          } else if (ev.type === 'error') {
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = `⚠ ${ev.message}`
              return n
            })
            // 知识库未构建 → 强制弹出构建门禁
            if (ev.code === 'kb_not_built' && currentId) {
              useKbGate.getState().forceOpen(currentId)
            }
          } else if (ev.type === 'done') {
            void useAuditSignal.getState().refresh()
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
            if (isFirstQ && reqSid) {
              const convId = reqSid
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

  // C3 追问链：卡片内的 followup 事件直接续问（不重建会话）
  useEffect(() => {
    const h = (e: Event): void => {
      const q = (e as CustomEvent).detail?.question as string | undefined
      if (q) void send(q)
    }
    window.addEventListener('tabletalk:followup', h as unknown as EventListener)
    return () => window.removeEventListener('tabletalk:followup', h as unknown as EventListener)
  }, [currentId, selectedTable])

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
    const reqSid = resolveSessionForConn(currentId)
    if (reqSid) touch(reqSid)
    try {
      await chatStream(
        {
          connection_id: currentId,
          // 保留 name:'clarify'——后端 _extract_clarify_answers 凭 system+name 识别澄清问答并 replay
          messages: msgs.map((m) => ({ role: m.role, content: m.content, name: m.name })),
          table: selectedTable,
          session_id: reqSid,
          title: useChat.getState().conversations.find((c) => c.id === reqSid)?.title ?? null,
          mode: 'report'
        },
        (ev: AiEvent) => {
          if ((ev as unknown as { type: string }).type === 'manifest') {
            const m = (ev as unknown as { manifest: Manifest }).manifest
            setTurns((t) => {
              const n = [...t]
              const last = n[n.length - 1]
              if (last?.role === 'ai') (last as unknown as { manifest: Manifest }).manifest = m
              return n
            })
            return
          }
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
            setTurns((tt) => {
              const n = [...tt]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = `${t('ws.planSections', { n: ev.sections.length })}${ev.sections.map((s) => s.title).join(' / ')}`
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
            setTurns((tt) => {
              const n = [...tt]
              const last = n[n.length - 1]
              if (last.role === 'ai') last.text = t('ws.reportReady')
              return n
            })
            toastMsg(t('ws.reportToast'))
          } else if (ev.type === 'done') {
            void useAuditSignal.getState().refresh()
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
    const labelKey = kind === 'explain' ? 'ws.analyzeExplain' : kind === 'optimize' ? 'ws.analyzeOptimize' : 'ws.analyzeRisk'
    const label = t(labelKey)
    setTurns((tt) => [...tt, { role: 'ai', text: t('ws.analyzeThinking', { label }), thinks: [t('ws.analyzeOf', { label })] }])
    try {
      const r = await selection({ connection_id: currentId, sql, kind })
      setTurns((tt) => {
        const n = [...tt]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = r.text
        return n
      })
    } catch (e) {
      setTurns((tt) => {
        const n = [...tt]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = `${t('ws.analyzeFail')}${(e as Error).message}`
        return n
      })
    }
  }

  async function exec(sql: string, confirm: boolean, question?: string, card?: AiCard, sessionId?: string | null): Promise<void> {
    if (!currentId) return
    setBusy(true)
    try {
      const r = await runQuery({ connectionId: currentId, sql, origin: 'ai', confirm, confirm_token: card?.confirm_token ?? null, session_id: sessionId ?? null })
      handleQueryResult(r, sql, question)
    } catch (e) {
      setTurns((tt) => {
        const n = [...tt]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = `${t('ws.execFail')}${(e as Error).message}`
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
        title: question ? `${t('ws.titleAsk')}${question}` : `ai · ${sql.split('\n')[0].slice(0, 42)}`,
        name: sql.slice(0, 20),
        headers: r.columns ?? [],
        types: r.types ?? [],
        rows,
        meta: `${r.truncated ? t('ws.truncated') : ''}${r.elapsed_ms}ms`.trim()
      })
      toastMsg(t('ws.resultToast'))
    } else if (r.verdict === 'review') {
      setTurns((tt) => {
        const n = [...tt]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = t('ws.reviewMsg', { n: r.preview_rows ?? '?' })
        return n
      })
    } else if (r.verdict === 'block') {
      setTurns((tt) => {
        const n = [...tt]
        const last = n[n.length - 1]
        if (last.role === 'ai') last.text = `${t('ws.blockMsg')}${r.reason}`
        return n
      })
    } else if (r.verdict === 'executed') {
      toastMsg(t('ws.execToast', { n: r.affected_rows ?? 0 }))
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
    <div className="ai-root">
      {/* 常驻对话面板 */}
      {(true) && (
        <div className="ai-panel">
          <div className="ai-panel-head">
            <span className={`ai-panel-state${busy ? ' busy' : ''}`}><i />{busy ? 'THINKING' : 'ONLINE'}</span>
            <span className="conv-title" title={activeConv?.title ?? t('chat.newConversation')}>
              {activeConv?.title ?? t('chat.newConversation')}
            </span>
            <span className="spacer" />
            <button className="ah-btn" title={t('ws.newConvTitle')} disabled={busy} onClick={newChat}>＋</button>
            <div className="ah-dd" ref={histDdRef}>
              <button
                className={`ah-btn${histOpen ? ' on' : ''}`}
                title={t('ws.historyTitle')}
                onClick={() => setHistOpen((o) => !o)}
              >
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" aria-hidden="true">
                  <circle cx="6" cy="6" r="4.6" />
                  <path d="M6 3.4 V6 L8.2 7.2" />
                </svg>
              </button>
              {histOpen && (
                <div className="ah-menu">
                  <div className="ah-mt mono">{t('ws.historyTitle')}</div>
                  {conversations.filter((c) => c.connId === currentId).map((c) => (
                    <button
                      key={c.id}
                      className={`ah-mi${c.id === activeId ? ' on' : ''}`}
                      onClick={() => switchTo(c.id, c)}
                    >
                      <span className="ah-t">{c.title ?? t('ws.unnamed')}</span>
                      <span className="ah-time mono">{relTime(c.updatedAt)}</span>
                    </button>
                  ))}
                  {conversations.filter((c) => c.connId === currentId).length === 0 && (
                    <div className="ah-none mono">{t('ws.noHistory')}</div>
                  )}
                </div>
              )}
            </div>
          </div>

          <div className="airail-scroll" ref={scrollRef}>
            {turns.length === 0 && (
              <div className="airail-hero">
                <div className="ah-top">
                  <span className="ah-brand mono">TABLETALK</span>
                  <span className="ah-live mono"><i />AI ONLINE</span>
                </div>
                <div className="ah-title">
                  {t('ws.hero1')}
                  <br />
                  <span className="ah-grad">{t('ws.hero2')}</span>
                </div>
                <div className="ah-sub">{t('ws.heroSub')}</div>
                <div className="ah-sugs">
                  {(dynamicSugs ?? SUGGESTIONS.map((k) => t(k))).map((q) => (
                    <button key={q} className="ah-sug" disabled={busy} onClick={() => void send(q as string)}>
                      <span className="as-ic mono">▸</span>
                      <span className="as-t">{q as string}</span>
                    </button>
                  ))}
                </div>
                <div className="ah-feats">
                  <span className="ah-feat"><i className="f-green" />{t('ws.featRead')}</span>
                  <span className="ah-feat"><i className="f-amber" />{t('ws.featWrite')}</span>
                  <span className="ah-feat"><i className="f-red" />{t('ws.featAudit')}</span>
                </div>
              </div>
            )}

            {turns.map((turn, i) =>
              turn.role === 'user' ? (
                <div className="m-u" key={i}>{turn.text}</div>
              ) : (
                <div className="m-a" key={i}>
                  {(turn.text || (turn.cards && turn.cards.length > 0) || turn.steps) && (
                    <div className="ai-id">
                      <span className="ai-avatar">◆</span>
                      <span className="ai-name">TABLETALK</span>
                    </div>
                  )}
                  {turn.isReport && !turn.clarify && (
                    <div className={`rpt-pill mono${turn.running ? ' live' : ''}`}>
                      <span className="rp-ic">▦</span>{t('ws.reportMode')}{turn.text ?? t('ws.generating')}
                    </div>
                  )}
                  {turn.clarify && (
                    <div className="clarify-box">
                      <div className="cl-q mono">{t('ws.clarifyTitle')}</div>
                      {turn.clarify.map((cq, ci) => (
                        <div key={ci} className="cl-line">{cq}</div>
                      ))}
                    </div>
                  )}
                  {turn.subtasks && turn.subtasks.length > 0 ? <SubtaskPanel subtasks={turn.subtasks} scene={turn.scene} /> : turn.steps && !(turn.text && (turn.text.includes('不处理此类问题') || turn.text.includes('不在处理范围') || turn.text.includes('引导'))) && <ThinkPanel steps={turn.steps} />}
                  {turn.manifest && <ManifestView manifest={turn.manifest} />}
                  {turn.text && !turn.isReport && (
                    <div className="ai-txt"><AiMd text={turn.text} /></div>
                  )}
                  {turn.text && turn.isReport && turn.clarify && null}
                  {turn.cards && turn.cards.map((c, ci) => (
                    <SqlCard
                      key={ci}
                      card={c}
                      question={turn.question ?? ''}
                      busy={busy}
                      pending={!!turn.pending}
                      dialect={connDialect}
                      connectionId={currentId}
                      sessionId={turn.sessionId ?? null}
                      onRun={(sql) => void exec(sql, false, turn.question)}
                      onConfirm={(sql, card) => void exec(sql, true, turn.question, card ?? c, turn.sessionId ?? null)}
                      onAnalyze={(kind, sql) => void analyze(kind, sql)}
                    />
                  ))}
                  {turn.running && (
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
              <div className="cl-prompt mono">{t('ws.clarifyPrompt')}</div>
              <div className="cl-q-line">{clarifyPending.q}</div>
              <div className="cl-ans">
                <input
                  className="a-field"
                  value={clarifyInput}
                  placeholder={t('ws.clarifyPlaceholder')}
                  onChange={(e) => setClarifyInput(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter') void answerClarify() }}
                  disabled={busy}
                  autoFocus
                />
                <button className="send" disabled={busy || !clarifyInput.trim()} onClick={() => void answerClarify()}>→</button>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 底部 AI 命令中心 */}
      <div className="ai-bar">
        {ctxTable && (
          <div className="ai-bar-top">
            <div className="ctx-chips">
              <span className="ctx-chip" title={t('ws.ctxTip')}>
                {t('ws.context')}{ctxTable}
                <button className="ctx-x" title={t('ws.removeContext')} onClick={() => selectTable(null)}>✕</button>
              </span>
            </div>
            {sugs.length > 0 && (
              <div className="sug-strip">
                {sugs.slice(0, 3).map((s, i) => (
                  <button key={i} className="sug-chip" disabled={busy} onClick={() => void send(s)} title={s}>
                    {s.length > 26 ? `${s.slice(0, 25)}…` : s}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
        <div className="ai-in-foot">
          <div className="ai-opt" title={t('ws.secPolicyTitle')}>
            <span className="ai-opt-l">{t('ws.secPolicy')}</span>
            <div className="ai-sel-dd" ref={secPolicyDdRef}>
              <button
                type="button"
                className={`ai-sel-btn${secPolicyOpen ? ' open' : ''}`}
                onClick={() => setSecPolicyOpen((o) => !o)}
              >
                <span>{trustLevel === 'all_confirm' ? t('ws.optAllConfirm') : t('ws.optReadAuto')}</span>
                <svg className="ai-sel-arrow" width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden="true">
                  <path d="M1.5 3 L4 5.5 L6.5 3" />
                </svg>
              </button>
              {secPolicyOpen && (
                <div className="ai-sel-menu">
                  <button
                    type="button"
                    className={`ai-sel-mi${trustLevel === 'read_auto' ? ' on' : ''}`}
                    onClick={() => { setTrustLevel('read_auto'); setSecPolicyOpen(false); toastMsg(t('ws.policyReadAuto')) }}
                  >
                    {t('ws.optReadAuto')}
                  </button>
                  <button
                    type="button"
                    className={`ai-sel-mi${trustLevel === 'all_confirm' ? ' on' : ''}`}
                    onClick={() => { setTrustLevel('all_confirm'); setSecPolicyOpen(false); toastMsg(t('ws.policyAllConfirm')) }}
                  >
                    {t('ws.optAllConfirm')}
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>
        <div className="ai-compose">
          <textarea
            ref={inputRef}
            className="compose-input"
            value={input}
            placeholder={t('ws.composePlaceholder')}
            onChange={(e) => { setInput(e.target.value); autoGrow() }}
            onInput={autoGrow}
            onFocus={() => setPanelOpen(true)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                void send()
              }
            }}
            disabled={busy}
            rows={2}
          />
          <button className="send" disabled={busy || !input.trim()} onClick={() => void send()} title={t('ws.send')}>→</button>
        </div>
      </div>
    </div>
  )
}
