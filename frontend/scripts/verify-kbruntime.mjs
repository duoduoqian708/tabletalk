// 知识库前端运行时摸排：绕过硬 API 构建，直接用 demo 库 + 真实 /overview 渲染三个核心视图
import { chromium } from '@playwright/test'
import { spawn, execSync } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'
const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tt-kbruntime'
const port = 8768, baseUrl = `http://127.0.0.1:${port}`
fs.rmSync(dataDir, { recursive: true, force: true }); fs.mkdirSync(dataDir, { recursive: true })
const py = path.join(backendDir, '.venv/bin/python')
const sc = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], { cwd: backendDir, env: {...process.env, TABLETALK_DATA_DIR: dataDir}, stdio: 'ignore' })
const sleep = (ms) => new Promise(r => setTimeout(r, ms))
async function health(t=40000){const d=Date.now()+t;while(Date.now()<d){try{if((await fetch(baseUrl+'/api/v1/health')).ok)return}catch{}await sleep(600)}throw new Error('health')}
// 直接走 HTTP API：建连接→构建→等 pending→confirm-all → /overview
async function api(path_, opts={}){ const r = await fetch(baseUrl+path_, {headers:{'Content-Type':'application/json', 'X-TableTalk-Token': (await (await fetch(baseUrl+'/api/v1/bootstrap')).json()).token}, ...opts}); return {status:r.status, body: await r.json().catch(()=>null)} }
const b = await chromium.launch()
try{
  await health()
  // 建连接并构建
  // 播种真实演示库（含 FK/枚举/多表），连接指向它
  execSync(`${JSON.stringify(py)} scripts/seed_demo_db.py ${dataDir}/demo.db`, {cwd: backendDir})
  const dbPath = path.join(dataDir, 'demo.db')
  await api('/api/v1/connections', {method:'POST', body: JSON.stringify({name:'demo', dialect:'sqlite', file: dbPath})})
  const boot = await (await fetch(baseUrl+'/api/v1/bootstrap')).json()
  const hdr = {'Content-Type':'application/json', 'X-TableTalk-Token': boot.token}
  const conn = (await (await fetch(baseUrl+'/api/v1/connections', {headers:hdr})).json())[0]
  const cid = conn.id
  await fetch(baseUrl+`/api/v1/knowledge/${cid}/build`, {method:'POST', headers:hdr})
  // 轮询待构建完成
  for(let i=0;i<60;i++){ const p = await (await fetch(baseUrl+`/api/v1/knowledge/${cid}/build/progress`,{headers:hdr})).json(); if(p.done && !p.error) break; await sleep(1000) }
  const stBefore = await (await fetch(baseUrl+`/api/v1/knowledge/${cid}/status`,{headers:hdr})).json()
  console.log('STATUS-BEFORE:', JSON.stringify(stBefore))
  // 不 confirm-all：保留提案期（draft_docs>0），验证对比/待审批/图 diff
  const ov = await (await fetch(baseUrl+`/api/v1/knowledge/${cid}/overview`,{headers:hdr})).json()
  console.log('OVERVIEW: built=%s tables=%d draft_count=%d', ov.built, ov.tables?.length, ov.draft_count)
  console.log('OVERVIEW-TBL0:', JSON.stringify({name:ov.tables?.[0]?.name, hasProposed: !!ov.tables?.[0]?.proposed_comment, cols: ov.tables?.[0]?.columns?.length}))
  console.log('OVERVIEW-GRAPH: edges=%d llm_draft=%d firstEdgeDiff=%s', ov.graph?.edges?.length, ov.graph?.llm_draft_edges?.length, (ov.graph?.edges||[])[0]?.diff)
  // 渲染三个视图
  const pg = await b.newPage({ viewport:{width:1480,height:940} })
  pg.on('pageerror', e => console.log('PAGE-ERR:', String(e).slice(0,260)))
  pg.on('console', m => { if(m.type()==='error') console.log('CONSOLE-ERR:', m.text().slice(0,260)) })
  await pg.goto(baseUrl, {waitUntil:'domcontentloaded'})
  await pg.waitForSelector('.appbar, .onboarding', {timeout:40000})
  await pg.click('button:has-text("知识库")').catch(()=>{})
  // 等审查页渲染
  await pg.waitForSelector('.review, .kb-cols-3', {timeout:20000}).catch(()=>{})
  await pg.waitForTimeout(1200)
  const tagCount = await pg.locator('.kb-tag-row').count().catch(()=>-1)
  const curTags = await pg.locator('.kb-current-tags .kb-tag-row').count().catch(()=>-1)
  const pendTags = await pg.locator('.kb-pending-tags .kb-tag-row').count().catch(()=>-1)
  const tables = await pg.locator('.rv-table').count().catch(()=>-1)
  const detailOpen = await pg.locator('.tdp').count().catch(()=>-1)
  console.log('RENDER: tagRows=%d currentTags=%d pendingTags=%d tables=%d tdp=%d', tagCount, curTags, pendTags, tables, detailOpen)
  // 点第一个表详情看字段对比按钮
  await pg.locator('.rv-table-row').first().click().catch(()=>{})
  await pg.waitForTimeout(600)
  const cmpBtn = await pg.locator('button[title]').evaluateAll(btns => btns.filter(x=>x.getAttribute('title')==='对比当前/提案').length).catch(()=> -1)
  const cmpCount = await pg.locator('.kb-cmp').count().catch(()=>0)
  const stDot = await pg.locator('.st-dot').count().catch(()=>-1)
  console.log('DETAIL: compareBtn=%d stDot=%d', cmpBtn, stDot)
  await pg.screenshot({path:'/tmp/kb-pending-detail.png'}).catch(()=>{})
  // 图库 tab
  await pg.click('button:has-text("图库")').catch(()=>{})
  await pg.waitForTimeout(800)
  await pg.click('button:has-text("2D")').catch(()=>{})
  await pg.waitForTimeout(800)
  const g2d = await pg.locator('.trg2d-edge').count().catch(()=>-1)
  const diffNew = await pg.locator('.trg2d-edge.diff-new').count().catch(()=>0)
  const diffMod = await pg.locator('.trg2d-edge.diff-modified').count().catch(()=>0)
  const diffRed = await pg.locator('.trg2d-edge.diff-removed').count().catch(()=>0)
  console.log('GRAPH-TAB: edges=%d diff-new=%d diff-mod=%d diff-removed=%d', g2d, diffNew, diffMod, diffRed)
  await pg.screenshot({path:'/tmp/kb-pending-graph.png'}).catch(()=>{})
  await pg.screenshot({path:'/tmp/kb-three-views.png'}).catch(()=>{})
}catch(e){ console.log('FAIL:', String(e).slice(0,400)) } finally { await b.close().catch(()=>{}); sc.kill() }
