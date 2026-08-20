import { request } from './client'
import type { AuditEntry } from './types'

export interface AuditQuery {
  verdict?: string
  origin?: string
  tier?: string
  from_ts?: string
  to_ts?: string
  report_id?: string
  limit?: number
  offset?: number
}

export interface AuditSummary {
  total: number
  by_verdict: Record<string, number>
  by_origin: Record<string, number>
  by_tier: Record<string, number>
  blocked_rate: number
  ai_ddl_count: number
  review_count: number
}

export async function listAudit(
  connId?: string,
  q: AuditQuery = {},
): Promise<{ count: number; entries: AuditEntry[] }> {
  const params = new URLSearchParams()
  if (connId) params.set('connection', connId)
  for (const [k, v] of Object.entries(q)) {
    if (v !== undefined && v !== '') params.set(k, String(v))
  }
  const qs = params.toString()
  return request<{ count: number; entries: AuditEntry[] }>(`/api/v1/audit${qs ? `?${qs}` : ''}`)
}

export async function auditSummary(
  connId?: string,
  q: { verdict?: string; from_ts?: string; to_ts?: string } = {},
): Promise<AuditSummary> {
  const params = new URLSearchParams()
  if (connId) params.set('connection', connId)
  for (const [k, v] of Object.entries(q)) {
    if (v !== undefined && v !== '') params.set(k, String(v))
  }
  const qs = params.toString()
  return request<AuditSummary>(`/api/v1/audit/summary${qs ? `?${qs}` : ''}`)
}
