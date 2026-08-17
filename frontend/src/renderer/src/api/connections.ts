import { request } from './client'
import type { ConnectionConfig } from './types'

export interface ConnectionInput {
  name?: string
  dialect?: string
  host?: string
  port?: number | null
  user?: string
  password?: string
  database?: string
  file?: string
  ssl?: boolean
  read_only?: boolean
  timeout?: number
}

export interface TestResult {
  ok: boolean
  latency_ms: number
  error: string | null
}

export function listConnections(): Promise<ConnectionConfig[]> {
  return request('/api/v1/connections')
}

export function createConnection(input: ConnectionInput): Promise<ConnectionConfig> {
  return request('/api/v1/connections', { method: 'POST', body: JSON.stringify(input) })
}

export function updateConnection(id: string, patch: ConnectionInput): Promise<ConnectionConfig> {
  return request(`/api/v1/connections/${id}`, { method: 'PUT', body: JSON.stringify(patch) })
}

export function deleteConnection(id: string): Promise<{ deleted: string }> {
  return request(`/api/v1/connections/${id}`, { method: 'DELETE' })
}

export function testConnection(id: string): Promise<TestResult> {
  return request(`/api/v1/connections/${id}/test`, { method: 'POST' })
}
