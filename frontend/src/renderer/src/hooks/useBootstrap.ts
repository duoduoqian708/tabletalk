import { useEffect, useState } from 'react'
import type { SidecarRuntime } from '@shared/types'
import { setRuntime } from '@renderer/api/client'

/** Web 引导：轮询 /api/v1/bootstrap 拿 token（后端未就绪时重试），注入 API client。 */
export function useBootstrap(): SidecarRuntime | null {
  const [rt, setRt] = useState<SidecarRuntime | null>(null)

  useEffect(() => {
    let alive = true
    const apply = (r: SidecarRuntime): void => {
      if (!alive) return
      setRt(r)
      setRuntime(r)
    }
    const tick = (): void => {
      void fetch('/api/v1/bootstrap')
        .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
        .then((b: { token: string; dataDir: string }) => {
          apply({ baseUrl: '', token: b.token, dataDir: b.dataDir })
        })
        .catch(() => {
          if (alive) setTimeout(tick, 700)
        })
    }
    tick()
    return () => {
      alive = false
    }
  }, [])

  return rt
}
