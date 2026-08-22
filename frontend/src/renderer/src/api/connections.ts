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
  sensitive?: string[]
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

/** 接入流程前置：用待保存的配置测试连接（不落盘）。通过后保存按钮才可点。
 * savedConnId（编辑模式）：密码留空时后端用该连接已存密码填充测试（旧密码不出网）。 */
export function testDraftConnection(input: ConnectionInput, opts?: { savedConnId?: string | null }): Promise<TestResult> {
  const body = opts?.savedConnId ? { ...input, saved_conn_id: opts.savedConnId } : input
  return request('/api/v1/connections/test-draft', { method: 'POST', body: JSON.stringify(body) })
}
