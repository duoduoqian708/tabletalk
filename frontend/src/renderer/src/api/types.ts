/** 敏感名单条目：字符串=旧 glob（兼容）；{table, columns} 精确名（段8，columns 空=整表） */
export type SensitiveEntry = string | { table: string; columns?: string[] }

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
  sensitive: SensitiveEntry[]
  kb_status?: 'none' | 'building' | 'pending_review' | 'ready'
  kb_updated_at?: string
}

export interface TableInfo {
  name: string
  kind: string
  comment: string
  column_count: number
  row_count: number
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
  reasons?: GateReason[]
  suggestions: string[]
  tables?: string[]
}

export interface GateReason {
  rule_id: string
  message: string
  message_en?: string
  objects: string[]
}

export interface BlastDirect {
  table: string
  estimated_rows: number | null
}
export interface BlastCascade {
  table: string
  via: string | null
  fk: string | null
  hops: number
  has_fk: boolean
}
export interface Blast {
  direct: BlastDirect[]
  cascade: BlastCascade[]
  constraints: string[]
  preview_rows: number | null
}

export interface QueryReview {
  verdict: 'review'
  tier: string
  reason: string
  reasons?: GateReason[]
  preview_rows: number | null
  blast?: Blast | null
  rollback?: { kind: string; backup_sql: string | null; rollback_sql: string; note: string } | null
  needs_confirm: true
  elapsed_ms: number
}

export interface QueryBlock {
  verdict: 'block'
  tier: string
  reason: string
  reasons?: GateReason[]
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
  report_id?: string
  reasons?: GateReason[]
  tables?: string[]
  schema_version?: number
  manifest?: any
  source?: string
  ack?: 'unread' | 'ack'
}

export interface TagInfo {
  name: string
  description: string
  status: 'draft' | 'confirmed'
  /** 后端持久化颜色（空 = 前端哈希色板兜底） */
  color: string
}

/** 知识条目状态机（v2：comment+values 整体确认） */
export type KbItemStatus = 'none' | 'draft' | 'confirmed'

/** 字段级知识视图（对照后端 store.overview() 的 columns 输出） */
export interface KbColumnView {
  name: string
  type: string
  pk: boolean
  fk: boolean
  /** 数据库自带注释（参考源） */
  db_comment: string
  /** 业务含义（AI 生成 → 人工确认） */
  comment: string
  /** 可选值对照 "P=待付款; S=已发货"（授权采样构建时非空） */
  values: string
  /** 枚举标记：契约 = 有 key-value 枚举数组（values 非空）才算枚举 */
  is_enum: boolean
  /** 示例值（首个非空样本，截断 60 字符） */
  example: string
  status: KbItemStatus
  /** 2026-09：本轮 AI 提案（与当前生效值并行；确认=提升 / 保持当前=清除） */
  proposed_comment?: string
  proposed_values?: string
  proposed_example?: string
}

/** 表级知识块（一表一块）：字段行挂在其下 */
export interface KbTableView {
  name: string
  kind: string
  db_comment: string
  column_count: number
  comment: string
  comment_status: KbItemStatus
  tags: { name: string; status: string }[]
  excluded: boolean
  ddl: string
  /** 2026-09：本轮表级提案（对比/取新/保持当前） */
  proposed_comment?: string
  columns: KbColumnView[]
  /** 向量化片段（可读表描述）：人工覆盖优先，否则构建合成文本 */
  vector_text: string
  /** 人工覆盖的向量化片段；null = 未覆盖（用构建合成） */
  vector_override: string | null
}

/** 边 v2：字段级端点 + 基数；from 恒为多侧 */
export interface GraphEdge {
  from: string
  from_col?: string | null
  to: string
  to_col?: string | null
  kind: 'fk' | 'overlap' | 'user' | 'llm' | 'naming' | 'value_overlap' | 'query_log'
  cardinality?: 'n:1' | '1:1'
  reason?: string
  weight?: number | null
  /** draft=LLM 未确认边（宿主合并 llm_draft_edges 时标记）；缺省视为 confirmed */
  status?: 'draft' | 'confirmed'
  /** 图 diff（2026-09）：removed=本轮未重新提案的已确认边（红） */
  diff?: 'new' | 'modified' | 'removed' | null
}

export interface GraphDraftEdge {
  from_table: string
  from_col?: string | null
  to_table: string
  to_col?: string | null
  reason: string
  source: string
  status?: 'previously_rejected'
  /** 图 diff（2026-09）：new=绿（新增）/ modified=黄（修改） */
  diff?: 'new' | 'modified'
}

/** 2D 图布局坐标（表名 → 画布中心点；overview 回读 / 拖拽写回） */
export type GraphLayout = Record<string, { x: number; y: number }>

export interface KnowledgeOverview {
  built?: boolean
  kb_status?: string
  synced_at?: string
  /** 当前生效版本号（版本制：确认启用时 +1；0=未启用） */
  version?: number
  tables: KbTableView[]
  graph: { edges: GraphEdge[]; excluded?: string[]; llm_draft_edges?: GraphDraftEdge[]; layout?: GraphLayout }
  tags: { library: TagInfo[]; tables: Record<string, string[]> }
  /** 待确认草案数（表+列注释） */
  draft_count: number
  tag_draft_count: number
  embedding_provider: string
}

/** 字段历史版本（版本制：确认时归档，供「版本回溯」复用旧值） */
export interface FieldHistoryItem {
  id: number
  batch_ts: string
  version: number
  comment: string
  values: string
  example: string
  status: string
}

export interface BuildProgress {
  stage: string
  percent: number
  done: boolean
  error: string | null
  kb_status: string
  detail: string | null
}

export interface KbStatus {
  kb_status: string
  kb_updated_at: string
  /** {draft_docs: 表+列注释草案, draft_tags, llm_graph_draft} */
  pending: { draft_docs: number; draft_tags: number; llm_graph_draft: number }
  building: boolean
}

export interface RouteResult {
  tables: string[]
  edges: GraphEdge[]
  seeded: number
}
