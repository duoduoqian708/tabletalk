// E2/E3 闭环：team 建用户→提 REVIEW→转审批→admin 批→usage 计数
import { chromium } from '@playwright/test'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tabletalk-approvals'
const port = 8775
const baseUrl = `http://127.0.0.1:${port}`
fs.rmSync(dataDir,{recursive:true, force:true})
fs.mkdirSync(dataDir,{recursive:true})
const py = fs.existsSync(path.join(backendDir, '.venv/bin/python')) ? path.join(backendDir, '.venv/bin/python') : 'python3'
const sidecar = spawn(py, ['-m','uvicorn','app.main:app','--port',String(port)], {cwd: backendDir, env:{...process.env, TABLETALK_DATA_DIR: dataDir}, stdio:'ignore'})
async function waitHealth(t=30000){ const d=Date.now()+t; while(Date.now()<d){ try{ const r=await fetch(`${baseUrl}/api/v1/health`); if(r.ok) return }catch{} await new Promise(r=>setTimeout(r,500))} throw new Error('health timeout')}
let pass=0,fail=0
function check(n,c,g){ if(c){pass++; console.log(`PASS ${n} → ${g}`)} else {fail++; console.log(`FAIL ${n} → ${g}`)} }
const browser=await chromium.launch()
try{
  await waitHealth()
  const singleToken = await (await fetch(`${baseUrl}/api/v1/bootstrap`)).json().then(j=>j.token)
  // 注册 admin
  let r = await fetch(`${baseUrl}/api/v1/auth/register`, {method:'POST', headers:{'Content-Type':'application/json', 'X-TableTalk-Token': singleToken}, body: JSON.stringify({username:'admin', password:'admin123', role:'admin'})})
  let j = await r.json()
  const adminToken = j.token
  check('注册 admin', !!adminToken, adminToken?.slice(0,12) || 'no')
  // 注册 member
  r = await fetch(`${baseUrl}/api/v1/auth/register`, {method:'POST', headers:{'Content-Type':'application/json', 'X-TableTalk-Token': adminToken}, body: JSON.stringify({username:'bob', password:'bob123', role:'member'})})
  j = await r.json()
  const memberToken = j.token
  check('注册 member', !!memberToken, memberToken?.slice(0,12) || 'no')
  // 建连接（用 admin）
  r = await fetch(`${baseUrl}/api/v1/connections`, {method:'POST', headers:{'Content-Type':'application/json', 'X-TableTalk-Token': adminToken}, body: JSON.stringify({name:'appr-test', dialect:'sqlite', file: `${dataDir}/demo.db`, read_only:false})})
  j = await r.json()
  const cid = j.id
  check('建连接', !!cid, cid||'no')
  // 触发知识库构建（简化：直接设 ready）
  // 通过 API 触发 build
  await fetch(`${baseUrl}/api/v1/knowledge/${cid}/build`, {method:'POST', headers:{'X-TableTalk-Token': adminToken, 'Content-Type':'application/json'}, body:'{}'})
  for(let i=0;i<15;i++){
    await new Promise(r=>setTimeout(r,800))
    const ov = await (await fetch(`${baseUrl}/api/v1/knowledge/${cid}/overview`, {headers:{'X-TableTalk-Token': adminToken}})).json()
    if(ov.kb_status==='pending_review' || ov.kb_status==='ready') break
  }
  await fetch(`${baseUrl}/api/v1/knowledge/${cid}/confirm-all`, {method:'POST', headers:{'X-TableTalk-Token': adminToken, 'Content-Type':'application/json'}, body:'{}'})
  // member 提 REVIEW（UPDATE）
  r = await fetch(`${baseUrl}/api/v1/query`, {method:'POST', headers:{'Content-Type':'application/json', 'X-TableTalk-Token': memberToken}, body: JSON.stringify({connection_id: cid, sql:"UPDATE orders SET status='paid' WHERE id=1", origin:'manual'})})
  let body = await r.json()
  check('member 提 REVIEW', body.verdict==='review', body.verdict)
  // 转审批（member 发起）
  r = await fetch(`${baseUrl}/api/v1/approvals`, {method:'POST', headers:{'Content-Type':'application/json', 'X-TableTalk-Token': memberToken}, body: JSON.stringify({connection_id: cid, sql:"UPDATE orders SET status='paid' WHERE id=1"})})
  j = await r.json()
  const aid = j.id
  check('转审批', !!aid, aid||'no')
  // admin 批
  r = await fetch(`${baseUrl}/api/v1/approvals/${aid}/approve`, {method:'POST', headers:{'Content-Type':'application/json', 'X-TableTalk-Token': adminToken}, body: JSON.stringify({note:'ok'})})
  j = await r.json()
  check('admin 批准', j.status==='approved', j.status||'no')
  // 审计链
  r = await fetch(`${baseUrl}/api/v1/audit?limit=50`, {headers:{'X-TableTalk-Token': adminToken}})
  const audit = await r.json()
  const hasApproval = audit.entries.some(e=> e.source==='approval')
  check('审计含 approval 链', hasApproval, hasApproval?'yes':'no')
  // E3 用量：先做一次 AI 查询以产生 egress
  await fetch(`${baseUrl}/api/v1/ai/chat`, {method:'POST', headers:{'Content-Type':'application/json', 'X-TableTalk-Token': adminToken}, body: JSON.stringify({connection_id: cid, messages:[{role:'user', content:'查订单总数'}]})}).catch(()=>{})
  await new Promise(r=>setTimeout(r,1500))
  r = await fetch(`${baseUrl}/api/v1/usage`, {headers:{'X-TableTalk-Token': adminToken}})
  const usage = await r.json()
  check('E3 用量统计', typeof usage.by_user==='object', JSON.stringify(usage.by_user).slice(0,60))
  check('E3 用量含 egress', usage.total_egress>=1 || Object.keys(usage.by_model).length>0, `egress:${usage.total_egress} model:${JSON.stringify(usage.by_model).slice(0,40)}`)
  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail?1:0
}catch(e){
  console.log('FAIL:', String(e).slice(0,800))
  console.log(e.stack?.slice(0,800))
  process.exitCode=1
}finally{
  await browser.close().catch(()=>{})
  sidecar.kill()
}
