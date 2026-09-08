import { request, getRuntime } from './client'
import { useI18n } from '@renderer/store/i18n'

export interface JobTask {
  name: string
  cron: string
  friendly: string
  connection: string
  enabled: boolean
  system: boolean
  file: string
  next_run?: string | null
  last_run?: string | null
  last_status?: string | null
  last_summary?: string | null
  running?: boolean
}

export interface TaskRun {
  id: number
  started_at: string
  finished_at?: string | null
  status: string
  summary?: string | null
  output?: string | null
  /** 产出物相对路径（reports/xxx.md），运行时从输出解析 */
  artifacts?: string[]
}

export interface TaskProposal {
  script: string
  name: string
  cron: string
  connection: string
  summary: string
  next_run?: string | null
  /** true = 拦截 3 次后放行的未验证提案（前端显示警告徽章） */
  untested?: boolean
  /** true = 未走需求确认（PLAN 硬闸 2 次打回后仍交付，前端黄标提示） */
  plan_skipped?: boolean
}

export interface TaskPlan {
  cron: string
  cron_friendly?: string
  connection: string
  tables: string[]
  mode: 'read' | 'write'
  output: string
  assumptions: string[]
  questions: string[]
}

export interface AgentReply {
  ok: boolean
  reply: string
  needs: 'clarify' | 'plan' | 'proposal'
  proposal?: TaskProposal | null
  plan?: TaskPlan | null
}

export async function listTasks(): Promise<{ tasks: JobTask[] }> {
  return request('/api/v1/tasks')
}

export async function deployTask(body: {
  name: string
  cron: string
  connection?: string
  script?: string
  overwrite?: boolean
}): Promise<{ task: JobTask; test?: { ok: boolean; status?: string; summary?: string } }> {
  return request('/api/v1/tasks/deploy', { method: 'POST', body: JSON.stringify(body) })
}

export async function patchTask(name: string, body: { enabled?: boolean; cron?: string }): Promise<{ task: JobTask }> {
  return request(`/api/v1/tasks/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify(body) })
}

export async function saveTaskScript(name: string, script: string): Promise<{ task: JobTask }> {
  return request(`/api/v1/tasks/${encodeURIComponent(name)}/script`, { method: 'PUT', body: JSON.stringify({ script }) })
}

export async function getTaskScript(name: string): Promise<{ script: string; cron: string; connection: string }> {
  return request(`/api/v1/tasks/${encodeURIComponent(name)}/script`)
}

export async function deleteTask(name: string): Promise<{ ok: boolean }> {
  return request(`/api/v1/tasks/${encodeURIComponent(name)}`, { method: 'DELETE' })
}

export async function runTask(name: string): Promise<{ ok: boolean; status?: string; summary?: string }> {
  return request(`/api/v1/tasks/${encodeURIComponent(name)}/run`, { method: 'POST', body: '{}' })
}

export async function cancelTask(name: string): Promise<{ ok: boolean; message?: string }> {
  return request(`/api/v1/tasks/${encodeURIComponent(name)}/cancel`, { method: 'POST', body: '{}' })
}

export interface TestResult { ok: boolean; status?: string; summary?: string; output?: string; exit_code?: number }

export async function testScript(script: string, connectionId?: string): Promise<TestResult> {
  return request('/api/v1/tasks/test', { method: 'POST', body: JSON.stringify({ script, connection_id: connectionId || '' }) })
}

export async function taskRuns(name: string): Promise<{ runs: TaskRun[] }> {
  return request(`/api/v1/tasks/${encodeURIComponent(name)}/runs`)
}

export interface ReportFile { ok: boolean; name: string; size: number; mtime: string; content: string }

export async function getReport(filename: string): Promise<ReportFile> {
  return request(`/api/v1/tasks/reports/${encodeURIComponent(filename)}`)
}

export async function agentChat(messages: { role: string; content: string }[], connectionId?: string, signal?: AbortSignal): Promise<AgentReply> {
  return request('/api/v1/tasks/agent', {
    method: 'POST',
    body: JSON.stringify({ messages, connection_id: connectionId || '' }),
    signal,
  })
}

/**
 * 流式版：SSE 读取，每收到 text chunk 调用 onChunk；
 * 返回 done 事件（needs + proposal）。兼容 mock（mock 路径现在也走 SSE）。
 */
export async function agentChatStream(
  messages: { role: string; content: string }[],
  connectionId: string | undefined,
  signal: AbortSignal | undefined,
  onChunk: (text: string) => void,
  editTask?: string,
  onReasoning?: (text: string) => void,
  sessionId?: string,
  confirmPlan?: boolean,
): Promise<AgentReply> {
  const runtime = getRuntime()
  const locale = useI18n.getState().locale
  const res = await fetch('/api/v1/tasks/agent', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(runtime?.token ? { 'X-TableTalk-Token': runtime.token } : {}),
      ...(locale ? { 'X-Locale': locale } : {})
    },
    body: JSON.stringify({
      messages,
      connection_id: connectionId || '',
      edit_task: editTask || '',
      session_id: sessionId || '',
      confirm_plan: !!confirmPlan,
    }),
    signal,
  })
  if (!res.ok || !res.body) {
    let msg = `HTTP ${res.status}`
    try { const b = await res.json() as { detail?: string }; if (b.detail) msg = b.detail } catch {}
    throw new Error(msg)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let result: AgentReply = { ok: true, needs: 'clarify', reply: '' }
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
      if (payload === '[DONE]') continue
      try {
        const ev = JSON.parse(payload) as { type?: string; content?: string; needs?: string; proposal?: TaskProposal | null; plan?: TaskPlan | null; reply?: string; message?: string }
        if (ev.type === 'text' && typeof ev.content === 'string') onChunk(ev.content)
        if (ev.type === 'reasoning' && typeof ev.content === 'string') onReasoning?.(ev.content)
        if (ev.type === 'done') result = { ok: true, needs: (ev.needs as AgentReply['needs']) || 'clarify', proposal: ev.proposal ?? null, plan: ev.plan ?? null, reply: ev.reply || '' }
        if (ev.type === 'error') throw new Error(ev.message || 'agent error')
      } catch (e) { if (e instanceof Error && e.message !== 'agent error') throw e }
    }
  }
  return result
}