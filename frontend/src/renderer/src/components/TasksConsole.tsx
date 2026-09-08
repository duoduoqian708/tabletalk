import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { useConnections } from '@renderer/store/connections'
import { useI18n } from '@renderer/store/i18n'
import { toastMsg } from '@renderer/utils/toast'
import { fmtDT } from '@renderer/lib/timefmt'
import { CloseBtn } from './ui/buttons'
import { IconX, IconSend, IconRefresh } from './ui/icons'
import {
  listTasks, deployTask, patchTask, getTaskScript, deleteTask, runTask, cancelTask, testScript, taskRuns, getReport,
  agentChatStream, type JobTask, type TaskRun, type TaskProposal, type TaskPlan,
} from '@renderer/api/tasks'
import { listResults, getResult } from '@renderer/api/results'

/* ═══════════════════════════════════════════════
   定时任务控制台 v3（左右分栏）
   - 左：任务列表 = jobs/*.py（声明头驱动），点击选中
   - 右：选中任务的执行记录 + 产出物预览（报表 markdown 渲染）
   - 新建/编辑 = AI 对话（需求确认单 → 脚本提案[沙箱实测] → 部署门禁），产物永远是 .py
   ═══════════════════════════════════════════════ */

interface Bubble { role: 'user' | 'assistant'; content: string; thinking?: string; proposal?: TaskProposal | null; plan?: TaskPlan | null }

const errMsg = (e: unknown): string => (e as { message?: string })?.message ?? String(e)

