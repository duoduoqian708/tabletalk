import { useEffect, useMemo, useRef, useState } from 'react'
import type { HealthStatus } from '@shared/types'
import { getRuntime } from '@renderer/api/client'
import { tags as fetchTags } from '@renderer/api/knowledge'
import { useConnections } from '@renderer/store/connections'
import { useResults } from '@renderer/store/results'
import { useSchema } from '@renderer/store/schema'
import { useUi, type View } from '@renderer/store/ui'
import { useI18n } from '@renderer/store/i18n'
import { previewTable } from '@renderer/api/schema'
import { useAuditSignal } from '@renderer/store/auditSignal'
import { ConnectionMenu } from './ConnectionMenu'
import { ConnectionModal } from './ConnectionModal'
import { KbBuildGate } from './KbBuildGate'
import { KbBuildConfirmDialog } from './KbBuildConfirmDialog'
import { KbReviewModal } from './KbReviewModal'
import { Graph3D } from './Graph3D'
import { GraphSearch } from './GraphSearch'
import { TagBar } from './TagBar'
import { NodePopup } from './NodePopup'
import { TableDataView } from './TableDataView'
import { DataTable } from './DataTable'
import { ReportCard } from './ReportCard'
import { AiRail } from './AiRail'
import { KnowledgeReview } from './KnowledgeReview'
import { AuditPage } from './ModulePages'
import { TasksConsole } from './TasksConsole'
import { CostDashboard } from './CostDashboard'
import { SettingsDrawer } from './SettingsDrawer'
import { AuditBadge } from './AuditBadge'
import { LoginDialog } from './LoginDialog'
import { setLoginRuntime } from '@renderer/hooks/useBootstrap'
interface Props {
  health: HealthStatus | null
}

const TABS: { key: View; labelKey: string }[] = [
  { key: 'workspace', labelKey: 'nav.workspace' },
  { key: 'knowledge', labelKey: 'nav.knowledge' },
  { key: 'audit', labelKey: 'nav.securityAudit' },
  { key: 'tasks', labelKey: 'nav.tasks' },
  { key: 'cost', labelKey: 'nav.cost' },
]

/** 表格视图顶部：关联表快速跳转（替代旧 40px 左缘微条，水平化融入工具栏）。 */
function RelStrip(): React.JSX.Element | null {
  const { t: tr } = useI18n()
  const currentId = useConnections((s) => s.currentId)
  const schema = useSchema((s) => s.data)
  const active = useResults((s) => s.tabs.find((t) => t.id === s.activeId) ?? null)
  const selectTable = useSchema((s) => s.selectTable)
  const push = useResults((s) => s.push)

  const tableName = active?.kind === 'data' ? active.name : null
  const related = tableName && schema
    ? Array.from(new Set(
        schema.foreign_keys
          .filter((fk) => fk.table === tableName || fk.ref_table === tableName)
          .map((fk) => (fk.table === tableName ? fk.ref_table : fk.table))
      ))
    : []
  if (related.length === 0) return null

  async function jump(t: string): Promise<void> {
    if (!currentId) return
    selectTable(t)
    try {
      const p = await previewTable(currentId, t)
      push({
        title: `schema · ${t}`,
        name: t,
        headers: p.columns,
        types: p.types,
        rows: p.rows,
        meta: tr('app.structPreview')
      })
    } catch {
      /* 预览失败静默 */
    }
  }

  return (
    <div className="rel-strip">
      <span className="rel-label">RELATED</span>
      {related.map((t) => (
        <button key={t} className="rel-chip" title={tr('app.viewData', { name: t })} onClick={() => void jump(t)}>
          {t}
        </button>
      ))}
    </div>
  )
}

