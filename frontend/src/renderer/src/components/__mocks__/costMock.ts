
export const USE_MOCK = false

const now = Date.now()
const day = 86400_000

function rnd(min: number, max: number) { return Math.floor(Math.random() * (max - min + 1)) + min }

/** 生成 N 天的每日聚合 */
export function mockDaily(days: number) {
  return Array.from({ length: days }, (_, i) => {
    const d = new Date(now - (days - 1 - i) * day)
    const calls = rnd(5, 40)
    const sessions = rnd(2, Math.min(calls, 12))
    const input = rnd(2000, 18000)
    const output = rnd(800, 6000)
    return {
      date: d.toISOString().slice(0, 10),
      calls,
      sessions,
      input_tokens: input,
      output_tokens: output,
      total_tokens: input + output,
      elapsed_ms_avg: rnd(80, 350),
    }
  })
}

const MODELS = ['deepseek-v4-flash', 'gpt-4o-mini']
const SESSION_IDS = [
  'sess_a3f2c1d8b9e047f1',
  'sess_7b1e4f092c3a88d2',
  'sess_9c0d3e5a1b7f66a3',
  'sess_e2f8a0c4d6b199e5',
  'sess_1a5b7c9d3e8f22b0',
  'sess_4d6f8a0b2c5e77c1',
]

const SAMPLE_REQUESTS: Record<string, object> = {
  query: {
    model: 'deepseek-v4-flash',
    messages: [
      { role: 'system', content: 'You are a SQL assistant...' },
      { role: 'user', content: '查询所有用户的订单数量' },
    ],
    tools: [{ type: 'function', function: { name: 'run_query' } }],
    stream: true,
    stream_options: { include_usage: true },
  },
  write: {
    model: 'deepseek-v4-flash',
    messages: [
      { role: 'system', content: 'You are a SQL assistant...' },
      { role: 'user', content: '把张三的邮箱改成 zhangsan@example.com' },
    ],
    tools: [{ type: 'function', function: { name: 'run_dml' } }],
    stream: true,
    stream_options: { include_usage: true },
  },
  report: {
    model: 'deepseek-v4-flash',
    messages: [
      { role: 'system', content: 'You are a data analyst...' },
      { role: 'user', content: '生成本月销售报告' },
    ],
    stream: true,
    stream_options: { include_usage: true },
  },
}

const SAMPLE_RESPONSES: Record<string, object> = {
  query: {
    id: 'chatcmpl-abc123',
    object: 'chat.completion',
    choices: [{
      index: 0,
      message: { role: 'assistant', content: null, tool_calls: [{ id: 'call_01', type: 'function', function: { name: 'run_query', arguments: '{"sql":"SELECT u.name, COUNT(o.id) as order_count FROM users u LEFT JOIN orders o ON o.user_id = u.id GROUP BY u.id"}' } }] },
      finish_reason: 'tool_calls',
    }],
    usage: { prompt_tokens: 1250, completion_tokens: 180, total_tokens: 1430 },
  },
  write: {
    id: 'chatcmpl-def456',
    object: 'chat.completion',
    choices: [{
      index: 0,
      message: { role: 'assistant', content: null, tool_calls: [{ id: 'call_02', type: 'function', function: { name: 'run_dml', arguments: '{"sql":"UPDATE users SET email = \'zhangsan@example.com\' WHERE name = \'张三\'"}' } }] },
      finish_reason: 'tool_calls',
    }],
    usage: { prompt_tokens: 980, completion_tokens: 95, total_tokens: 1075 },
  },
  report: {
    id: 'chatcmpl-ghi789',
    object: 'chat.completion',
    choices: [{
      index: 0,
      message: { role: 'assistant', content: '本月销售总额为 ¥128,450，环比增长 12.3%...' },
      finish_reason: 'stop',
    }],
    usage: { prompt_tokens: 3200, completion_tokens: 850, total_tokens: 4050 },
  },
}