export function TasksConsole(): React.JSX.Element {
  const connList = useConnections((s) => s.list)
  const currentId = useConnections((s) => s.currentId)
  const { t } = useI18n()

  const [tasks, setTasks] = useState<JobTask[]>([])
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<string>('') // 左侧选中的任务名
  const [runs, setRuns] = useState<Record<string, TaskRun[]>>({})
  const [runsLoading, setRunsLoading] = useState(false)
  // 删除二次确认（平台风 modal，替代 window.confirm）
  const [rmTask, setRmTask] = useState<JobTask | null>(null)

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

  // 选中任务 → 右侧加载其执行记录（每次点击都拉新，保证最新）
  const selectTask = useCallback(async (name: string) => {
    setSelected(name)
    setRunsLoading(true)
    try { const d = await taskRuns(name); setRuns((r) => ({ ...r, [name]: d.runs || [] })) }
    catch { setRuns((r) => ({ ...r, [name]: [] })) }
    finally { setRunsLoading(false) }
  }, [])

  async function refreshRuns(): Promise<void> {
    if (!selected) return
    setRunsLoading(true)
    try { const d = await taskRuns(selected); setRuns((r) => ({ ...r, [selected]: d.runs || [] })) }
    catch { /* ignore */ }
    finally { setRunsLoading(false) }
  }

  async function toggle(task: JobTask): Promise<void> {
    try { await patchTask(task.name, { enabled: !task.enabled }); void load() }
    catch (e) { toastMsg(t('tk.toastFailed', { msg: errMsg(e) })) }
  }

  async function runNow(task: JobTask): Promise<void> {
    setBusy(true)
    try {
      await runTask(task.name); toastMsg(t('tk.toastRun')); void load()
      if (selected === task.name) void refreshRuns()
    }
    catch (e) { toastMsg(t('tk.toastFailed', { msg: errMsg(e) })) }
    finally { setBusy(false) }
  }

  async function remove(task: JobTask): Promise<void> {
    try {
      await deleteTask(task.name)
      toastMsg(t('tk.toastDeleted'))
      setRuns((r) => { const n = { ...r }; delete n[task.name]; return n })
      if (selected === task.name) setSelected('')
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

  async function testProposal(scriptOverride?: string): Promise<void> {
    const p = lastProposal
    if (!p || agentBusy) return
    const script = scriptOverride ?? p.script
    setBusy(true); setTest(null)
    try {
      const connId = connList.find((c) => c.id === currentId)?.name ?? ''
      const res = await testScript(script, connId || p.connection)
      setTest(res)
    } catch (e) { setTest({ ok: false, summary: errMsg(e) }) }
    finally { setBusy(false) }
  }

  // 提案卡内手动编辑脚本：写回最新 proposal（后续测试/部署都以编辑后版本为准）
  function applyProposalScript(script: string): void {
    setBubbles((b) => {
      for (let i = b.length - 1; i >= 0; i--) {
        if (b[i].proposal) {
          const n = [...b]
          n[i] = { ...n[i], proposal: { ...n[i].proposal!, script } }
          return n
        }
      }
      return b
    })
    setTest(null)
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

  async function sendAgent(text?: string, opts?: { confirmPlan?: boolean }): Promise<void> {
    const q = (text ?? agentInput).trim()
    if (!q || agentBusy) return
    const next: Bubble[] = [...bubbles, { role: 'user', content: q }]
    setBubbles([...next, { role: 'assistant', content: '', thinking: '' }])
    setAgentInput(''); setAgentBusy(true); setTest(null); setConflict(false)
    const ctrl = new AbortController()
    abortRef.current = ctrl
    // ReAct 调试循环可能多轮，留 420s+（后端墙钟护栏 420s 兜底）
    const timer = setTimeout(() => ctrl.abort(), 420_000)
    try {
      const result = await agentChatStream(
        // 回放历史：为 plan/proposal 气泡附加协议标记——后端据此做 PLAN 硬闸的启发式自动确认
        next.map((b) => ({
          role: b.role,
          content: b.content
            + (b.plan ? '\n\n[需求登记单已输出]' : '')
            + (b.proposal ? '\n\n[脚本提案已交付]' : ''),
        })),
        currentId || undefined,
        ctrl.signal,
        (chunk) => setBubbles((b) => {
          const last = b[b.length - 1]
          if (last?.role === 'assistant' && !last.proposal && !last.plan) return [...b.slice(0, -1), { ...last, content: last.content + chunk }]
          return b
        }),
        editName || undefined,
        (reason) => setBubbles((b) => {
          const last = b[b.length - 1]
          if (last?.role === 'assistant' && !last.proposal && !last.plan && !last.content) return [...b.slice(0, -1), { ...last, thinking: (last.thinking ?? '') + reason }]
          return b
        }),
        sessionId,
        opts?.confirmPlan,
      )
      setBubbles((b) => {
        const last = b[b.length - 1]
        if (last?.role === 'assistant') return [...b.slice(0, -1), { ...last, content: result.reply || last.content, proposal: result.proposal, plan: result.plan }]
        return b
      })
    } catch (e) {
      const msg = e instanceof DOMException && e.name === 'AbortError' ? t('tk.timeoutAborted') : errMsg(e)
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
    const deployedName = lastProposal.name || editName || t('tk.taskFallback')
    try {
      await deployTask({
        name: deployedName,
        cron: lastProposal.cron || '',
        connection: lastProposal.connection || connName,
        script: lastProposal.script,
        overwrite: overwrite || isEdit,
      })
      toastMsg(t('tk.toastDeployed'))
      closeAgent()
      // 刷新列表并选中刚部署的任务——右侧立即可见，无需手动刷新页面
      await load()
      void selectTask(deployedName)
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

      {/* ═══ 左右分栏：任务列表 | 执行记录 ═══ */}
      <div className="tk-body">
        {/* 左：任务列表（点击选中 → 右侧显示执行记录） */}
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
            const isSel = task.name === selected
            return (
              <div key={task.name} className={`tk-card${isSel ? ' open' : ''}`}>
                <div className="tk-card-head" onClick={() => void selectTask(task.name)}>
                  <span className="tk-glyph">◷</span>
                  <span className="tk-name">{task.name}</span>
                  {task.system && <span className="tk-freq tk-sys">{t('tk.sysTag')}</span>}
                  <span className="tk-cron mono">{task.cron}</span>
                  {task.connection && <span className="tk-conn mono">{task.connection}</span>}
                  <span className={`tk-status ${on ? 'on' : 'off'}`}><span className="tk-status-dot" />{on ? t('tk.statOn') : t('tk.statOff')}</span>
                  <span className="spacer" />
                  <button
                    className={`mini-btn ${on ? 'dang' : ''}`}
                    onClick={(e) => { e.stopPropagation(); void toggle(task) }}
                  >{on ? t('tk.onOff') : t('tk.offOn')}</button>
                  {task.running ? (
                    <button className="mini-btn dang" onClick={(e) => { e.stopPropagation(); void cancelTask(task.name).then(() => { toastMsg(t('tk.cancelled')); void load() }) }} title="取消">■ {t('tk.cancel')}</button>
                  ) : (
                    <button className="mini-btn set" onClick={(e) => { e.stopPropagation(); void runNow(task) }} disabled={busy}>▶ {t('tk.run')}</button>
                  )}
                  <button className="mini-btn" onClick={(e) => { e.stopPropagation(); void openEdit(task) }}>{t('tk.edit')}</button>
                  <button className="mini-btn dang" onClick={(e) => { e.stopPropagation(); setRmTask(task) }}>{t('tk.delete')}</button>
                </div>
                <div className="tk-card-body">
                  <div className="tk-meta mono">
                    <span>{task.friendly}</span>
                    <span className="tk-meta-sep">·</span>
                    <span>{t('tk.lastRun')} <b className={task.last_run ? '' : 'never'}>{task.last_run ? fmtDT(task.last_run) : t('tk.never')}</b></span>
                    <span className="tk-meta-sep">·</span>
                    <span className={task.next_run ? 'tk-nextrun' : 'tk-nextrun never'}>{t('tk.nextRun')} {task.next_run ? fmtDT(task.next_run) : '—'}</span>
                    {task.last_status === 'error' && (
                      <>
                        <span className="tk-meta-sep">·</span>
                        <span style={{ color: 'var(--red, #d33)', display: 'inline-flex', alignItems: 'center' }}><IconX size={9} /></span>
                      </>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>

        {/* 右：选中任务的执行记录 + 产出物 */}
        <RunsPanel
          task={tasks.find((x) => x.name === selected) || null}
          runs={selected ? runs[selected] : undefined}
          loading={runsLoading}
          t={t}
          onRefresh={() => void refreshRuns()}
        />
      </div>

      {/* ═══ AI 新建/编辑任务对话 ═══ */}
      {agentOpen && (
        <div className="tk-mask" onClick={closeAgent}>
          <div className="tk-dialog agent" onClick={(e) => e.stopPropagation()}>
            <div className="tk-dlg-head">
              {isEditMode ? `${t('tk.edit')} · ${editName}` : t('tk.dialogTitle')}
              <CloseBtn className="kbh-close" title={t('common.close')} onClick={closeAgent} />
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
                      <span className="tk-attach-size mono">{t('tk.lineCount', { n: editScript.split('\n').length })}</span>
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
                    {b.plan && !b.proposal && <PlanCard plan={b.plan} t={t} busy={agentBusy} onConfirm={() => void sendAgent(t('tk.planConfirm'), { confirmPlan: true })} />}
                    {b.proposal && <ProposalCard proposal={b.proposal} t={t} busy={busy} conflict={conflict}
                      onDeploy={() => void deployProposal(conflict)} onOverwrite={() => void deployProposal(true)}
                      onContinue={() => setConflict(false)} onTest={testProposal} onScriptChange={applyProposalScript}
                      testResult={testState} />}
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
                <button className="tk-send" disabled={agentBusy || !agentInput.trim()} onClick={() => void sendAgent()} title={t('tk.send')}><IconSend size={14} /></button>
              </div>
            </div>
          </div>
        </div>
      )}

      {rmTask && (
        <div className="modal-mask open" onClick={(e) => { if (e.target === e.currentTarget) setRmTask(null) }}>
          <div className="modal confirm" style={{ width: 400 }}>
            <div className="mh">
              <span className="t">⚠ {t('settings.del.title')}</span>
              <CloseBtn className="close" title={t('common.close')} onClick={() => setRmTask(null)} />
            </div>
            <div className="mb">
              <div className="del-text">{t('tk.delTask', { name: rmTask.name })}</div>
              {rmTask.system && <div className="del-note">{t('tk.delTaskSystem')}</div>}
            </div>
            <div className="mf">
              <button className="btn" onClick={() => setRmTask(null)}>{t('common.cancel')}</button>
              <button className="btn danger" onClick={() => { const job = rmTask; setRmTask(null); void remove(job) }}>{t('common.delete')}</button>
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
  onTest?: (script?: string) => void
  onScriptChange?: (script: string) => void
  testResult?: { ok: boolean; summary?: string; output?: string } | null
}): React.JSX.Element {
  const { proposal, t, busy, conflict } = props
  const [draft, setDraft] = useState(proposal.script)
  const [editing, setEditing] = useState(false)
  useEffect(() => { setDraft(proposal.script); setEditing(false) }, [proposal])
  const dirty = draft !== proposal.script
  return (
    <div className="tk-proposal">
      <div className="tk-proposal-head">
        <span className="tk-proposal-name">{proposal.name || t('tk.taskFallback')}</span>
        {proposal.cron && <span className="tk-cron mono">{proposal.cron}</span>}
        {proposal.connection && <span className="tk-conn mono">{proposal.connection}</span>}
        {proposal.untested && <span className="tk-freq dang" title={t('tk.untestedWarn')}>{t('tk.untestedWarn')}</span>}
        {proposal.plan_skipped && <span className="tk-freq plan-warn" title={t('tk.planSkippedWarn')}>{t('tk.planSkippedWarn')}</span>}
        <span className="spacer" />
        <span className="tk-nextrun mono">{proposal.next_run ? fmtDT(proposal.next_run) : ''}</span>
      </div>
      <div className="tk-proposal-summary">{proposal.summary}</div>
      {editing ? (
        <textarea
          className="tk-script-code"
          style={{ width: '100%', minHeight: 260, resize: 'vertical', whiteSpace: 'pre', fontFamily: 'var(--mono, monospace)', fontSize: 11 }}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
        />
      ) : (
        <pre className="tk-script-code">{dirty ? draft : proposal.script}</pre>
      )}
      {props.testResult && (
        <div style={{ padding: '8px 12px', borderTop: '1px solid var(--line)' }}>
          <span className="mono" style={{ fontSize: 11, color: props.testResult.ok ? 'var(--ok, green)' : 'var(--danger, red)' }}>
            {t('tk.testResult', { summary: props.testResult.summary || (props.testResult.ok ? t('tk.testPass') : t('tk.testFail')) })}
          </span>
          {props.testResult.output && <pre style={{ maxHeight: 120, overflowY: 'auto', fontSize: 10, color: 'var(--ink-dim)', marginTop: 4 }}>{props.testResult.output.slice(0, 800)}</pre>}
        </div>
      )}
      <div className="tk-proposal-actions">
        <span className="mono" style={{ fontSize: 11, color: 'var(--ink-faint)', marginRight: 'auto', alignSelf: 'center' }}>{t('tk.proposalTitle')}</span>
        {editing ? (
          <>
            <button className="btn gho" disabled={busy} onClick={() => { setDraft(proposal.script); setEditing(false) }}>{t('tk.cancelEdit')}</button>
            <button className="btn pri" disabled={busy || !dirty || !props.onScriptChange} onClick={() => { props.onScriptChange?.(draft); setEditing(false) }}>{t('tk.applyEdit')}</button>
          </>
        ) : (
          <>
            <button className="btn gho" disabled={busy} onClick={() => setEditing(true)}>{t('tk.edit')}</button>
            {props.onTest && <button className="btn gho" disabled={busy} onClick={() => props.onTest?.(dirty ? draft : undefined)}>{t('tk.test')}</button>}
          </>
        )}
        <button className="btn gho" disabled={busy} onClick={props.onContinue}>{t('tk.keepEdit')}</button>
        {!conflict
          ? <button className="btn pri" disabled={busy} onClick={props.onDeploy}>{t('tk.deploy')}</button>
          : <button className="btn pri" disabled={busy} onClick={props.onOverwrite}>{t('tk.overwrite')}</button>}
      </div>
    </div>
  )
}

function PlanCard(props: {
  plan: TaskPlan
  t: (k: string, vars?: Record<string, string | number>) => string
  busy: boolean
  onConfirm: () => void
}): React.JSX.Element {
  const { plan, t } = props
  return (
    <div className="tk-proposal">
      <div className="tk-proposal-head">
        <span className="tk-proposal-name">{t('tk.planTitle')}</span>
        {plan.cron && <span className="tk-cron mono">{plan.cron}</span>}
        {plan.cron_friendly && <span className="tk-freq">{plan.cron_friendly}</span>}
        {plan.connection && <span className="tk-conn mono">{plan.connection}</span>}
        <span className={`tk-freq ${plan.mode === 'write' ? 'dang' : ''}`}>{plan.mode === 'write' ? t('tk.planModeWrite') : t('tk.planModeRead')}</span>
      </div>
      <div className="tk-proposal-summary">{plan.output}</div>
      <div style={{ padding: '8px 12px', borderTop: '1px solid var(--line)', display: 'grid', gap: 6, fontSize: 12 }}>
        {plan.tables.length > 0 && (
          <div><span style={{ color: 'var(--ink-faint)' }}>{t('tk.planTables')}：</span><span className="mono">{plan.tables.join('、')}</span></div>
        )}
        {plan.assumptions.length > 0 && (
          <div><span style={{ color: 'var(--ink-faint)' }}>{t('tk.planAssumptions')}：</span>
            <ul style={{ margin: '2px 0 0 16px', padding: 0 }}>{plan.assumptions.map((a, i) => <li key={i}>{a}</li>)}</ul>
          </div>
        )}
        {plan.questions.length > 0 && (
          <div>
            <span style={{ color: 'var(--warn, #b8860b)' }}>{t('tk.planQuestions')}：</span>
            <ul style={{ margin: '2px 0 0 16px', padding: 0 }}>{plan.questions.map((q, i) => <li key={i} style={{ marginBottom: 2 }}>{q}</li>)}</ul>
            <div style={{ marginTop: 4, color: 'var(--ink-faint)', fontSize: 11 }}>{t('tk.answerHint')}</div>
          </div>
        )}
      </div>
      {plan.questions.length === 0 && (
        <div className="tk-proposal-actions">
          <span className="mono" style={{ fontSize: 11, color: 'var(--ink-faint)', marginRight: 'auto', alignSelf: 'center' }}>{t('tk.planTitle')}</span>
          <button className="btn pri" disabled={props.busy} onClick={props.onConfirm}>{t('tk.planConfirm')}</button>
        </div>
      )}
    </div>
  )
}

/* ═══ 右侧：执行记录面板（含产出物预览） ═══ */

type TT = (k: string, vars?: Record<string, string | number>) => string

function RunsPanel(props: {
  task: JobTask | null
  runs?: TaskRun[]
  loading: boolean
  t: TT
  onRefresh: () => void
}): React.JSX.Element {
  const { task, runs, loading } = props
  const [openRun, setOpenRun] = useState<number | null>(null)
  useEffect(() => { setOpenRun(null) }, [task?.name])

  if (!task) {
    return (
      <div className="tk-runs-panel">
        <div className="tk-runs-empty big">
          <div className="tk-empty-g">◷</div>
          <div>{props.t('tk.selectTaskHint')}</div>
        </div>
      </div>
    )
  }
  const list = runs ?? []
  return (
    <div className="tk-runs-panel">
      <div className="tk-runs-head">
        <span className="tk-glyph">◷</span>
        <span className="tk-name">{task.name}</span>
        <span className="tk-freq">{task.friendly}</span>
        <span className="spacer" />
        <button className="mini-btn" onClick={props.onRefresh} disabled={loading}><IconRefresh size={10} /> {props.t('tk.refresh')}</button>
      </div>
      <div className="tk-runs-body">
        {loading && list.length === 0 && <div className="tk-runs-empty">{props.t('common.loading')}</div>}
        {!loading && list.length === 0 && (
          <div className="tk-runs-empty">
            <div>{props.t('tk.never')}</div>
            <div className="tk-empty-sub">{props.t('tk.runsHint')}</div>
          </div>
        )}
        {list.map((r) => (
          <div key={r.id} className={`tk-run-card${openRun === r.id ? ' open' : ''}`}>
            <div className="tk-run-row" onClick={() => setOpenRun(openRun === r.id ? null : r.id)}>
              <span className={`tk-run-st ${r.status}`}>{r.status}</span>
              <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.summary || '—'}</span>
              {r.artifacts && r.artifacts.length > 0 && (
                <span className="tk-conn mono" title={r.artifacts.join('\n')}>📄 {r.artifacts.length}</span>
              )}
              <span className="tk-run-at mono">{fmtDT(r.started_at)}</span>
              <span className="tk-chev">{openRun === r.id ? '▾' : '▸'}</span>
            </div>
            {openRun === r.id && (
              <div className="tk-run-detail">
                {r.output && r.output.trim()
                  ? <pre className="tk-run-output">{r.output}</pre>
                  : <div className="tk-run-noout">{props.t('tk.noOutput')}</div>}
                {r.artifacts && r.artifacts.length > 0 && (
                  <div className="tk-art-list">
                    <span className="tk-art-label">{props.t('tk.artifactsLabel')}</span>
                    {r.artifacts.map((f) => (
                      <ReportPreview key={f} file={f} jobName={task.name} runId={r.id} t={props.t} />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function ReportPreview(props: { file: string; jobName: string; runId: number; t: TT }): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const [content, setContent] = useState('')
  const [err, setErr] = useState('')
  useEffect(() => {
    if (!open || content || err) return
    let on = true
    // 优先走统一报告库（results.db，任务运行收编产物）；查不到（旧数据）回退 reports/ 文件端点
    ;(async () => {
      try {
        const list = await listResults('job', props.jobName, 100)
        const hit = (list.results || []).find(
          (it) => it.meta?.file === props.file && Number((it.meta as { run_id?: number })?.run_id) === props.runId,
        )
        if (hit) {
          const d = await getResult(hit.id)
          if (on) setContent(d.result.content)
          return
        }
      } catch { /* 落到文件端点回退 */ }
      try {
        const d = await getReport(props.file)
        if (on) setContent(d.content)
      } catch (e) {
        if (on) setErr(errMsg(e))
      }
    })()
    return () => { on = false }
  }, [open, content, err, props.file, props.jobName, props.runId])
  return (
    <div className="tk-art-item">
      <button className="tk-art-chip" onClick={() => setOpen(!open)}>
        📄 {props.file} <span className="tk-chev">{open ? '▾' : '▸'}</span>
      </button>
      {open && (err
        ? <div className="tk-run-noout">{err}</div>
        : !content
          ? <div className="tk-run-noout">{props.t('common.loading')}</div>
          : <div className="tk-report-view"><ReactMarkdown>{content}</ReactMarkdown></div>
      )}
    </div>
  )
}