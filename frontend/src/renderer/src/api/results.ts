import { request } from './client'

export interface ResultItem {
  id: string
  source: 'job' | 'chat'
  group_id: string
  source_id: string
  connection_id?: string
  title?: string
  format: string
  size?: number
  meta?: Record<string, unknown>
  created_at: string
}

export interface ResultDetail extends ResultItem {
  content: string
}

export async function listResults(source?: string, groupId?: string, limit = 50): Promise<{ ok: boolean; results: ResultItem[] }> {
  const q = new URLSearchParams()
  if (source) q.set('source', source)
  if (groupId) q.set('group_id', groupId)
  q.set('limit', String(limit))
  return request(`/api/v1/results?${q.toString()}`)
}

export async function getResult(id: string): Promise<{ ok: boolean; result: ResultDetail }> {
  return request(`/api/v1/results/${encodeURIComponent(id)}`)
}
