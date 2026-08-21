import type { SidecarRuntime } from '@shared/types'
import { useI18n } from '@renderer/store/i18n'

let rt: SidecarRuntime | null = null

export function setRuntime(r: SidecarRuntime | null): void {
  rt = r
}

export function getRuntime(): SidecarRuntime | null {
  return rt
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const r = rt
  if (!r) throw new ApiError(0, useI18n.getState().t('api.sidecarNotReady'))
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init.headers as Record<string, string>) ?? {})
  }
  if (r.token) headers['X-TableTalk-Token'] = r.token
  const dbgStart = import.meta.env.DEV ? performance.now() : 0
  if (import.meta.env.DEV) console.log(`[tabletalk][api] ${init.method ?? 'GET'} ${path}`)
  const res = await fetch(`${r.baseUrl}${path}`, { ...init, headers })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body.detail) msg = body.detail
    } catch {
      /* 非 JSON 错误体 */
    }
    if (import.meta.env.DEV) console.log(`[tabletalk][api] ${init.method ?? 'GET'} ${path} -> ${res.status} (${msg}) ${Math.round(performance.now() - dbgStart)}ms`)
    throw new ApiError(res.status, msg)
  }
  const data = (await res.json()) as T
  if (import.meta.env.DEV) console.log(`[tabletalk][api] ${init.method ?? 'GET'} ${path} -> 200 ok ${Math.round(performance.now() - dbgStart)}ms`)
  return data
}
