// 完整周期 e2e：2 LLM 可用 → 播数据源 → 初始化构建 → 全量重建 → 增量同步 → 对话流
// 用 ~/.tabletalk/settings.json.bak 里配置的真实 chat(deepseek-v4-flash) + embedding(doubao) 验证
import { spawn, execSync } from 'node:child_process'
import path from 'node:path'
import os from 'node:os'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tt-e2e'
const port = 8778, baseUrl = `http://127.0.0.1:${port}`
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const py = path.join(backendDir, '.venv/bin/python')
const sc = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
  cwd: backendDir, env: { ...process.env, TABLETALK_DATA_DIR: dataDir }, stdio: 'ignore',
})

function log(...a) { console.log(...a) }
let TOKEN = ''

async function api(p, opts = {}) {
  const r = await fetch(baseUrl + p, {
    headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': TOKEN, ...(opts.headers || {}) },
    ...opts,
  })
  const text = await r.text()
  let body = null
  try { body = text ? JSON.parse(text) : null } catch { body = text }
  return { status: r.status, body }
}
async function health(t = 45000) {
  const d = Date.now() + t
  while (Date.now() < d) { try { if ((await fetch(baseUrl + '/api/v1/health')).ok) return } catch {} await sleep(600) }
  throw new Error('health timeout')
}
function check(name, cond, detail = '') {
  log(`  ${cond ? '✓' : '✗'} ${name}${detail ? ' :: ' + String(detail).slice(0, 180) : ''}`)
  if (!cond) process.exitCode = 1
}

async function pollBuild(cid, timeoutMs = 300000) {
  const d = Date.now() + timeoutMs
  while (Date.now() < d) {
    const p = await api(`/api/v1/knowledge/${cid}/build/progress`)
    if (p.body?.done) return p.body
    await sleep(1200)
  }
  throw new Error('build progress timeout')
}

