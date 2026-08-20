import { useEffect, useState } from 'react'
import type { HealthStatus, SidecarRuntime } from '@shared/types'
import { request } from '@renderer/api/client'
import { useBootstrap } from '@renderer/hooks/useBootstrap'
import { useI18n } from '@renderer/store/i18n'
import { useConnections } from '@renderer/store/connections'
import { useSchema } from '@renderer/store/schema'
import { AppLayout } from '@renderer/components/AppLayout'

function useHealth(rt: SidecarRuntime | null): HealthStatus | null {
  const [health, setHealth] = useState<HealthStatus | null>(null)

  useEffect(() => {
    if (!rt) return
    let alive = true
    void request<HealthStatus>('/api/v1/health')
      .then((h) => alive && setHealth(h))
      .catch(() => alive && setHealth(null))
    return () => {
      alive = false
    }
  }, [rt])

  return health
}

function Boot({ title, hint }: { title: string; hint: string }): React.JSX.Element {
  return (
    <div className="boot">
      <header className="boot-bar">
        <span className="wordmark">
          <span className="dot" />
          tabletalk <small>AI DATABASE TERMINAL</small>
        </span>
      </header>
      <main className="boot-body">
        <div className="boot-card">
          <div className="kicker">{title}</div>
          <div className="spinner">
            <i />
            <i />
            <i />
          </div>
          <p className="hint">{hint}</p>
        </div>
      </main>
      <footer className="statusline mono">
        <span className="guard">gate&nbsp;:&nbsp;…</span>
      </footer>
    </div>
  )
}

export default function App(): React.JSX.Element {
  const rt = useBootstrap()
  const health = useHealth(rt)
  const { t } = useI18n()
  const loadConns = useConnections((s) => s.load)
  const currentId = useConnections((s) => s.currentId)
  const loadSchema = useSchema((s) => s.load)

  useEffect(() => {
    if (rt) void loadConns()
  }, [rt, loadConns])

  useEffect(() => {
    if (currentId) void loadSchema(currentId)
  }, [currentId, loadSchema])

  if (!rt) return <Boot title="connecting to tabletalk" hint={t('bootstrap.connectingHint')} />
  if (!health) return <Boot title="waiting for service" hint={t('bootstrap.readyHint')} />
  return <AppLayout health={health} />
}
