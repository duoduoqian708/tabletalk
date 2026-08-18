import { getRuntime, request } from './client'

export type ReasoningEffort = 'off' | 'low' | 'medium' | 'high'

export interface AiChatMsg {
  role: string
  content: string
  name?: string | null
  tool_call_id?: string | null
}

export interface AiCard {
  tier: string
  verdict: string
  sql: string
  sub?: string
  preview_rows?: number | null
  reason?: string
  /** 循环内 run_query 工具的真实执行结果（含 rows，供前端直接渲染、消除双执行） */
  result?: {
    columns: string[]
    types: string[]
    rows: unknown[][]
    row_count: number
    truncated?: boolean
    elapsed_ms?: number
  } | null
}

/* 报告模式：章节计划/执行/图表数据/数字回溯 */
export interface ReportSection {
  id: string
  title: string
  intent?: string
  chart_hint?: string
  depends_on_chapter?: string | null
  sql?: string
}
export interface ReportSectionResult {
  id: string
  title: string
  intent?: string
  result_id: string
  ok: boolean
  reason?: string
  sql?: string
  chart: { kind: string; data: unknown[][]; columns?: string[] }
  rows: unknown[][]
  columns: string[]
  types?: string[]
  row_count: number
  elapsed_ms?: number | null
}
export interface ReportRef {
  result_id: string
  title: string
  sql_head: string
  row_count: number
}

export type AiEvent =
  | { type: 'turn_start'; connection: string }
  | { type: 'text'; content: string }
  | { type: 'think'; text: string }
  | { type: 'sql_card'; card: AiCard }
  | { type: 'stage'; stage: string; value?: unknown; tables?: string[] }
  | { type: 'done' }
  | { type: 'error'; message: string }
  // 报告模式事件
  | { type: 'report_start'; connection: string; report_id: string; snapshot_ts: string }
  | { type: 'clarify'; question: string; field: string }
  | { type: 'plan'; sections: ReportSection[] }
  | ({ type: 'section' } & ReportSectionResult)
  | { type: 'narration'; section_id: string | null; text: string; refs: ReportRef[] }
  | { type: 'report_done'; report_id: string; section_count: number }

export interface ChatParams {
  connection_id: string
  messages: AiChatMsg[]
  include_data?: boolean
  table?: string | null
  /** 会话管理：前端会话 id（后端据此 upsert；提问才刷新更新时间） */
  session_id?: string | null
  title?: string | null
  /** 模式：query 单查询（默认）| report 分析报告。前端「报告」按钮显式传 report */
  mode?: 'query' | 'report' | null
  /** 思考强度（对话级）：off=关闭 | low | medium | high；仅支持推理的模型可用 */
  reasoning?: ReasoningEffort | null
  /** 按对话选模型：命中 ai_models 时优先；缺省走默认模型 */
  model_id?: string | null
}

/** AI 网关连通性 + 能力探测测试。 */
export interface GatewayCapabilities {
  connectivity?: boolean
  function_calling?: boolean
  reasoning?: boolean | null
  streaming?: boolean
  context_window?: number | null
}

export async function testGateway(p: {
  model_id?: string
  provider?: string
  baseUrl?: string
  apiKey?: string
  model?: string
}): Promise<{
  ok: boolean
  provider: string
  latency_ms?: number
  error?: string
  reply?: string
  model?: string
  capabilities?: GatewayCapabilities
}> {
  const params = new URLSearchParams()
  if (p.model_id) params.set('model_id', p.model_id)
  if (p.provider) params.set('provider', p.provider)
  if (p.baseUrl) params.set('base_url', p.baseUrl)
  if (p.apiKey) params.set('api_key', p.apiKey)
  if (p.model) params.set('model', p.model)
  const qs = params.toString()
  return request(`/api/v1/ai/test${qs ? `?${qs}` : ''}`, { method: 'POST' })
}

/** 嵌入模型连通性 + 维度探测测试。 */
export async function testEmbedding(p: {
  model_id?: string
  provider?: string
  baseUrl?: string
  apiKey?: string
  model?: string
}): Promise<{
  ok: boolean
  provider?: string
  model?: string
  dimensions?: number
  latency_ms?: number
  error?: string
  note?: string
}> {
  const params = new URLSearchParams()
  if (p.model_id) params.set('model_id', p.model_id)
  if (p.provider) params.set('provider', p.provider)
  if (p.baseUrl) params.set('base_url', p.baseUrl)
  if (p.apiKey) params.set('api_key', p.apiKey)
  if (p.model) params.set('model', p.model)
  const qs = params.toString()
  return request(`/api/v1/ai/embedding/test${qs ? `?${qs}` : ''}`, { method: 'POST' })
}

/** 选中 SQL 解释/优化/风险（/ai/selection）。 */
export async function selection(p: {
  connection_id: string
  sql: string
  kind: 'explain' | 'optimize' | 'risk'
}): Promise<{ kind: string; text: string }> {
  return request('/api/v1/ai/selection', {
    method: 'POST',
    body: JSON.stringify({ connection_id: p.connection_id, sql: p.sql, kind: p.kind })
  })
}

/** 前端异步生成对话标题后回传后端（不刷新会话更新时间）。 */
export async function setSessionTitle(sessionId: string, title: string): Promise<{ ok: boolean }> {
  return request(`/api/v1/chat/sessions/${sessionId}/title`, {
    method: 'PUT',
    body: JSON.stringify({ title })
  })
}

/** SSE 流式聊天：逐事件回调。服务端无状态，前端带完整消息历史。 */
export async function chatStream(params: ChatParams, onEvent: (ev: AiEvent) => void): Promise<void> {
  const rt = getRuntime()
  if (!rt) throw new Error('sidecar 未就绪')
  const res = await fetch(`${rt.baseUrl}/api/v1/ai/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(rt.token ? { 'X-Cleared-Token': rt.token } : {})
    },
    body: JSON.stringify({
      connection_id: params.connection_id,
      messages: params.messages,
      include_data: params.include_data ?? false,
      table: params.table ?? null,
      session_id: params.session_id ?? null,
      title: params.title ?? null,
      mode: params.mode ?? null,
      reasoning: params.reasoning ?? null
    })
  })
  if (!res.ok || !res.body) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body.detail) msg = body.detail
    } catch {
      /* 非 JSON 错误体 */
    }
    // TODO: 测试后删除
    console.log(`[CLEARED][sse] chat 非 200: ${res.status} ${msg}`)
    throw new Error(msg)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  // TODO: 测试后删除——SSE 事件计数（另测 done/error/超时）
  let evCount = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop() ?? ''
    for (const line of lines) {
      const t = line.trim()
      if (!t.startsWith('data:')) continue
      const payload = t.slice(5).trim()
      if (payload === '[DONE]') return
      try {
        const ev = JSON.parse(payload) as AiEvent
        evCount += 1
        console.log(`[CLEARED][sse] [${evCount}] type=${ev.type}${ev.type === 'sql_card' ? ' verdict=' + (ev.card?.verdict ?? '?') : ''}${ev.type === 'error' ? ' msg=' + (ev as any).message : ''}`)
        onEvent(ev)
      } catch {
        /* 跳过非 JSON 事件 */
      }
    }
  }
}
