import { getRuntime, request } from './client'
import { useI18n } from '@renderer/store/i18n'

export type ReasoningEffort = 'off' | 'low' | 'medium' | 'high'

export interface AiChatMsg {
  role: string
  content: string
  name?: string | null
  tool_call_id?: string | null
}

export interface GateReason {
  rule_id: string
  message: string
  message_en?: string
  objects: string[]
}

export interface Manifest {
  tables: string[]
  kb_docs: number
  history_turns: number
  include_data: boolean
  redactions: string[]
  mode: string
  ts: string
  model: string
  provider: string
}

export interface Blast {
  direct: { table: string; estimated_rows: number | null }[]
  cascade: { table: string; via: string | null; fk: string | null; hops: number; has_fk: boolean }[]
  constraints: string[]
  preview_rows: number | null
}

export interface AiCard {
  tier: string
  verdict: string
  sql: string
  sub?: string
  preview_rows?: number | null
  blast?: Blast | null
  rollback?: { kind: string; backup_sql: string | null; rollback_sql: string; note: string } | null
  reason?: string
  reasons?: GateReason[]
  /** 循环内 run_query 工具的真实执行结果（含 rows，供前端直接渲染、消除双执行） */
  result?: {
    columns: string[]
    types: string[]
    rows: unknown[][]
    row_count: number
    truncated?: boolean
    elapsed_ms?: number
  } | null
  /** 人工确认/运行后已执行：展示"已留痕"标记 */
  executed?: boolean
  affected?: number
  /** WS4 确认协议：DML 需确认卡 */
  confirm_token?: string
  expires_in?: number
  needs_confirm?: boolean
  /** WS5 问题库命中卡 */
  question_library?: boolean
  question_id?: string
  /** S3：LLM 顺带产出的可选追加项（用户点一下 → 轻量改写 SQL） */
  options?: SqlOption[]
}

/** S3：SQL 可选追加项建议 */
export interface SqlOption {
  id?: string
  label: string
  hint?: string
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

// 引擎合一（2026-09）：后端只发 table/chart 两种 block；sql_editor/confirm/choice
// 是旧 chat_stream 时代的替代显示物（卡片挂载修复后无发射方），已删。
export type Block =
  | { kind: 'text'; text: string }
  | { kind: 'table'; columns: string[]; rows: unknown[][]; title?: string }
  | { kind: 'chart'; chartType: 'bar' | 'line' | 'pie'; title?: string; data: unknown[][]; columns?: string[] }

export type AiEvent =
  | { type: 'turn_start'; connection: string }
  | { type: 'text'; content: string }
  | { type: 'think'; text: string }
  | { type: 'sql_card'; card: AiCard }
  | { type: 'stage'; stage: string; value?: unknown; tables?: string[]; vec_tables?: string[] }
  | { type: 'manifest'; manifest: Manifest }
  | { type: 'done'; context_meta?: Record<string, unknown> }
  | { type: 'error'; message: string; code?: string }
  | { type: 'scene_start'; scene: string; intent?: string | null; skill_id?: string | null }
  | { type: 'scene_done'; scene: string }
  | { type: 'subtask_start'; id: string; tool: string; label: string; status: string }
  | { type: 'subtask_progress'; id: string; tool: string; delta: string }
  | { type: 'subtask_done'; id: string; tool: string; status: string; detail?: string }
  | { type: 'block'; id: string; block: Block }
  // 任务级（2026-09 §19.6 实时任务流）
  | { type: 'task_start'; id: string; skill: string; action: string }
  | { type: 'task_result'; index: number; id: string; result_type: string; ok: boolean; error?: string | null; data?: Record<string, unknown> }
  | { type: 'task_done'; id: string; ok: boolean; error?: string | null; result_type?: string }
  | { type: 'plan_stopped'; reason: string }
  // 报告模式事件
  | { type: 'report_start'; connection: string; report_id: string; snapshot_ts: string }
  | { type: 'clarify'; question: string; field?: string; options?: string[]; origin?: 'ask_user' | 'report' }
  | { type: 'plan'; sections: ReportSection[] }
  | ({ type: 'section' } & ReportSectionResult)
  | { type: 'narration'; section_id: string | null; text: string; refs: ReportRef[] }
  | { type: 'report_done'; report_id: string; section_count: number }
  // 受控计划（propose_plan → 人审 → 分段执行）
  | { type: 'plan_pending'; plan_id: string; title: string; steps: { action: string; description: string; sql?: string }[]; expires_in: number }
  | { type: 'plan_started'; plan_id: string; title: string; steps: number }
  | { type: 'plan_awaiting'; plan_id: string; remaining: number }
  | { type: 'plan_done'; plan_id: string; completed: number }
  | { type: 'plan_failed'; plan_id: string; completed: number }

/** 受控计划确认：SSE 流式执行下一段（task_* / sql_card / plan_* 事件）。
 *  signal：停止按钮/墙钟超时兜底（AbortError 向调用方抛出，由其静默处理）。 */
export async function confirmPlanStream(
  planId: string,
  sessionId: string,
  onEvent: (ev: AiEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const rt = getRuntime()
  const locale = useI18n.getState().locale
  const res = await fetch(`/api/v1/ai/plans/${encodeURIComponent(planId)}/confirm`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(rt?.token ? { 'X-TableTalk-Token': rt.token } : {}),
      ...(locale ? { 'X-Locale': locale } : {})
    },
    body: JSON.stringify({ session_id: sessionId }),
    ...(signal ? { signal } : {})
  })
  if (!res.ok || !res.body) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body.detail) msg = body.detail
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new Error(msg)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
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
        onEvent(JSON.parse(payload) as AiEvent)
      } catch {
        /* 跳过非 JSON 事件 */
      }
    }
  }
}