function mockCallDetail(id: number, skill: string, model: string) {
  const input = rnd(400, 4000)
  const output = rnd(100, 1500)
  return {
    id,
    ts: new Date(now - rnd(0, 3) * day + rnd(0, 86400_000)).toISOString().slice(0, 19),
    conn_id: 'demo',
    skill,
    model,
    provider: 'deepseek',
    session_id: '',
    input_tokens: input,
    output_tokens: output,
    elapsed_ms: rnd(50, 500),
    request_json: SAMPLE_REQUESTS[skill] || SAMPLE_REQUESTS.query,
    response_json: SAMPLE_RESPONSES[skill] || SAMPLE_RESPONSES.query,
  }
}

function mockCalls(sid: string, n: number) {
  const base = now - rnd(0, 3) * day
  const llmSkills = ['query', 'write', 'report']
  return Array.from({ length: n }, (_, i) => {
    const skill = llmSkills[rnd(0, llmSkills.length - 1)]
    const model = MODELS[rnd(0, 1)]
    const input = rnd(400, 4000)
    const output = rnd(100, 1500)
    return {
      id: rnd(1000, 99999),
      ts: new Date(base + i * rnd(2000, 60000)).toISOString().slice(0, 19),
      conn_id: 'demo',
      skill,
      model,
    provider: 'deepseek',
      session_id: sid,
      input_tokens: input,
      output_tokens: output,
      elapsed_ms: rnd(50, 500),
    }
  }).sort((a, b) => a.ts.localeCompare(b.ts))
}

export function mockSessions() {
  return SESSION_IDS.map(sid => {
    const calls = mockCalls(sid, rnd(3, 15))
    const total = calls.reduce((s, c) => s + c.input_tokens + c.output_tokens, 0)
    const skills = [...new Set(calls.map(c => c.skill))].join(', ')
    return {
      session_id: sid,
      calls: calls.length,
      total_tokens: total,
      input_tokens: calls.reduce((s, c) => s + c.input_tokens, 0),
      output_tokens: calls.reduce((s, c) => s + c.output_tokens, 0),
      first_ts: calls[0].ts,
      last_ts: calls[calls.length - 1].ts,
      skills,
      conn_id: 'demo',
    }
  }).sort((a, b) => b.last_ts.localeCompare(a.last_ts))
}

/** 缓存：同一批 mock 数据在 session 期间不变（刷新页面重新生成） */
let _cachedSessions: ReturnType<typeof mockSessions> | null = null
let _cachedCalls: Map<string, ReturnType<typeof mockCalls>> | null = null

function ensureCache() {
  if (!_cachedSessions) {
    _cachedSessions = mockSessions()
    _cachedCalls = new Map()
    for (const s of _cachedSessions) {
      _cachedCalls!.set(s.session_id, mockCalls(s.session_id, s.calls))
    }
  }
}

export function getMockSessions() {
  ensureCache()
  return _cachedSessions!
}

export function getMockCalls(sid: string) {
  ensureCache()
  return _cachedCalls!.get(sid) || []
}

export function getMockCallDetail(callId: number) {
  // 根据 ID 生成确定性的 mock 详情
  const skills = ['query', 'write', 'report']
  const skill = skills[callId % skills.length]
  const model = callId % 2 === 0 ? 'deepseek-v4-flash' : 'gpt-4o-mini'
  return mockCallDetail(callId, skill, model)
}

export function getMockSummary(days: number) {
  const daily = mockDaily(days)
  const total_calls = daily.reduce((s, d) => s + d.calls, 0)
  const input_tokens = daily.reduce((s, d) => s + d.input_tokens, 0)
  const output_tokens = daily.reduce((s, d) => s + d.output_tokens, 0)
  return {
    total_calls,
    total_tokens: input_tokens + output_tokens,
    input_tokens,
    output_tokens,
    total_cost_usd: (input_tokens * 0.15 + output_tokens * 0.6) / 1_000_000,
  }
}
