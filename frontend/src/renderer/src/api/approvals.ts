import { request } from './client'

export interface ApprovalItem {
  id: string
  connection_id: string
  sql: string
  requested_by: string
  requested_at: string
  status: 'pending' | 'approved' | 'rejected'
  reviewed_by?: string | null
  reviewed_at?: string | null
  note?: string | null
  preview_rows?: number | null
  executed_audit_id?: number | null
  rollback_ref?: string | null
}

export async function listApprovals(status?: string): Promise<{ items: ApprovalItem[] }> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : ''
  return request(`/api/v1/approvals${qs}`)
}

export async function approveApproval(id: string): Promise<{ id: string; result?: unknown }> {
  return request(`/api/v1/approvals/${id}/approve`, { method: 'POST', body: JSON.stringify({}) })
}

export async function rejectApproval(id: string, note?: string): Promise<{ id: string; status: string }> {
  return request(`/api/v1/approvals/${id}/reject`, { method: 'POST', body: JSON.stringify({ note }) })
}
