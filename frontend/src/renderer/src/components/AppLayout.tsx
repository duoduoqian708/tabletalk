import { useEffect, useMemo, useRef, useState } from 'react'
import type { HealthStatus } from '@shared/types'
import { getRuntime } from '@renderer/api/client'
import { useConnections } from '@renderer/store/connections'
import { useResults } from '@renderer/store/results'
import { useSchema } from '@renderer/store/schema'
import { useUi, type MainView, type View } from '@renderer/store/ui'
import { useI18n } from '@renderer/store/i18n'
import { previewTable } from '@renderer/api/schema'
import { ConnectionMenu } from './ConnectionMenu'
import { ConnectionModal } from './ConnectionModal'
import { KbBuildGate } from './KbBuildGate'
import { Graph3D } from './Graph3D'
import { NodePopup } from './NodePopup'
import { TableDataView } from './TableDataView'
import { DataTable } from './DataTable'
import { ReportCard } from './ReportCard'
import { AiRail } from './AiRail'
import { KnowledgeReview } from './KnowledgeReview'
import { AuditPage } from './ModulePages'
import { SettingsDrawer } from './SettingsDrawer'
interface Props {
  health: HealthStatus | null
}

const TABS: { key: View; labelKey: string }[] = [
  { key: 'workspace', labelKey: 'nav.workspace' },
  { key: 'knowledge', labelKey: 'nav.knowledge' },
  { key: 'audit', labelKey: 'nav.securityAudit' }
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

  const active = tabs.find((t) => t.id === activeId) ?? null
  const activeReport = active?.kind === 'report' ? active.report : null

  // 沉浸式 3D 图谱数据
  const graphNodes = useMemo(
    () => schemaData?.tables.map((t) => ({ name: t.name, row_count: t.row_count, column_count: t.column_count })) ?? [],
    [schemaData]
  )
  const graphEdges = useMemo(
    () => schemaData?.foreign_keys.map((f) => ({ table: f.table, ref_table: f.ref_table })) ?? [],
    [schemaData]
  )
  const [nodeSel, setNodeSel] = useState<string | null>(null)
  const [nodeData, setNodeData] = useState<string | null>(null)
  const [nodePos, setNodePos] = useState<{ x: number; y: number } | null>(null)
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
        return t ? { rowCount: t.row_count, columnCount: t.column_count, fkCount: fk } : null
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

  // 当前展示的表名（顶栏上下文指示）
  const subject = active ? active.title.replace(new RegExp('^' + t('ws.titleAsk').replace(/[.*+?^${}()|[\]\\]/g, '\\$&')), '').replace(/^schema · /, '') : selectedTable ?? '—'

  async function useDemo(): Promise<void> {
    if (!rt) return
    await create({ name: '演示库', dialect: 'sqlite', file: `${rt.dataDir}/demo.db`, read_only: true })
  }

  const MainToggle = ({ target }: { target: MainView }): React.JSX.Element => (
    <button
      className={`ws-tb${mainView === target ? ' on' : ''}`}
      onClick={() => setMainView(target)}
      title={target === 'graph' ? t('nav.graphTitle') : t('nav.tableTitle')}
    >
      {target === 'graph' ? t('nav.graph') : t('nav.table')}
    </button>
  )

  return (
    <div className="app">
      <header className="appbar">
        <span className="wordmark">
          <span className="dot" />
          <b className="wm-t">tabletalk</b>
          <small>AI DATABASE TERMINAL</small>
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
        <div className="gw mono">
          <span className="gw-provider">{health?.ai_provider_name ?? '—'}</span>
          <span className="gw-divider" />
          <span>{health?.ai_model ?? '—'}</span>
          {health?.ai_mock_downgraded && (
            <span className="gw-warn" title={t('app.mockDowngradedTitle')}>· {t('app.mockDowngraded')}</span>
          )}
        </div>
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
                    <MainToggle target="graph" />
                    <MainToggle target="table" />
                    <span className="ws-crumb mono">{subject}</span>
                    <span className="spacer" />
                    {mainView === 'table' && tabs.length === 0 && (
                      <span className="ws-empty-hint">{t('app.emptyHint')}</span>
                    )}
                  </div>
                  <div className="ws-canvas-area">
                  {mainView === 'graph' ? (
                    <div className={`g3d-wrap${nodeData ? ' mini' : ''}`} ref={g3dWrapRef}>
                      <Graph3D
                        tables={graphNodes}
                        foreignKeys={graphEdges}
                        selectedName={nodeSel}
                        onSelectNode={(name, x, y) => { setNodeSel(name); setNodePos({ x, y }) }}
                        onClearSelection={() => { setNodeSel(null); setNodePos(null) }}
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
                            rowCount={nodeInfo.rowCount}
                            columnCount={nodeInfo.columnCount}
                            fkCount={nodeInfo.fkCount}
                            x={px}
                            y={py}
                            onClose={() => { setNodeSel(null); setNodePos(null) }}
                            onOpenData={() => { setNodeData(nodeSel); setNodeSel(null); setNodePos(null) }}
                          />
                        )
                      })()}
                    </div>
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
                  {nodeData && currentId && (
                    <TableDataView connId={currentId} table={nodeData} onClose={() => setNodeData(null)} />
                  )}
                  </div>
                </div>
              )}
              <div className="ws-splitter" onPointerDown={onSplitterDown} title={t('app.splitterTitle')} />
              <AiRail
                providerName={health?.ai_provider_name}
                modelLabel={health?.ai_model}
              />
            </section>
          </>
        ) : view === 'knowledge' || view === 'graph' ? (
          <KnowledgeReview />
        ) : (
          <AuditPage />
        )}
      </main>

      {/* 航电状态栏：gate 状态 + 连接 + AI 网关 */}
      <footer className="statusline deck-status">
        <span className="guard"><span className="led" />gate&nbsp;:&nbsp;{health?.gate ?? '…'}</span>
        <span className="mid">
          <span>conn&nbsp;:&nbsp;<b>{list.find((c) => c.id === currentId)?.name ?? '—'}</b></span>
          <span>dialect&nbsp;:&nbsp;<b>{list.find((c) => c.id === currentId)?.dialect ?? '—'}</b></span>
          <span>provider&nbsp;:&nbsp;<b>{health?.ai_provider_name ?? '—'}</b></span>
          <span>model&nbsp;:&nbsp;<b>{health?.ai_model ?? '—'}</b></span>
          {health?.ai_mock_downgraded && <span className="warn">{t('app.mockDowngraded')}</span>}
        </span>
      </footer>

      <ConnectionModal open={modalOpen} onClose={() => setModalOpen(false)} />
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} onNewConnection={() => { setSettingsOpen(false); setModalOpen(true) }} />
      <KbBuildGate />
    </div>
  )
}
