import { useEffect, useState } from 'react'
import type { SidecarRuntime } from '@shared/types'
import { setRuntime } from '@renderer/api/client'

/** Web 引导：轮询 /api/v1/bootstrap 拿 token（后端未就绪时重试），注入 API client；团队模式下支持本地登录覆盖。 */
export function useBootstrap(): SidecarRuntime | null {
  const [rt, setRt] = useState<SidecarRuntime | null>(null)

  useEffect(() => {
    let alive = true
    const apply = (r: SidecarRuntime): void => {
      if (!alive) return
      setRt(r)
      setRuntime(r)
    }
    // 团队模式：优先尝试本地已登录 token
    try {
      const saved = localStorage.getItem('tabletalk-token')
      const savedDir = localStorage.getItem('tabletalk-dataDir')
      if (saved && savedDir) {
        const r = { baseUrl: '', token: saved, dataDir: savedDir }
        apply(r)
        // 校验有效性，失效则回退到 bootstrap
        void fetch('/api/v1/connections', { headers: { 'X-TableTalk-Token': saved } }).then((res) => {
          if (!res.ok && alive) {
            localStorage.removeItem('tabletalk-token')
            // 回退获取 bootstrap
            const tick = (): void => {
              void fetch('/api/v1/bootstrap')
                .then((res) => (res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`))))
                .then((b: { token: string; dataDir: string }) => apply({ baseUrl: '', token: b.token, dataDir: b.dataDir }))
                .catch(() => { if (alive) setTimeout(tick, 700) })
            }
            tick()
          }
        })
        return () => { alive = false }
      }
    } catch {}
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

export function setLoginRuntime(token: string, dataDir: string): void {
  const r = { baseUrl: '', token, dataDir }
  setRuntime(r)
  try {
    localStorage.setItem('tabletalk-token', token)
    localStorage.setItem('tabletalk-dataDir', dataDir)
  } catch {}
}
