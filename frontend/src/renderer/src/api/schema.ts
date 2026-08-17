import { request } from './client'
import type { SchemaResponse, TablePreview } from './types'

export function getSchema(connId: string, refresh = false): Promise<SchemaResponse> {
  return request(`/api/v1/connections/${connId}/schema?refresh=${refresh}`)
}

export function previewTable(connId: string, table: string, limit = 100): Promise<TablePreview> {
  return request(`/api/v1/connections/${connId}/schema/${table}/preview?limit=${limit}`)
}
