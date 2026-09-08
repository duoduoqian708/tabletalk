import { request } from './client'
import type { QueryResponse } from './types'

export interface RunQueryParams {
  connectionId: string
  sql: string
  origin?: 'manual' | 'ai'
  confirm?: boolean
  confirm_token?: string | null
  session_id?: string | null
  limit?: number
  offset?: number
  countTotal?: boolean
  /** 停止按钮 abort（AbortError 向调用方抛出，由其静默处理） */
  signal?: AbortSignal
}

export function runQuery(p: RunQueryParams): Promise<QueryResponse> {
  return request('/api/v1/query', {
    method: 'POST',
    ...(p.signal ? { signal: p.signal } : {}),
    body: JSON.stringify({
      connection_id: p.connectionId,
      sql: p.sql,
      origin: p.origin ?? 'manual',
      confirm: p.confirm ?? false,
      confirm_token: p.confirm_token ?? null,
      session_id: p.session_id ?? null,
      limit: p.limit,
      offset: p.offset,
      count_total: p.countTotal ?? false
    })
  })
}

export function cancelDml(sessionId: string, confirmToken: string): Promise<{ ok: boolean }> {
  return request('/api/v1/ai/dml/cancel', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId, confirm_token: confirmToken })
  })
}

export function cancelQuery(connectionId: string): Promise<{ cancelled: number }> {
  return request('/api/v1/query/cancel', {
    method: 'POST',
    body: JSON.stringify({ connection_id: connectionId })
  })
}

export function formatSql(sql: string, dialect = 'sqlite'): Promise<{ formatted: string }> {
  return request('/api/v1/sql/format', {
    method: 'POST',
    body: JSON.stringify({ sql, dialect })
  })
}
