import { useRef, useState } from 'react'
import type { HealthStatus } from '@shared/types'
import { getRuntime } from '@renderer/api/client'
import { useConnections } from '@renderer/store/connections'
import { useResults } from '@renderer/store/results'
import { useUi, type View } from '@renderer/store/ui'
import { ConnectionMenu } from './ConnectionMenu'
import { ConnectionModal } from './ConnectionModal'
import { SchemaRail } from './SchemaRail'
import { DataTable } from './DataTable'
import { ReportCard } from './ReportCard'
import { AiRail } from './AiRail'
import { KnowledgeReview } from './KnowledgeReview'
import { GatePage } from './ModulePages'
import { AuditPage } from './ModulePages'
import { GraphPage } from './ModulePages'
import { SettingsDrawer } from './SettingsDrawer'

interface Props {
  health: HealthStatus | null
}

const TABS: { key: View; label: string }[] = [
  { key: 'workspace', label: '工作台' },
  { key: 'gate', label: '安全闸门' },
  { key: 'knowledge', label: '知识库' },
  { key: 'graph', label: '知识图谱' },
  { key: 'audit', label: '审计' }
]

/** 分隔拖拽手柄：mousedown 后跟随鼠标移动回调 delta。 */
function DragHandle({ onDrag }: { onDrag: (dx: number) => void }): React.JSX.Element {
  const startX = useRef(0)

  return (
    <div
      className="drag-h"
      onMouseDown={(e) => {
        e.preventDefault()
        startX.current = e.clientX
        document.body.classList.add('resizing')
        const move = (ev: MouseEvent): void => {
          onDrag(ev.clientX - startX.current)
          startX.current = ev.clientX
        }
        const up = (): void => {
          document.body.classList.remove('resizing')
          document.removeEventListener('mousemove', move)
          document.removeEventListener('mouseup', up)
        }
        document.addEventListener('mousemove', move)
        document.addEventListener('mouseup', up)
      }}
    />
  )
}

export function AppLayout({ health }: Props): React.JSX.Element {
  const [schemaOn, setSchemaOn] = useState(true)
  const [schemaW, setSchemaW] = useState(170)
  const [aiW, setAiW] = useState(450)
  const [modalOpen, setModalOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const list = useConnections((s) => s.list)
  const create = useConnections((s) => s.create)
  const result = useResults((s) => s.result)
  const report = useResults((s) => s.report)
  const view = useUi((s) => s.view)
  const setView = useUi((s) => s.setView)
  const rt = getRuntime()

  // 结果来源主题：schema 点表 → 表名；AI 查询 → 问题主题；报告 → 报告标题
  const subject = report
    ? report.title
    : result
      ? result.title.startsWith('schema · ')
        ? `表 ${result.title.slice('schema · '.length)}`
        : result.title
      : '—'

  async function useDemo(): Promise<void> {
    if (!rt) return
    await create({ name: '演示库', dialect: 'sqlite', file: `${rt.dataDir}/demo.db`, read_only: true })
  }

  return (
    <div className="app">
      <header className="appbar">
        <span className="wordmark">
          <span className="dot" />
          DATUM <small>DB</small>
        </span>
        <ConnectionMenu onNew={() => setModalOpen(true)} />
        <nav className="nav">
          {TABS.map((t) => (
            <button
              key={t.key}
              className={`tab${view === t.key ? ' on' : ''}`}
              onClick={() => setView(t.key)}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <span className="spacer" />
        <div className="gw mono">
          gateway&nbsp;:&nbsp;<b style={{ color: 'var(--ink-dim)', fontWeight: 500 }}>{health?.ai_effective_provider ?? health?.ai_provider ?? '—'}</b>
          {health?.ai_mock_downgraded && (
            <span style={{ marginLeft: 6, color: '#c8871e', fontSize: 11 }} title="cloud 无 key，已静默降级为 mock">· mock降级</span>
          )}
        </div>
        <button className="sys-btn" onClick={() => setSettingsOpen(true)}>
          <span className="gear">⚙</span>系统设置
        </button>
      </header>

      <main className="stage" key={view}>
        {view === 'workspace' ? (
          <>
            <SchemaRail open={schemaOn} width={schemaW} />
            <DragHandle onDrag={(dx) => setSchemaW((w) => Math.min(320, Math.max(96, w + dx)))} />
            <section className="workspace">
              <div className="ws-ctx">
                <button className={`iconbtn${schemaOn ? ' on' : ''}`} onClick={() => setSchemaOn(!schemaOn)}>
                  ⌘ schema
                </button>
                <span className="arrow">→</span>
                <span className="pill subj" title={subject}>{subject}</span>
              </div>

              {list.length === 0 ? (
                <div className="onboarding">
                  <div className="card">
                    <div className="kicker">no connection</div>
                    <div className="big">连接一个数据库开始</div>
                    <div className="hint">
                      用演示库最快：内置 14 表电商数据，含 FK 关联，可直接构建知识图谱。
                      <br />
                      或新建一个 MySQL / PostgreSQL 连接。
                    </div>
                    <button className="primary" onClick={useDemo}>使用演示库</button>
                  </div>
                </div>
              ) : report ? (
                <ReportCard report={report} />
              ) : (
                <DataTable />
              )}
            </section>
            <DragHandle onDrag={(dx) => setAiW((w) => Math.min(720, Math.max(320, w - dx)))} />
            <AiRail width={aiW} provider={health?.ai_effective_provider ?? health?.ai_provider} />
          </>
        ) : view === 'gate' ? (
          <GatePage health={health} />
        ) : view === 'knowledge' ? (
          <KnowledgeReview />
        ) : view === 'graph' ? (
          <GraphPage />
        ) : (
          <AuditPage />
        )}
      </main>

      <footer className="statusline mono">
        <span className="guard">gate&nbsp;:&nbsp;{health?.gate ?? '…'}</span>
        <span className="mid">
          <span>model&nbsp;:&nbsp;<b>{health?.ai_model ?? '—'}</b>&nbsp;<span style={{ color: 'var(--ink-dim)' }}>({health?.ai_effective_provider ?? health?.ai_provider})</span></span>
          <span>dialect&nbsp;:&nbsp;<b>{health?.dialects?.join(' / ') ?? '—'}</b></span>
        </span>
      </footer>

      <ConnectionModal open={modalOpen} onClose={() => setModalOpen(false)} />
      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} onNewConnection={() => { setSettingsOpen(false); setModalOpen(true) }} />
    </div>
  )
}
