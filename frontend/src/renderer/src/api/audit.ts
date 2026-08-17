import { request } from './client'
import type { AuditEntry } from './types'

export async function listAudit(connId?: string): Promise<AuditEntry[]> {
  const q = connId ? `?connection=${encodeURIComponent(connId)}` : ''
  const r = await request<{ count: number; entries: AuditEntry[] }>(`/api/v1/audit${q}`)
  return r.entries
}
