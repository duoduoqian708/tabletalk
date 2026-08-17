import { request } from './client'
import type { QueryResponse } from './types'

export interface RunQueryParams {
  connectionId: string
  sql: string
  origin?: 'manual' | 'ai'
  confirm?: boolean
  limit?: number
  offset?: number
  countTotal?: boolean
}

export function runQuery(p: RunQueryParams): Promise<QueryResponse> {
  return request('/api/v1/query', {
    method: 'POST',
    body: JSON.stringify({
      connection_id: p.connectionId,
      sql: p.sql,
      origin: p.origin ?? 'manual',
      confirm: p.confirm ?? false,
      limit: p.limit,
      offset: p.offset,
      count_total: p.countTotal ?? false
    })
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
