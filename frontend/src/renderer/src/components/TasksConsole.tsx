import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useI18n } from '@renderer/store/i18n'
import { toastMsg } from '@renderer/utils/toast'
import { fmtDT } from '@renderer/lib/timefmt'
import {
  listTasks, deployTask, patchTask, getTaskScript, deleteTask, runTask, cancelTask, testScript, taskRuns,
  agentChatStream, type JobTask, type TaskRun, type TaskProposal,
} from '@renderer/api/tasks'

/* ═══════════════════════════════════════════════
   定时任务控制台 v2（全脚本化）
   - 任务列表 = jobs/*.py（声明头驱动）
   - 新建 = AI 对话（澄清 → 脚本提案 → 部署），产物永远是 .py
   - 每条：启停 / 运行 / 编辑脚本 / 删除 + 展开看执行记录
   ═══════════════════════════════════════════════ */

interface Bubble { role: 'user' | 'assistant'; content: string; thinking?: string; proposal?: TaskProposal | null }

const errMsg = (e: unknown): string => (e as { message?: string })?.message ?? String(e)

export function TasksConsole(): React.JSX.Element {
  const connList = useConnections((s) => s.list)
  const currentId = useConnections((s) => s.currentId)
  const { t } = useI18n()

  const [tasks, setTasks] = useState<JobTask[]>([])
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [runs, setRuns] = useState<Record<string, TaskRun[]>>({})

  const load = useCallback(async () => {
    try { const d = await listTasks(); setTasks(d.tasks || []) } catch { setTasks([]) }
  }, [])
  useEffect(() => { void load() }, [load])

  const stats = useMemo(() => {
    return {
      total: tasks.length,
      on: tasks.filter((x) => x.enabled).length,
      off: tasks.filter((x) => !x.enabled).length,
      sys: tasks.filter((x) => x.system).length,
    }
  }, [tasks])

  async function toggleExpand(name: string): Promise<void> {
    setExpanded((s) => { const n = new Set(s); if (n.has(name)) n.delete(name); else n.add(name); return n })
    if (!runs[name]) {
      try { const d = await taskRuns(name); setRuns((r) => ({ ...r, [name]: d.runs || [] })) } catch { /* ignore */ }
    }
  }

  async function toggle(task: JobTask): Promise<void> {
    try { await patchTask(task.name, { enabled: !task.enabled }); void load() }
    catch (e) { toastMsg(t('tk.toastFailed', { msg: errMsg(e) })) }
  }

  async function runNow(task: JobTask): Promise<void> {
    setBusy(true)
    try { await runTask(task.name); toastMsg(t('tk.toastRun')); void load() }
    catch (e) { toastMsg(t('tk.toastFailed', { msg: errMsg(e) })) }
    finally { setBusy(false) }
  }

  async function remove(task: JobTask): Promise<void> {
    if (!window.confirm(`${t('tk.delete')}「${task.name}」？`)) return
    try {
      await deleteTask(task.name)
      toastMsg(t('tk.toastDeleted'))
      setRuns((r) => { const n = { ...r }; delete n[task.name]; return n })
      void load()
    } catch (e) { toastMsg(t('tk.toastFailed', { msg: errMsg(e) })) }
  }

  // ── AI 新建对话 ──
  const [agentOpen, setAgentOpen] = useState(false)
  const [sessionId, setSessionId] = useState('') // 每次打开对话框生成一次，跨轮复用候选工作区
  const [bubbles, setBubbles] = useState<Bubble[]>([])
  const [agentInput, setAgentInput] = useState('')
  const [agentBusy, setAgentBusy] = useState(false)
  const [conflict, setConflict] = useState(false)
  const chatRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  useEffect(() => { chatRef.current?.scrollTo({ top: chatRef.current.scrollHeight, behavior: 'smooth' }) }, [bubbles, agentBusy])

  // ── 编辑模式状态 ──
  const [editName, setEditName] = useState('')
  const [editCron, setEditCron] = useState('')
  const [editScript, setEditScript] = useState('')
  const isEditMode = editName !== '' // 有任务名 = 编辑模式
  const newSession = (): string =>
    typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `s${Date.now()}`

  async function openEdit(task: JobTask): Promise<void> {
    try {
      const d = await getTaskScript(task.name)
      setEditName(task.name)
      setEditCron(d.cron)
      setEditScript(d.script)
      setSessionId(newSession())
      setBubbles([])
      setAgentBusy(false)
      setConflict(false)
      abortRef.current?.abort()
      setAgentOpen(true)
    } catch (e) { toastMsg(t('tk.toastFailed', { msg: errMsg(e) })) }
  }

  async function saveEditMeta(): Promise<void> {
    if (!editName) return
    setBusy(true)
    try {
      await patchTask(editName, { cron: editCron })
      toastMsg(t('tk.toastSaved'))
    } catch (e) { toastMsg(t('tk.toastFailed', { msg: errMsg(e) })) }
    finally { setBusy(false); void load() }
  }

  function closeAgent(): void {
    setAgentOpen(false); setBubbles([]); setAgentBusy(false); setEditName(''); setEditCron(''); setEditScript(''); setSessionId(''); abortRef.current?.abort()
  }

  const [testState, setTest] = useState<{ ok: boolean; summary?: string; output?: string } | null>(null)

  async function testProposal(): Promise<void> {
    if (!lastProposal || agentBusy) return
    setBusy(true); setTest(null)
    try {
      const connId = connList.find((c) => c.id === currentId)?.name ?? ''
      const res = await testScript(lastProposal.script, connId || lastProposal.connection)
      setTest(res)
    } catch (e) { setTest({ ok: false, summary: errMsg(e) }) }
    finally { setBusy(false) }
  }

  const lastProposal = useMemo(() => {
    for (let i = bubbles.length - 1; i >= 0; i--) if (bubbles[i].proposal) return bubbles[i].proposal!
    return null
  }, [bubbles])

  // 建议哑片（对话开始时 ↑ 输入区上方）：点一下就发
  const QUICK_EXAMPLES = [
    '每天早上 9 点，把昨天的订单按地区汇总，写进 order_daily 表',
    '每 30 分钟检查低于安全库存的商品，写入库存预警表',
    '每周一早上生成上周销售报表并总结',
  ]

  const agentInputRef = useRef<HTMLTextAreaElement>(null)
  function autoGrowAgent(): void {
    const el = agentInputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(180, Math.max(48, el.scrollHeight)) + 'px'
  }

  async function sendAgent(text?: string): Promise<void> {
    const q = (text ?? agentInput).trim()
    if (!q || agentBusy) return
    const next: Bubble[] = [...bubbles, { role: 'user', content: q }]
    setBubbles([...next, { role: 'assistant', content: '', thinking: '' }])
    setAgentInput(''); setAgentBusy(true)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    // ReAct 调试循环可能多轮，留 420s+（后端墙钟护栏 420s 兜底）
    const timer = setTimeout(() => ctrl.abort(), 420_000)
    try {
      const result = await agentChatStream(
        next.map((b) => ({ role: b.role, content: b.content })),
        currentId || undefined,
        ctrl.signal,
        (chunk) => setBubbles((b) => {
          const last = b[b.length - 1]
          if (last?.role === 'assistant' && !last.proposal) return [...b.slice(0, -1), { ...last, content: last.content + chunk }]
          return b
        }),
        editName || undefined,
        (reason) => setBubbles((b) => {
          const last = b[b.length - 1]
          if (last?.role === 'assistant' && !last.proposal && !last.content) return [...b.slice(0, -1), { ...last, thinking: (last.thinking ?? '') + reason }]
          return b
        }),
        sessionId,
      )
      setBubbles((b) => {
        const last = b[b.length - 1]
        if (last?.role === 'assistant') return [...b.slice(0, -1), { ...last, content: result.reply || last.content, proposal: result.proposal }]
        return b
      })
    } catch (e) {
      const msg = e instanceof DOMException && e.name === 'AbortError' ? '请求已超时或被取消' : errMsg(e)
      setBubbles((b) => [...b, { role: 'assistant', content: `⚠ ${msg}` }])
    } finally { setAgentBusy(false); abortRef.current = null; clearTimeout(timer) }
  }

  function cancelAgent(): void {
    abortRef.current?.abort()
    setAgentBusy(false)
  }

  async function deployProposal(overwrite: boolean): Promise<void> {
    if (!lastProposal) return
    setBusy(true)
    const connName = connList.find((c) => c.id === currentId)?.name ?? ''
    const isEdit = !!editName
    try {
      await deployTask({
        name: lastProposal.name || editName || '任务',
        cron: lastProposal.cron || '',
        connection: lastProposal.connection || connName,
        script: lastProposal.script,
        overwrite: overwrite || isEdit,
      })
      toastMsg(t('tk.toastDeployed'))
      closeAgent()
    } catch (e) {
      if (String(errMsg(e)).includes('409')) setConflict(true)
      else toastMsg(t('tk.toastFailed', { msg: errMsg(e) }))
    } finally { setBusy(false) }
  }

  return (
    <div className="tk-page">
      {/* ═══ 顶部 ═══ */}
      <div className="tk-top">
        <div>
          <h1 className="tk-title">{t('tk.title')}</h1>
          <p className="tk-sub">{t('tk.sub')}</p>
        </div>
        <div className="tk-top-r">
          <button className="tk-new" onClick={() => { closeAgent(); setSessionId(newSession()); setAgentOpen(true) }}>{t('tk.newBtn')}</button>
        </div>
      </div>

      {/* ═══ 统计条 ═══ */}
      <div className="tk-stats">
        <div className="tk-stat"><span className="tk-stat-n">{stats.total}</span><span className="tk-stat-l">{t('tk.statTotal')}</span></div>
        <div className="tk-stat"><span className="tk-stat-n ok">{stats.on}</span><span className="tk-stat-l">{t('tk.statOn')}</span></div>
        <div className="tk-stat"><span className="tk-stat-n off">{stats.off}</span><span className="tk-stat-l">{t('tk.statOff')}</span></div>
        <div className="tk-stat"><span className="tk-stat-n dim">{stats.sys}</span><span className="tk-stat-l">{t('tk.statSystem')}</span></div>
      </div>

      {/* ═══ 任务列表 ═══ */}
      <div className="tk-list">
        {tasks.length === 0 && (
          <div className="tk-empty">
            <div className="tk-empty-g">◷</div>
            <div>{t('tk.emptyTitle')}</div>
            <div className="tk-empty-sub">{t('tk.emptySub')}</div>
          </div>
        )}
        {tasks.map((task) => {
          const on = task.enabled
          const open = expanded.has(task.name)
          const taskRunsList = runs[task.name]
          return (
            <div key={task.name} className={`tk-card${open ? ' open' : ''}`}>
              <div className="tk-card-head" onClick={() => void toggleExpand(task.name)}>
                <span className="tk-glyph">◷</span>
                <span className="tk-name">{task.name}</span>
                {task.system && <span className="tk-freq tk-sys">{t('tk.sysTag')}</span>}
                <span className="tk-cron mono">{task.cron}</span>
                <span className="tk-freq">{task.friendly}</span>
                {task.connection && <span className="tk-conn mono">{task.connection}</span>}
                <span className={`tk-status ${on ? 'on' : 'off'}`}><span className="tk-status-dot" />{on ? t('tk.statOn') : t('tk.statOff')}</span>
                <span className="spacer" />
                <button
                  className={`mini-btn ${on ? 'dang' : ''}`}
                  onClick={(e) => { e.stopPropagation(); void toggle(task) }}
                >{on ? t('tk.onOff') : t('tk.offOn')}</button>
                {task.running ? (
                  <button className="mini-btn dang" onClick={(e) => { e.stopPropagation(); void cancelTask(task.name).then(() => { toastMsg('已取消'); void load() }) }} title="取消">■ {t('tk.cancel')}</button>
                ) : (
                  <button className="mini-btn set" onClick={(e) => { e.stopPropagation(); void runNow(task) }} disabled={busy}>▶ {t('tk.run')}</button>
                )}
                <button className="mini-btn" onClick={(e) => { e.stopPropagation(); void openEdit(task) }}>{t('tk.edit')}</button>
                <button className="mini-btn dang" onClick={(e) => { e.stopPropagation(); void remove(task) }}>{t('tk.delete')}</button>
                <span className="tk-chev">{open ? '▾' : '▸'}</span>
              </div>
              {open && (
                <div className="tk-card-body">
                  <div className="tk-meta mono">
                    <span>{t('tk.lastRun')} <b className={task.last_run ? '' : 'never'}>{task.last_run ? fmtDT(task.last_run) : t('tk.never')}</b></span>
                    <span className="tk-meta-sep">·</span>
                    <span className={task.next_run ? 'tk-nextrun' : 'tk-nextrun never'}>{t('tk.nextRun')} {task.next_run ? fmtDT(task.next_run) : '—'}</span>
                    {task.last_summary && <span className="tk-meta-sep">·</span>}
                    {task.last_summary && <span className="mono" style={{ color: 'var(--ink-dim)' }}>{task.last_summary}</span>}
                  </div>
                  <div className="tk-runs">
                    {taskRunsList === undefined && <span className="tk-run-at">{t('common.loading')}</span>}
                    {taskRunsList !== undefined && taskRunsList.length === 0 && <span style={{ fontSize: 11, color: 'var(--ink-faint)' }}>{t('tk.never')}</span>}
                    {taskRunsList?.map((r) => (
                      <div key={r.id} className="tk-run-row">
                        <span className={`tk-run-st ${r.status}`}>{r.status}</span>
                        <span style={{ flex: 1 }}>{r.summary || '—'}</span>
                        <span className="tk-run-at mono">{fmtDT(r.started_at)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* ═══ AI 新建/编辑任务对话 ═══ */}
      {agentOpen && (
        <div className="tk-mask" onClick={closeAgent}>
          <div className="tk-dialog agent" onClick={(e) => e.stopPropagation()}>
            <div className="tk-dlg-head">
              {isEditMode ? `${t('tk.edit')} · ${editName}` : t('tk.dialogTitle')}
              <button className="kbh-close" onClick={closeAgent}>✕</button>
            </div>

            {/* 编辑模式：名称 + cron 独立编辑栏 */}
            {isEditMode && (
              <div className="tk-dlg-body" style={{ paddingBottom: 0 }}>
                <div style={{ display: 'flex', gap: 10, alignItems: 'flex-end' }}>
                  <label className="tk-field" style={{ flex: 2 }}>
                    <span>{t('tk.editNameLabel')}</span>
                    <input value={editName} onChange={(e) => setEditName(e.target.value)} />
                  </label>
                  <label className="tk-field" style={{ flex: 1 }}>
                    <span>{t('tk.editCronLabel')}</span>
                    <input className="mono" value={editCron} onChange={(e) => setEditCron(e.target.value)} />
                  </label>
                  <button className="btn pri" style={{ height: 34, marginBottom: 1 }} disabled={busy} onClick={() => void saveEditMeta()}>{t('tk.save')}</button>
                </div>
              </div>
            )}

            {/* 对话区 */}
            <div className="tk-dlg-body">
              <div className="tk-chat" ref={chatRef}>
                {/* 编辑模式：脚本附件卡 */}
                {isEditMode && (
                  <div className="tk-attach">
                    <div className="tk-attach-head">
                      <span className="tk-attach-icon">📄</span>
                      <span className="tk-attach-name">{editName}.py</span>
                      <span className="tk-attach-size mono">{editScript.split('\n').length} 行</span>
                    </div>
                    <pre className="tk-attach-code">{editScript}</pre>
                  </div>
                )}
                {bubbles.map((b, i) => (
                  <div key={i}>
                    {b.role === 'user' ? (
                      <div className="tk-bub user">{b.content}</div>
                    ) : (
                      <div className="tk-bub ai">
                        {b.thinking ? <div className="tk-think">💭 {b.thinking}</div> : null}
                        {b.content}
                      </div>
                    )}
                    {b.proposal && <ProposalCard proposal={b.proposal} t={t} busy={busy} conflict={conflict}
                      onDeploy={() => void deployProposal(conflict)} onOverwrite={() => void deployProposal(true)}
                      onContinue={() => setConflict(false)} onTest={testProposal} testResult={testState} />}
                  </div>
                ))}
                {agentBusy && (
                <div className="tk-bub ai" style={{ color: 'var(--ink-faint)' }}>
                  {t('tk.agentThinking')}
                  <button className="btn gho" style={{ marginLeft: 8, fontSize: 11 }} onClick={cancelAgent}>{t('common.cancel')}</button>
                </div>
              )}
              </div>
            </div>

            {/* 输入区 */}
            <div className="tk-dlg-body" style={{ borderTop: '1px solid var(--line)' }}>
              {conflict && (
                <div className="tk-conflict">
                  <span>{t('tk.conflictNote')}</span>
                  <button className="mini-btn dang" onClick={() => void deployProposal(true)}>{t('tk.overwrite')}</button>
                  <button className="mini-btn" onClick={() => setConflict(false)}>{t('common.cancel')}</button>
                </div>
              )}
              {/* 新建模式：快捷 chips */}
              {!isEditMode && bubbles.length === 0 && (
                <div className="tk-chips">
                  {QUICK_EXAMPLES.map((e) => (
                    <button key={e} className="tk-chip" disabled={agentBusy} onClick={() => void sendAgent(e)}>{e}</button>
                  ))}
                </div>
              )}
              <div className="ai-compose" style={{ border: '1px solid var(--line-strong)', borderRadius: 'var(--r-md)', padding: 8, background: 'var(--void-2)' }}>
                <textarea
                  ref={agentInputRef}
                  className="compose-input"
                  rows={2}
                  value={agentInput}
                  placeholder={isEditMode ? t('tk.editHint') : t('tk.dialogHint')}
                  onChange={(e) => { setAgentInput(e.target.value); autoGrowAgent() }}
                  onInput={autoGrowAgent}
                  onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void sendAgent() } }}
                  disabled={agentBusy}
                />
                <button className="tk-send" disabled={agentBusy || !agentInput.trim()} onClick={() => void sendAgent()} title={t('tk.send')}>→</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function ProposalCard(props: {
  proposal: TaskProposal
  t: (k: string, vars?: Record<string, string | number>) => string
  busy: boolean
  conflict: boolean
  onDeploy: () => void
  onOverwrite: () => void
  onContinue: () => void
  onTest?: () => void
  testResult?: { ok: boolean; summary?: string; output?: string } | null
}): React.JSX.Element {
  const { proposal, t, busy, conflict } = props
  return (
    <div className="tk-proposal">
      <div className="tk-proposal-head">
        <span className="tk-proposal-name">{proposal.name || '任务'}</span>
        {proposal.cron && <span className="tk-cron mono">{proposal.cron}</span>}
        {proposal.connection && <span className="tk-conn mono">{proposal.connection}</span>}
        <span className="spacer" />
        <span className="tk-nextrun mono">{proposal.next_run ? fmtDT(proposal.next_run) : ''}</span>
      </div>
      <div className="tk-proposal-summary">{proposal.summary}</div>
      <pre className="tk-script-code">{proposal.script}</pre>
      {props.testResult && (
        <div style={{ padding: '8px 12px', borderTop: '1px solid var(--line)' }}>
          <span className="mono" style={{ fontSize: 11, color: 'var(--ink-faint)' }}>测试结果：{props.testResult.summary || (props.testResult.ok ? '成功' : '失败')}</span>
          {props.testResult.output && <pre style={{ maxHeight: 120, overflowY: 'auto', fontSize: 10, color: 'var(--ink-dim)', marginTop: 4 }}>{props.testResult.output.slice(0, 800)}</pre>}
        </div>
      )}
      <div className="tk-proposal-actions">
        <span className="mono" style={{ fontSize: 11, color: 'var(--ink-faint)', marginRight: 'auto', alignSelf: 'center' }}>{t('tk.proposalTitle')}</span>
        {props.onTest && <button className="btn gho" disabled={busy} onClick={props.onTest}>测试</button>}
        <button className="btn gho" disabled={busy} onClick={props.onContinue}>{t('tk.keepEdit')}</button>
        {!conflict
          ? <button className="btn pri" disabled={busy} onClick={props.onDeploy}>{t('tk.deploy')}</button>
          : <button className="btn pri" disabled={busy} onClick={props.onOverwrite}>{t('tk.overwrite')}</button>}
      </div>
    </div>
  )
}