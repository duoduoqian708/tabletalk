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

export async function auditEgress(connId?: string): Promise<{ total: number; by_model: Record<string, number>; by_mode: Record<string, number>; entries: AuditEntry[] }> {
  const params = new URLSearchParams()
  if (connId) params.set('connection', connId)
  const qs = params.toString()
  return request(`/api/v1/audit/egress${qs ? `?${qs}` : ''}`)
}

export async function auditWeekly(connId?: string): Promise<{ weekly: Record<string, number>; top_tables: [string, number][]; anomalies: { ts: string; sql: string; verdict: string }[]; total: number }> {
  const params = new URLSearchParams()
  if (connId) params.set('connection', connId)
  const qs = params.toString()
  return request(`/api/v1/audit/weekly${qs ? `?${qs}` : ''}`)
}

export interface SignalInfo { unread_exceptions: number; pending_approvals: number; today: { blocked: number; review: number } }
export interface StatBucket { bucket: string; total: number; allow: number; review: number; block: number }

export async function auditSignal(): Promise<SignalInfo> {
  return request('/api/v1/audit/signal')
}

export async function auditStats(scope: 'today' | '7d' | '30d', connection?: string): Promise<{ scope: string; granularity: string; buckets: StatBucket[] }> {
  const p = new URLSearchParams({ scope })
  if (connection) p.set('connection', connection)
  return request(`/api/v1/audit/stats?${p}`)
}

export async function ackAudit(id: number): Promise<void> {
  await request(`/api/v1/audit/${id}/ack`, { method: 'POST', body: '{}' })
}

export interface PageQuery { connection?: string; q?: string; exception?: boolean; unread_only?: boolean; verdict?: string; origin?: string; tier?: string; from_ts?: string; cursor?: number; limit?: number }

export async function listAuditPage(query: PageQuery): Promise<{ items: (AuditEntry & { _id: number })[]; next_cursor: number | null }> {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v !== undefined && v !== '' && v !== false) p.set(k, String(v === true ? 'true' : v))
  }
  return request(`/api/v1/audit?${p}`)
}