export function AppLayout({ health }: Props): React.JSX.Element {
  const [modalOpen, setModalOpen] = useState(false)
  const [editingConnId, setEditingConnId] = useState<string | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const list = useConnections((s) => s.list)
  const currentId = useConnections((s) => s.currentId)
  const create = useConnections((s) => s.create)
  const tabs = useResults((s) => s.tabs)
  const activeId = useResults((s) => s.activeId)
  const closeTab = useResults((s) => s.closeTab)
  const activate = useResults((s) => s.activate)
  const selectedTable = useSchema((s) => s.selectedTable)
  const schemaData = useSchema((s) => s.data)
  const view = useUi((s) => s.view)
  const setView = useUi((s) => s.setView)
  const mainView = useUi((s) => s.mainView)
  const setMainView = useUi((s) => s.setMainView)
  const { t } = useI18n()
  const rt = getRuntime()
  const [loginIsInitial, setLoginIsInitial] = useState(false)
  const showLogin = !rt?.token

  const active = tabs.find((t) => t.id === activeId) ?? null
  const activeReport = active?.kind === 'report' ? active.report : null

  // 沉浸式 3D 图谱数据
  const graphNodes = useMemo(
    () => schemaData?.tables.map((t) => ({ name: t.name, row_count: t.row_count, column_count: t.column_count, kind: t.kind as 'table' | 'view' })) ?? [],
    [schemaData]
  )
  const graphEdges = useMemo(
    () => schemaData?.foreign_keys.map((f) => ({ table: f.table, ref_table: f.ref_table })) ?? [],
    [schemaData]
  )
  const [nodeSel, setNodeSel] = useState<string | null>(null)
  const [nodeData, setNodeData] = useState<string | null>(null)
  const [closingNode, setClosingNode] = useState<string | null>(null)
  const [nodePos, setNodePos] = useState<{ x: number; y: number } | null>(null)
  /** 工作台标签过滤：选中的标签名 → 非匹配表变暗 */
  const [dimmedTag, setDimmedTag] = useState<string | null>(null)
  const [tagTableMap, setTagTableMap] = useState<Record<string, string[]>>({})
  useEffect(() => {
    if (!currentId || !dimmedTag) { setTagTableMap({}); return }
    let alive = true
    void fetchTags(currentId)
      .then((r) => { if (alive) setTagTableMap(r.tables ?? {}) })
      .catch(() => { if (alive) setTagTableMap({}) })
    return () => { alive = false }
  }, [currentId, dimmedTag])
  const dimmedTables = useMemo(() => {
    if (!dimmedTag) return undefined
    const allTables = new Set(graphNodes.map((n) => n.name))
    const matching = new Set<string>()
    for (const [tbl, tags] of Object.entries(tagTableMap)) {
      if (tags.includes(dimmedTag)) matching.add(tbl)
    }
    return new Set([...allTables].filter((n) => !matching.has(n)))
  }, [dimmedTag, tagTableMap, graphNodes])
  const handleCloseTable = (): void => {
    if (!nodeData) return
    setClosingNode(nodeData)
    setNodeData(null)
    window.setTimeout(() => setClosingNode(null), 420)
  }
  const displayNode = nodeData || closingNode
  const isTableClosing = !!closingNode
  // 启停由外层控制，避免按钮随 g3d-wrap 位移动画
  const [graphPaused, setGraphPaused] = useState<boolean>(() => {
    try { return sessionStorage.getItem('tabletalk-graph-paused') === '1' } catch { return false }
  })
  useEffect(() => {
    try { sessionStorage.setItem('tabletalk-graph-paused', graphPaused ? '1' : '0') } catch {}
  }, [graphPaused])
  // 左右分栏：右轨宽度（px），默认 1/4
  const wsRef = useRef<HTMLDivElement>(null)
  const g3dWrapRef = useRef<HTMLDivElement>(null)
  const [railW, setRailW] = useState<number>(() =>
    Math.max(280, Math.round((typeof window !== 'undefined' ? window.innerWidth : 1440) * 0.25))
  )
  const dragRef = useRef<{ startX: number; startW: number } | null>(null)
  const onSplitterDown = (e: React.PointerEvent): void => {
    e.preventDefault()
    dragRef.current = { startX: e.clientX, startW: railW }
    document.body.classList.add('ws-resizing')
    const move = (ev: PointerEvent): void => {
      if (!dragRef.current || !wsRef.current) return
      const rect = wsRef.current.getBoundingClientRect()
      const dx = ev.clientX - dragRef.current.startX
      let w = dragRef.current.startW - dx
      const min = Math.round(rect.width * 0.18)
      const max = Math.round(rect.width * 0.5)
      w = Math.max(min, Math.min(max, w))
      setRailW(w)
    }
    const up = (): void => {
      dragRef.current = null
      document.body.classList.remove('ws-resizing')
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }
  const nodeInfo = nodeSel
    ? (() => {
        const t = schemaData?.tables.find((x) => x.name === nodeSel)
        const fk = schemaData?.foreign_keys.filter((f) => f.table === nodeSel || f.ref_table === nodeSel).length ?? 0
        return t ? { rowCount: t.row_count, columnCount: t.column_count, fkCount: fk, kind: t.kind as 'table' | 'view' } : null
      })()
    : null

  // 新标签到达（AI 查询 / 预览 / 报告）→ 自动切到表格视图；标签清空 → 回图谱
  useEffect(() => {
    if (activeId) setMainView('table')
  }, [activeId, setMainView])
  useEffect(() => {
    if (tabs.length === 0 && mainView === 'table') setMainView('graph')
  }, [tabs.length, mainView, setMainView])

  // ⌘1 图谱 / ⌘2 表格 秒切
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if ((e.metaKey || e.ctrlKey) && e.key === '1') {
        e.preventDefault()
        setMainView('graph')
      }
      if ((e.metaKey || e.ctrlKey) && e.key === '2') {
        e.preventDefault()
        setMainView('table')
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setMainView])

  // C5 可追溯：表 chip 跳转星图定位（与 D1 搜索定位同链路）
  useEffect(() => {
    const h = (e: Event): void => {
      const tbl = (e as CustomEvent).detail?.table as string | undefined
      if (!tbl) return
      setView('workspace')
      setMainView('graph')
      setNodeSel(tbl)
      // 触发 Graph3D 内部 flyTo（通过 do-locate 事件）
      window.dispatchEvent(new CustomEvent('tabletalk:do-locate', { detail: { table: tbl } }))
    }
    window.addEventListener('tabletalk:locate', h as EventListener)
    return () => window.removeEventListener('tabletalk:locate', h as EventListener)
  }, [setView, setMainView])

  // 安全徽章轮询启停（随应用生命周期）
  useEffect(() => {
    useAuditSignal.getState().start()
    return () => useAuditSignal.getState().stop()
  }, [])

  // 当前展示的表名（顶栏上下文指示）
  const subject = active ? active.title.replace(new RegExp('^' + t('ws.titleAsk').replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), '').replace(/^schema · /, '') : selectedTable ?? '—'

  async function useDemo(): Promise<void> {
    if (!rt) return
    await create({ name: t('conn.demoName'), dialect: 'sqlite', file: `${rt.dataDir}/demo.db`, read_only: true })
  }

  return (
    <div className="app">
      <header className="appbar">
        <span className="wordmark">
          <span className="dot" />
          <b className="wm-t">tabletalk</b>
          <small>{t('ui.tagline')}</small>
        </span>
        <ConnectionMenu onNew={() => setModalOpen(true)} />
        <nav className="nav">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              className={`tab${view === tab.key ? ' on' : ''}`}
              onClick={() => setView(tab.key)}
            >
              {t(tab.labelKey)}
            </button>
          ))}
        </nav>
        <span className="cur-table mono" title={t('ui.currentView')}>{subject}</span>
        <span className="spacer" />
        <button className="sys-btn" title={t('settings.title')} onClick={() => setSettingsOpen(true)}>
          <span className="gear">⚙</span>
        </button>
      </header>

      <main className="stage" key={view}>
        {view === 'workspace' ? (
          <>
            <section
              className="workspace"
              ref={wsRef}
              style={{ ['--rail-w' as string]: `${railW}px` } as React.CSSProperties}
            >
              {list.length === 0 ? (
                <div className="ws-left">
                  <div className="onboarding">
                    <div className="card">
                      <div className="kicker">no connection</div>
                      <div className="big">{t('app.onboarding.title')}</div>
                      <div className="hint">
                        {t('app.onboarding.hint1')}
                        <br />
                        {t('app.onboarding.hint2')}
                      </div>
                      <button className="primary" onClick={useDemo}>{t('app.onboarding.useDemo')}</button>
                    </div>
                  </div>
                </div>
              ) : (
                <div className="ws-left">
                  <div className="ws-toolbar">
                    <GraphSearch
                      tables={graphNodes}
                      onPick={(name) => { setMainView('graph'); window.dispatchEvent(new CustomEvent('tabletalk:do-locate', { detail: { table: name } })) }}
                    />
                    <TagBar connId={currentId} onSelect={setDimmedTag} />
                    <span className="spacer" />
                    {mainView === 'table' && tabs.length === 0 && (
                      <span className="ws-empty-hint">{t('app.emptyHint')}</span>
                    )}
                  </div>
                  <div className="ws-canvas-area">
                  {mainView === 'graph' ? (
                    <>
                      <div className={`g3d-wrap${nodeData ? ' mini' : ''}`} ref={g3dWrapRef}>
                        <Graph3D
                          tables={graphNodes}
                          foreignKeys={graphEdges}
                          selectedName={nodeSel}
                          mini={!!nodeData}
                          dimmedTables={dimmedTables}
                          paused={graphPaused}
                          onTogglePause={() => setGraphPaused((v) => !v)}
                          onSelectNode={(name, x, y) => { setNodeSel(name); setNodePos({ x, y }) }}
                          onClearSelection={() => { setNodeSel(null); setNodePos(null) }}
                          onOpenData={(name) => { setNodeData(name); setNodeSel(name); setNodePos(null) }}
                        />
                        {nodeSel && nodeInfo && nodePos && !nodeData && (() => {
                          const POP_W = 296
                          const POP_H = 196
                          const wrap = g3dWrapRef.current
                          const w = wrap ? wrap.clientWidth : 0
                          const h = wrap ? wrap.clientHeight : 0
                          const px = Math.min(Math.max(8, nodePos.x + 16), Math.max(8, w - POP_W - 8))
                          const py = Math.min(Math.max(8, nodePos.y - 24), Math.max(8, h - POP_H - 8))
                          return (
                            <NodePopup
                              table={nodeSel}
                              kind={nodeInfo.kind}
                              rowCount={nodeInfo.rowCount}
                              columnCount={nodeInfo.columnCount}
                              fkCount={nodeInfo.fkCount}
                              x={px}
                              y={py}
                              onClose={() => { setNodeSel(null); setNodePos(null) }}
                              onOpenData={() => { setNodeData(nodeSel!); setNodePos(null) }}
                            />
                          )
                        })()}
                      </div>
                      <button
                        className={`g3d-pause g3d-pause-external ${graphPaused ? 'is-paused' : ''}`}
                        title={graphPaused ? t('graph.resume') : t('graph.pause')}
                        onClick={() => setGraphPaused((v) => !v)}
                        aria-label={graphPaused ? t('graph.resume') : t('graph.pause')}
                      >
                        {graphPaused ? '▶' : '⏸'}
                      </button>
                    </>
                  ) : (
                    <div className="ws-data">
                      {tabs.length > 0 && (
                        <div className="ws-tabbar">
                          {tabs.map((tab) => (
                            <span key={tab.id} className={`ws-tab${tab.id === activeId ? ' on' : ''}`} onClick={() => activate(tab.id)}>
                              <span className="ws-tab-ic">{tab.kind === 'report' ? '▤' : '◈'}</span>
                              <span className="ws-tab-t">{tab.kind === 'report' ? `${t('app.tab.report')} · ${tab.title}` : tab.name ? `${t('app.tab.data')} · ${tab.name}` : tab.title}</span>
                              <button
                                className="ws-tab-x"
                                title={t('common.close')}
                                onClick={(e) => {
                                  e.stopPropagation()
                                  closeTab(tab.id)
                                }}
                              >✕</button>
                            </span>
                          ))}
                        </div>
                      )}
                      <RelStrip />
                      <div className="ws-data-body">
                        {activeReport ? (
                          <ReportCard report={activeReport} />
                        ) : active ? (
                          <DataTable />
                        ) : (
                          <div className="ws-table-empty">
                            <div className="kicker">no data</div>
                            <div className="hint">{t('app.tableEmptyHint')}</div>
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                  {(displayNode) && currentId && (
                    <TableDataView connId={currentId} table={displayNode} closing={isTableClosing} onClose={handleCloseTable} />
                  )}
                  </div>
                </div>
              )}
              <div className="ws-splitter" onPointerDown={onSplitterDown} title={t('app.splitterTitle')} />
              <AiRail />
            </section>
          </>
        ) : view === 'knowledge' || view === 'graph' ? (
          <KnowledgeReview />
        ) : view === 'tasks' ? (
          <TasksConsole />
        ) : view === 'cost' ? (
          <CostDashboard />
        ) : (
          <AuditPage />
        )}
      </main>

      {/* 航电状态栏：gate 状态 + 连接 + AI 网关 */}
      <footer className="statusline deck-status">
        <span className="mid">
          <span className="mid-item">
            <span className="led" />
            <b>{list.find((c) => c.id === currentId)?.name ?? '—'}</b>
            <span className="mid-sep">|</span>
            <b>{health?.ai_provider_name ?? '—'}-{health?.ai_model ?? '—'}</b>
          </span>
        </span>
          <AuditBadge />
      </footer>

      <ConnectionModal open={modalOpen} editId={editingConnId} onClose={() => { setModalOpen(false); setEditingConnId(null) }} />
      <SettingsDrawer
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        // 编辑/新建弹窗盖在抽屉上（modal z-index 高于抽屉）：保存后回到设置页
        onNewConnection={() => { setEditingConnId(null); setModalOpen(true) }}
        onEditConnection={(id) => { setEditingConnId(id); setModalOpen(true) }}
      />
      <KbBuildGate />
      <KbBuildConfirmDialog />
      <KbReviewModal />
      <LoginDialog
        open={showLogin}
        isInitial={loginIsInitial}
        onLogin={async (username, password) => {
          try {
            const r = await fetch('/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) })
            const j = await r.json()
            if (!r.ok) return { ok: false, error: j.detail || t('login.fail') }
            setLoginIsInitial(!!j.user?.is_initial)
            // 取 dataDir 用于后续
            const b = await (await fetch('/api/v1/bootstrap')).json().catch(() => ({ dataDir: '' }))
            setLoginRuntime(j.token, b.dataDir || '')
            // 强制刷新以使 getRuntime 生效
            window.location.reload()
            return { ok: true, is_initial: !!j.user?.is_initial }
          } catch (e) {
            return { ok: false, error: (e as Error).message }
          }
        }}
        onChangePassword={async (username, oldPwd, newPwd) => {
          try {
            const r = await fetch('/api/v1/auth/change-password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, old_password: oldPwd, new_password: newPwd }) })
            const j = await r.json()
            if (!r.ok) return { ok: false, error: j.detail || t('login.changeFail') }
            setLoginRuntime(j.token, (await (await fetch('/api/v1/bootstrap')).json().catch(() => ({ dataDir: '' }))).dataDir || '')
            setLoginIsInitial(false)
            return { ok: true }
          } catch (e) {
            return { ok: false, error: (e as Error).message }
          }
        }}
      />
    </div>
  )
}