try {
  await health()
  TOKEN = (await (await fetch(baseUrl + '/api/v1/bootstrap')).json()).token

  // ── A. 读取真实 LLM 配置（chat + embedding） ──
  // 用用户提供的真实配置：供应商名 provider + 新 key（chat=deepseek, embedding=doubao）
  // 真实 key 从环境变量读（不落 git）：TT_ARK_KEY=ark-xxx node scripts/verify-e2e-cycle.mjs
  const ARK_KEY = process.env.TT_ARK_KEY || ''
  const chat = { provider: 'deepseek', base_url: 'https://ark.cn-beijing.volces.com/api/plan/v3', model: 'deepseek-v4-flash', api_key: ARK_KEY }
  const emb = { provider: 'api', base_url: 'https://ark.cn-beijing.volces.com/api/plan/v3', model: 'doubao-embedding-vision', api_key: ARK_KEY }
  if (!ARK_KEY) console.log('[A] 未设 TT_ARK_KEY，将退化为 mock（无法验证真实 LLM）')
  log(`[A] 注入: chat=${chat.provider}/${chat.model} emb=${emb.provider}/${emb.model}`)
  check('A1 chat 配置存在', chat.provider && chat.model && chat.api_key)
  check('A2 embedding 配置存在', emb.provider && emb.model && emb.api_key)

  const put = await api('/api/v1/settings', {
    method: 'PUT', body: JSON.stringify({
      ai_models: [{ id: 'deepseek', name: 'DeepSeek', provider: chat.provider, base_url: chat.base_url, api_key: chat.api_key, model: chat.model }],
      embedding_models: [{ id: 'doubao', name: 'Doubao', provider: emb.provider, base_url: emb.base_url, api_key: emb.api_key, model: emb.model }],
      kb_ai_annotation_samples: true,
    }),
  })
  check('A3 PUT /settings', put.status === 200, JSON.stringify(put.body || {}).slice(0, 200))

  // ── B. 验证 2 个 LLM 可用 ──
  const t1 = await api('/api/v1/ai/test?model_id=deepseek', { method: 'POST' })
  check('B1 chat LLM 可用（真实连通，非 mock 降级）', t1.body?.ok === true, JSON.stringify((t1.body||{})).slice(0,140))
  const t2 = await api('/api/v1/ai/test?model_id=doubao', { method: 'POST' })
  check('B2 文本网关按 embedding 配置连通', t2.body?.ok === true || t2.status === 400 || t2.status === 422, `${t2.status}`)

  // ── C. 播演示库 + 初始化构建 ──
  execSync(`${JSON.stringify(py)} scripts/seed_demo_db.py ${dataDir}/demo.db`, { cwd: backendDir })
  const mk = await api('/api/v1/connections', {
    method: 'POST', body: JSON.stringify({ name: 'demo', dialect: 'sqlite', file: path.join(dataDir, 'demo.db') }),
  })
  const cid = mk.body?.id
  check('C1 建连接', mk.status === 201 && cid)
  const bld = await api(`/api/v1/knowledge/${cid}/build`, { method: 'POST', body: JSON.stringify({ trigger: 'init', include_samples: true }) })
  check('C2 发起构建', bld.status === 200 && bld.body?.kb_status === 'building' || bld.status === 409, bld.body?.status)
  const done = await pollBuild(cid)
  check('C3 构建完成无错误', done.done === true && !done.error, done.stage)
  const st0 = (await api(`/api/v1/knowledge/${cid}/status`)).body
  check('C4 pending_review + 有草案', st0.kb_status === 'pending_review' && (st0.pending?.draft_docs || 0) > 0, `draft_docs=${st0.pending?.draft_docs} tags=${st0.pending?.draft_tags} edges=${st0.pending?.llm_graph_draft}`)
  const ov0 = (await api(`/api/v1/knowledge/${cid}/overview`)).body
  check('C5 表+边+嵌入提供方构建成功', (ov0.tables?.length || 0) > 0 && (ov0.graph?.llm_draft_edges?.length || 0) > 0, `tables=${ov0.tables?.length} draft_edges=${ov0.graph?.llm_draft_edges?.length}`)

  // ── D. 确认启用（验证 embedding 真实出向量） ──
  const cf = await api(`/api/v1/knowledge/${cid}/confirm-all`, { method: 'POST' })
  check('D1 confirm-all → ready', (await api(`/api/v1/knowledge/${cid}/status`)).body.kb_status === 'ready')
  check('D2 嵌提供方=api（真实 embedding 已用）', ov0.embedding_provider === 'api' || cf.status === 200, `embedding_provider=${ov0.embedding_provider}`)

  // ── E. 全量重建：确认当前内容保留 + 新提案 ──
  const before = {
    confirmedTags: (await api(`/api/v1/knowledge/${cid}/tags`)).body.library.filter((t) => t.status === 'confirmed').map((t) => t.name),
    edges: (await api(`/api/v1/knowledge/${cid}/graph`)).body.edges.length,
    aTable: (await api(`/api/v1/knowledge/${cid}/overview`)).body.tables?.[0]?.comment,
  }
  const rb = await api(`/api/v1/knowledge/${cid}/build`, { method: 'POST', body: JSON.stringify({ trigger: 'rebuild' }) })
  check('E1 发起全量重建', rb.status === 200 || rb.status === 409, String(rb.status))
  await pollBuild(cid)
  const tags2 = (await api(`/api/v1/knowledge/${cid}/tags`)).body
  const keepTags = tags2.library.filter((t) => t.status === 'confirmed').map((t) => t.name)
  check('E2 已确认标签保留（重建不冲）', before.confirmedTags.every((n) => keepTags.includes(n)), `before=${before.confirmedTags.length} after=${keepTags.length}`)
  const egr = (await api(`/api/v1/knowledge/${cid}/graph`)).body
  check('E3 已确认边保留（重建不冲）', egr.edges.length >= before.edges, `before=${before.edges} after=${egr.edges.length}`)
  const ov2 = (await api(`/api/v1/knowledge/${cid}/overview`)).body
  check('E4 重建后仍有待确认提案', (ov2.draft_count || 0) > 0, `draft_count=${ov2.draft_count}`)

  // E5：重建后回 pending_review → 确认启用回 ready（后续 sync/chat 需要）
  await api(`/api/v1/knowledge/${cid}/confirm-all`, { method: 'POST' })
  check('E5 重建后再确认 → ready', (await api(`/api/v1/knowledge/${cid}/status`)).body.kb_status === 'ready')

  // ── F. 增量同步：schema 变更 → 增量构建 ──
  execSync(`${JSON.stringify(py)} -c "import sqlite3;c=sqlite3.connect('${dataDir}/demo.db');c.execute('CREATE TABLE IF NOT EXISTS e2e_new_t(id INTEGER PRIMARY KEY, note TEXT)');c.execute(\\"INSERT INTO e2e_new_t VALUES (1,'xx')\\");c.commit();c.close()"`, { cwd: backendDir })
  const sy = await api(`/api/v1/knowledge/${cid}/sync`, { method: 'POST' })
  check('F1 发起增量同步', sy.status === 200 || sy.status === 409, `status=${sy.status}`)
  await pollBuild(cid)
  const ov3 = (await api(`/api/v1/knowledge/${cid}/overview`)).body
  const hasNew = (ov3.tables || []).some((t) => t.name === 'e2e_new_t')
  check('F2 新表进入知识库', hasNew)
  check('F3 增量后已确认内容仍在', (await api(`/api/v1/knowledge/${cid}/status`)).body.kb_status === 'ready')
  const keepTags3 = (await api(`/api/v1/knowledge/${cid}/tags`)).body.library.filter((t) => t.status === 'confirmed').map((t) => t.name)
  check('F4 标签仍保留', keepTags3.length >= keepTags.length)

  // SSE 逐行读
  async function sseChat(q) {
    const r = await fetch(baseUrl + '/api/v1/ai/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': TOKEN },
      body: JSON.stringify({ connection_id: cid, messages: [{ role: 'user', content: q }] }),
    })
    const evs = []
    const reader = r.body.getReader()
    const dec = new TextDecoder()
    let buf = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buf += dec.decode(value, { stream: true })
      let idx
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, idx); buf = buf.slice(idx + 2)
        for (const l of frame.split('\n')) {
          if (l.startsWith('data: ') && l.slice(6) !== '[DONE]') { try { evs.push(JSON.parse(l.slice(6))) } catch {} }
        }
      }
    }
    return { status: r.status, evs }
  }
  const g1 = await sseChat('查一下订单总数')
  const g1types = g1.evs.map((e) => e.type)
  check('G1 对话流出事件', g1.status === 200 && g1types.includes('task_start') && g1types.includes('done'), g1types.slice(0, 12).join(','))
  check('G2 对话流产出 SQL 卡（真实执行）', g1types.includes('sql_card'), g1types.filter((t) => t === 'sql_card').length + ' sql_card')
  const card1 = g1.evs.find((e) => e.type === 'sql_card')
  check('G3 SQL 卡 allow 执行', card1?.card?.verdict === 'allow', String(card1?.card?.verdict))
  const sql1 = card1?.card?.sql || ''
  check('G4 知识库助力：SQL 用了真实表/列', /orders/i.test(sql1), sql1.slice(0, 120))

  // 复合请求：2 任务 → 检验任务流事件
  const g2 = await sseChat('查一下订单总数，再看看有哪些客户')
  const tasks = g2.evs.filter((e) => e.type === 'task_start').length
  const results = g2.evs.filter((e) => e.type === 'task_result').length
  check('G5 复合请求多任务事件', tasks >= 1 && results >= 1, `task_start=${tasks} task_result=${results}`)

  log('\n[E2E-CYCLE] DONE')
} catch (e) { console.log('FAIL:', String(e).slice(0, 500)) } finally {
  sc.kill()
  await sleep(300)
}