/** 受控计划拒绝：标记 rejected 并写系统消息（模型下轮可见） */
export async function rejectPlan(planId: string, sessionId: string): Promise<void> {
  await request(`/api/v1/ai/plans/${encodeURIComponent(planId)}/reject`, {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId })
  })
}

export interface AiToolLog {
  id: string
  tool: string
  label: string
  status: 'running' | 'done' | 'error'
  logs: string[]   // 实时日志流（subtask_progress delta + 结果/错误）
}
export interface AiTaskFlow {
  id: string
  skill: string
  action: string
  status: 'running' | 'done' | 'error' | 'blocked'
  result_type?: string
  index?: number
  error?: string | null
  tools: AiToolLog[]
}

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
  onStep?: (step: string, ok: boolean, value?: number | null) => void
}): Promise<{
  ok: boolean
  provider?: string
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
  const rt = getRuntime()
  if (!rt) throw new Error(useI18n.getState().t('api.sidecarNotReady'))
  const locale = useI18n.getState().locale
  const res = await fetch(`${rt.baseUrl}/api/v1/ai/test${qs ? `?${qs}` : ''}`, {
    method: 'POST',
    headers: {
      ...(rt.token ? { 'X-TableTalk-Token': rt.token } : {}),
      ...(locale ? { 'X-Locale': locale } : {})
    }
  })
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      if (body.detail) msg = body.detail
    } catch { /* 非 JSON */ }
    throw new Error(msg)
  }
  const contentType = res.headers.get('content-type') ?? ''
  // mock/early-error 返回 JSON（非 SSE）
  if (!contentType.includes('text/event-stream')) {
    return res.json() as Promise<any>
  }
  // SSE 流式：逐事件回调 onStep，返回最后一个 done 事件
  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let doneResult: any = { ok: false }
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
      if (!payload) continue
      try {
        const ev = JSON.parse(payload)
        if (ev.step === 'done') {
          doneResult = ev
        } else if (p.onStep) {
          p.onStep(ev.step, ev.ok, ev.value ?? null)
        }
      } catch { /* 忽略解析错误 */ }
    }
  }
  return doneResult
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

export interface BuiltinProvider {
  /** 规范名（存库值，后端别名归一到 adapter） */
  name: string
  /** 下拉显示名（火山有按量/Agent Plan 双条目，display 唯一） */
  display: string
  adapter: string
  /** URL 预设（自动带出，可改） */
  base_url: string
  /** "|" 分隔的推荐模型候选（拉取失败时的预设兜底） */
  models: string
  /** 默认向量维度（仅向量清单有，选中时预填） */
  dimensions?: string
}

/** 内置供应商下拉清单（含 URL 预设与推荐模型）。 */
export async function getBuiltinProviders(): Promise<BuiltinProvider[]> {
  const r = await request<{ providers: BuiltinProvider[] }>('/api/v1/ai/providers')
  return r.providers
}

/** 内置向量供应商下拉清单（三家 + 火山双路 + 自定义）。 */
export async function getBuiltinEmbeddingProviders(): Promise<BuiltinProvider[]> {
  const r = await request<{ providers: BuiltinProvider[] }>('/api/v1/ai/embedding/providers')
  return r.providers
}

/** 代理拉取上游 /models（key 服务端持有；不支持时 ok=false，前端回退自由输入）。 */
export async function fetchUpstreamModels(p: {
  baseUrl: string
  apiKey?: string
  modelId?: string
}): Promise<{ ok: boolean; models?: string[]; error?: string }> {
  const params = new URLSearchParams()
  params.set('base_url', p.baseUrl)
  if (p.apiKey) params.set('api_key', p.apiKey)
  if (p.modelId) params.set('model_id', p.modelId)
  return request(`/api/v1/ai/upstream/models?${params.toString()}`)
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

/** SSE 流式聊天：逐事件回调。服务端无状态，前端带完整消息历史。
 *  signal：停止按钮/墙钟超时兜底（AbortError 向调用方抛出，由其静默处理）。 */
export async function chatStream(
  params: ChatParams,
  onEvent: (ev: AiEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const rt = getRuntime()
  if (!rt) throw new Error(useI18n.getState().t('api.sidecarNotReady'))
  const locale = useI18n.getState().locale
  const res = await fetch(`${rt.baseUrl}/api/v1/ai/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(rt.token ? { 'X-TableTalk-Token': rt.token } : {}),
      ...(locale ? { 'X-Locale': locale } : {})
    },
    ...(signal ? { signal } : {}),
    body: JSON.stringify({
      connection_id: params.connection_id,
      messages: params.messages,
      include_data: params.include_data ?? false,
      table: params.table ?? null,
      session_id: params.session_id ?? null,
      title: params.title ?? null,
      mode: params.mode ?? null,
      reasoning: params.reasoning ?? null,
      model_id: params.model_id ?? null
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
    if (import.meta.env.DEV) console.log(`[tabletalk][sse] chat 非 200: ${res.status} ${msg}`)
    throw new Error(msg)
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
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
        if (import.meta.env.DEV) console.log(`[tabletalk][sse] [${evCount}] type=${ev.type}${ev.type === 'sql_card' ? ' verdict=' + (ev.card?.verdict ?? '?') : ''}${ev.type === 'error' ? ' msg=' + (ev as any).message : ''}`)
        onEvent(ev)
      } catch {
        /* 跳过非 JSON 事件 */
      }
    }
  }
}
