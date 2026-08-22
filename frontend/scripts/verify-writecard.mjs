// WS2 验证：query 只读化后，写意图(preflight)仍路由 write 技能 → run_dml → HITL 黄卡(review)，
// 且不自动执行。服务端 SSE 级验证（绕过 WS6 画布图 .g-node 已不存在的陈旧 DOM 断言）。
// 用法：node scripts/verify-writecard.mjs
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const backendDir = path.resolve(new URL('.', import.meta.url).pathname, '../../backend')
const dataDir = '/tmp/tabletalk-wcard'
const port = 8780
const baseUrl = `http://127.0.0.1:${port}`
fs.rmSync(dataDir, { recursive: true, force: true }); fs.mkdirSync(dataDir, { recursive: true })
const py = fs.existsSync(path.join(backendDir, '.venv/bin/python')) ? path.join(backendDir, '.venv/bin/python') : 'python3'
const sidecar = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], { cwd: backendDir, env: { ...process.env, TABLETALK_DATA_DIR: dataDir }, stdio: 'ignore' })
const waitHealth = async (t = 30000) => { const d = Date.now() + t; while (Date.now() < d) { try { const r = await fetch(`${baseUrl}/api/v1/health`); if (r.ok) return } catch {} await new Promise(r => setTimeout(r, 400)) } throw new Error('health timeout') }
let pass = 0, fail = 0
const check = (n, c, g) => { if (c) { pass++; console.log(`PASS ${n} → ${g}`) } else { fail++; console.log(`FAIL ${n} → ${g}`) } }

try {
  await waitHealth()
  const boot = await (await fetch(`${baseUrl}/api/v1/bootstrap`)).json()
  const H = { 'Content-Type': 'application/json', 'X-TableTalk-Token': boot.token }
  const res = await fetch(`${baseUrl}/api/v1/connections`, { method: 'POST', headers: H, body: JSON.stringify({ name: '可写演示库', dialect: 'sqlite', file: `${dataDir}/demo.db`, read_only: false }) })
  check('创建可写连接', res.ok, String(res.status))
  const connId = (await res.json()).id

  // 构建并启用知识库（chat 前置）
  await fetch(`${baseUrl}/api/v1/knowledge/${connId}/build`, { method: 'POST', headers: H })
  let done = false
  for (let i = 0; i < 120 && !done; i++) {
    const p = await (await fetch(`${baseUrl}/api/v1/knowledge/${connId}/build/progress`, { headers: H })).json()
    if (p.done) done = true
    else await new Promise(r => setTimeout(r, 400))
  }
  check('知识库构建完成', done, String(done))
  await fetch(`${baseUrl}/api/v1/knowledge/${connId}/confirm-all`, { method: 'POST', headers: H }).catch(() => {})

  // 写意图 → write 技能 → run_dml → review 卡，不自动执行
  const r = await fetch(`${baseUrl}/api/v1/ai/chat`, { method: 'POST', headers: H, body: JSON.stringify({ connection_id: connId, messages: [{ role: 'user', content: '给库存为 0 的产品涨价 10%' }] }) })
  const txt = await r.text()
  const cards = []
  let calledDml = false
  for (const line of txt.split('\n')) {
    if (!line.startsWith('data:')) continue
    let j; try { j = JSON.parse(line.slice(5).trim()) } catch { continue }
    if (j.type === 'think' && String(j.text).includes('run_dml')) calledDml = true
    if (j.type === 'sql_card') cards.push(j.card)
  }
  check('写意图走 run_dml 工具', calledDml, String(calledDml))
  const review = cards.some(c => String(c.verdict).includes('review'))
  check('写操作出黄卡(verdict=review)', review, JSON.stringify(cards.map(c => c.verdict)).slice(0, 60))
  const autoAllow = cards.some(c => String(c.verdict).toLowerCase() === 'allow' && String(c.tier).toLowerCase().includes('writ'))
  check('写操作未自动执行(无 allow)', !autoAllow, autoAllow ? '被自动执行了!' : '未自动执行 ✓')

  console.log(`\nWS2 写卡路由验证: ${pass} passed, ${fail} failed`)
} finally {
  sidecar.kill('SIGTERM')
  process.exit(fail ? 1 : 0)
}