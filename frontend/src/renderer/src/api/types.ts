export interface ConnectionConfig {
  id: string
  name: string
  dialect: string
  host: string
  port: number | null
  user: string
  password: string
  database: string
  file: string
  ssl: boolean
  read_only: boolean
  timeout: number
  credential_ref: string | null
  created_at: string
  sensitive: string[]
}

export interface TableInfo {
  name: string
  kind: string
  comment: string
  column_count: number
}

export interface ColumnInfo {
  table: string
  name: string
  type: string
  nullable: boolean
  pk: boolean
  fk: boolean
  default: string | null
  comment: string
}

export interface FKInfo {
  table: string
  column: string
  ref_table: string
  ref_column: string
}

export interface SchemaResponse {
  connection: string
  dialect: string
  databases: string[]
  tables: TableInfo[]
  columns: ColumnInfo[]
  foreign_keys: FKInfo[]
}

export interface TablePreview {
  columns: string[]
  types: string[]
  rows: unknown[][]
  total: number
}

export interface QueryAllow {
  verdict: 'allow'
  tier: 'read'
  columns: string[]
  types: string[]
  rows: unknown[][]
  row_count: number
  truncated: boolean
  affected_rows: number | null
  is_dml: boolean
  elapsed_ms: number
  total: number | null
  reason: string
  suggestions: string[]
  tables?: string[]
}

export interface QueryReview {
  verdict: 'review'
  tier: string
  reason: string
  preview_rows: number | null
  needs_confirm: true
  elapsed_ms: number
}

export interface QueryBlock {
  verdict: 'block'
  tier: string
  reason: string
  suggestions: string[]
  elapsed_ms: number
}

export interface QueryExecuted {
  verdict: 'executed'
  tier: string
  reason: string
  affected_rows: number | null
  row_count: number | null
  elapsed_ms: number
}

export type QueryResponse = QueryAllow | QueryReview | QueryBlock | QueryExecuted

export interface AuditEntry {
  ts: string
  connection: string
  origin: string
  tier: string
  verdict: string
  status: string
  sql: string
  elapsed_ms: number
}

export interface TagInfo {
  name: string
  description: string
  status: 'draft' | 'confirmed'
}

export interface ReviewTable {
  name: string
  kind: string
  column_count: number
  comment: string
  comment_status: string
  tags: { name: string; status: string }[]
}

export interface ReviewColumn {
  table: string
  name: string
  type: string
  pk: boolean
  fk: boolean
  comment: string
  status: string
}

export interface GraphEdge {
  from: string
  from_col?: string
  to: string
  to_col?: string
  kind: 'fk' | 'overlap'
  weight?: number
  shared?: number
}

export interface KnowledgeOverview {
  tables: ReviewTable[]
  columns: ReviewColumn[]
  graph: { edges: GraphEdge[] }
  tags: { library: TagInfo[]; tables: Record<string, string[]> }
  draft_count: number
  tag_draft_count: number
  sample_cols: number
  embedding_provider: string
}

export interface RouteResult {
  tables: string[]
  edges: GraphEdge[]
  seeded: number
}
