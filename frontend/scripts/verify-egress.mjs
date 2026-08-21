// B1 + A1 回归：出网清单可枚举 & 拦截可解释
import { chromium } from '@playwright/test'
import { buildAndConfirm } from './lib-onboard.mjs'
import { spawn } from 'node:child_process'
import path from 'node:path'
import fs from 'node:fs'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const root = path.resolve(__dirname, '..')
const backendDir = path.resolve(root, '../backend')
const dataDir = '/tmp/tabletalk-egress'
const port = 8772
const baseUrl = `http://127.0.0.1:${port}`

fs.rmSync(dataDir, { recursive: true, force: true })
fs.mkdirSync(dataDir, { recursive: true })
const py = fs.existsSync(path.join(backendDir, '.venv/bin/python')) ? path.join(backendDir, '.venv/bin/python') : 'python3'
const sidecar = spawn(py, ['-m', 'uvicorn', 'app.main:app', '--port', String(port)], {
  cwd: backendDir, env: { ...process.env, TABLETALK_DATA_DIR: dataDir }, stdio: 'ignore',
})
async function waitHealth(t=30000){ const d=Date.now()+t; while(Date.now()<d){ try{ const r=await fetch(`${baseUrl}/api/v1/health`); if(r.ok) return }catch{} await new Promise(r=>setTimeout(r,500))} throw new Error('health timeout')}
let pass=0,fail=0
function check(name, cond, got){ if(cond){ pass++; console.log(`PASS ${name} → ${got}`)} else { fail++; console.log(`FAIL ${name} → ${got}`)} }
const browser=await chromium.launch()
try{
  await waitHealth()
  const page=await browser.newPage({viewport:{width:1480,height:940}})
  await page.goto(baseUrl,{waitUntil:'domcontentloaded'})
  await page.waitForSelector('.appbar, .onboarding',{timeout:40000})
  const onboard=await page.$('.onboarding .primary')
  if(onboard){ await onboard.click(); await page.waitForSelector('.appbar',{timeout:20000})}
  await buildAndConfirm(page)
  await page.waitForSelector('.graph3d',{timeout:20000})
  await page.waitForTimeout(800)

  // 获取 bootstrap token 以便直接调审计 API
  const token = await page.evaluate(async () => {
    const r = await fetch('/api/v1/bootstrap'); const j = await r.json(); return j.token
  })
  // ---------- B1：发送一条查询，检查 manifest ----------
  // 点输入框，输入问题
  page.on('console', msg=> { if(msg.text().includes('manifest')) console.log('BROWSER LOG:', msg.text()) })
  await page.click('.compose-input')
  await page.fill('.compose-input','查订单总数')
  await page.click('.ai-bar .send')
  // 等待 manifest 出现（放宽为 attached，且等待 ai turn 出现）
  await page.waitForSelector('.m-a',{timeout:20000})
  await page.waitForTimeout(1500)
  const hasManifest = await page.evaluate(()=> !!document.querySelector('.manifest'))
  if(!hasManifest){
    const html = await page.evaluate(()=> document.documentElement.outerHTML.slice(0,6000))
    console.log('DEBUG no manifest, html snippet:', html.slice(0,2000))
    const ls = await page.evaluate(()=> localStorage.getItem('tabletalk-chats-v1')?.slice(0,2000))
    console.log('LS snippet:', ls)
  }
  await page.waitForSelector('.manifest',{state:'attached', timeout:10000})
  const manifestText = await page.textContent('.manifest-head .manifest-human')
  check('B1 manifest 折叠文案出现', !!manifestText && manifestText.includes('张表'), manifestText||'—')
  // 展开查看详情
  await page.click('.manifest-head')
  await page.waitForSelector('.manifest-body',{timeout:5000})
  const body = await page.textContent('.manifest-body')
  check('B1 manifest 展开含 tables/kb_docs', !!body && body.includes('tables') && body.includes('kb_docs'), body?.slice(0,80)||'—')
  // 等待文本回复
  await page.waitForSelector('.ai-txt',{timeout:20000})
  // 检查审计 egress
  const egressAudit = await page.evaluate(async (tok) => {
    const r = await fetch('/api/v1/audit?limit=100', {headers:{'X-TableTalk-Token': tok}})
    const j = await r.json(); return j.entries.filter((e)=>e.verdict==='egress')
  }, token)
  check('B1 审计 egress 存在', egressAudit.length>=1, `egress=${egressAudit.length}`)
  if(egressAudit.length>0){
    const m = egressAudit[egressAudit.length-1].manifest
    check('B1 审计 manifest 与 SSE 一致（含 tables）', !!m && Array.isArray(m.tables), JSON.stringify(m?.tables||''))
    check('B1 manifest 含 kb_docs/history_turns', typeof m?.kb_docs==='number' && typeof m?.history_turns==='number', `kb=${m?.kb_docs} hist=${m?.history_turns}`)
    check('B1 manifest 出网可枚举（有 ts/model）', !!m?.ts && !!m?.provider, `ts=${m?.ts} prov=${m?.provider}`)
  }
  // 再次发送，验证出网可枚举率 100%（第二次也有）
  await page.click('.compose-input')
  await page.fill('.compose-input','按月统计销售额')
  await page.click('.ai-bar .send')
  await page.waitForTimeout(1500)
  await page.waitForSelector('.manifest',{timeout:10000})
  const egress2 = await page.evaluate(async (tok)=>{
    const r=await fetch('/api/v1/audit?limit=100',{headers:{'X-TableTalk-Token':tok}})
    const j=await r.json(); return j.entries.filter((e)=>e.verdict==='egress').length
  }, token)
  check('B1 第二次发送仍有 manifest（可枚举 100%）', egress2>=2, `egress2=${egress2}`)

  // ---------- A1：拦截可解释 ----------
  // 直接通过 API 触发 BLOCK（UPDATE 无 WHERE）并检查审计 reasons
  const blockRes = await page.evaluate(async (tok)=>{
    // 找一个 writable 连接（先列出）
    const r=await fetch('/api/v1/connections',{headers:{'X-TableTalk-Token':tok}})
    const conns=await r.json()
    let cid=conns.find((c)=>!c.read_only && c.kb_status==='ready')?.id
    if(!cid){
      // 尝试用第一个
      cid=conns[0]?.id
    }
    if(!cid) return {err:'no conn'}
    const q=await fetch('/api/v1/query',{method:'POST', headers:{'X-TableTalk-Token':tok,'Content-Type':'application/json'}, body: JSON.stringify({connection_id:cid, sql:"UPDATE orders SET status='paid'", origin:'manual'})})
    const j=await q.json()
    return {cid, body:j}
  }, token)
  if(blockRes.body){
    check('A1 BLOCK 接口返回 reasons 结构化', Array.isArray(blockRes.body.reasons) && blockRes.body.reasons.length>0, JSON.stringify(blockRes.body.reasons||'').slice(0,120))
    if(blockRes.body.reasons && blockRes.body.reasons[0]){
      check('A1 reason 含 rule_id/message/objects', !!blockRes.body.reasons[0].rule_id && !!blockRes.body.reasons[0].message, blockRes.body.reasons[0].rule_id)
      const okRule = ['dml-no-where','read-only','dml-confirm'].includes(blockRes.body.reasons[0].rule_id)
      check('A1 reason 含有效 rule_id', okRule, blockRes.body.reasons[0].rule_id)
    }
  }
  // 检查审计中 BLOCK 也有 reasons
  const blockAudit = await page.evaluate(async (tok)=>{
    const r=await fetch('/api/v1/audit?limit=50',{headers:{'X-TableTalk-Token':tok}})
    const j=await r.json(); return j.entries.filter((e)=>e.verdict==='block').slice(-1)[0]
  }, token)
  check('A1 审计 BLOCK 含 reasons', Array.isArray(blockAudit?.reasons) && blockAudit.reasons.length>0, JSON.stringify(blockAudit?.reasons||'').slice(0,120))
  // 前端卡片展示：通过 AI 触发一个 DML 无 WHERE（mock 关键词“提价 库存为0”）
  await page.click('.compose-input')
  await page.fill('.compose-input','把库存为 0 的商品提价 10%')
  await page.click('.ai-bar .send')
  await page.waitForSelector('.sql.block, .sql.review',{timeout:20000})
  const cardReasons = await page.$$eval('.rp-reason', els=>els.map(e=>e.textContent?.slice(0,60)||''))
  check('A1 前端卡片按条渲染 reasons', cardReasons.length>0, `reasons=${cardReasons.length}`)
  if(cardReasons.length>0){
    const hasRule = cardReasons[0].includes('read-only') || cardReasons[0].includes('dml') || cardReasons[0].includes('BLOCK') || cardReasons[0].includes('review')
    check('A1 卡片含 rule_id', hasRule, cardReasons[0])
  }
  // 审计展开也应显示 reasons
  await page.click('button:has-text("安全与审计")')
  await page.waitForSelector('.audit-table',{timeout:10000})
  await page.waitForTimeout(500)
  // 点第一行展开
  const firstRow = await page.$('.audit-table tbody tr.clickable')
  if(firstRow){ await firstRow.click(); await page.waitForSelector('.exp-reasons',{timeout:5000}).catch(()=>{}) }
  const exp = await page.$('.exp-reasons')
  check('A1 审计展开显示 reasons', !!exp, exp? '有':'无')
  // 返回工作台以便后续 A3 测试
  await page.click('button:has-text("工作台")')
  await page.waitForSelector('.graph3d',{timeout:10000})

  // ---------- A3：爆炸半径 ----------
  const blastRes = await page.evaluate(async (tok) => {
    // 找一个可写连接，若无则新建
    let conns = await (await fetch('/api/v1/connections',{headers:{'X-TableTalk-Token':tok}})).json()
    let cid = conns.find(c=>!c.read_only && c.kb_status==='ready')?.id
    if(!cid){
      const r = await fetch('/api/v1/connections',{method:'POST', headers:{'X-TableTalk-Token':tok,'Content-Type':'application/json'}, body: JSON.stringify({name:'a3-test-'+Date.now(), dialect:'sqlite', file: '/Users/mac/.tabletalk/demo.db', read_only:false})})
      const j = await r.json()
      cid = j.id
      // 触发构建
      await fetch(`/api/v1/knowledge/${cid}/build`,{method:'POST', headers:{'X-TableTalk-Token':tok,'Content-Type':'application/json'}, body:'{}'})
      for(let i=0;i<20;i++){
        await new Promise(res=>setTimeout(res,800))
        const ov = await (await fetch(`/api/v1/knowledge/${cid}/overview`,{headers:{'X-TableTalk-Token':tok}})).json()
        if(ov.kb_status==='pending_review' || ov.kb_status==='ready') break
      }
      // confirm
      await fetch(`/api/v1/knowledge/${cid}/confirm-all`,{method:'POST', headers:{'X-TableTalk-Token':tok,'Content-Type':'application/json'}, body:'{}'})
      await new Promise(res=>setTimeout(res,500))
    }
    const q = await fetch('/api/v1/query',{method:'POST', headers:{'X-TableTalk-Token':tok,'Content-Type':'application/json'}, body: JSON.stringify({connection_id:cid, sql:"UPDATE orders SET status='paid' WHERE id=1", origin:'manual'})})
    const body = await q.json()
    return body
  }, token)
  check('A3 blast 存在于 REVIEW', !!blastRes.blast, blastRes.blast? '有':'无')
  if(blastRes.blast){
    check('A3 direct 为 orders', blastRes.blast.direct[0]?.table==='orders', blastRes.blast.direct[0]?.table||'—')
    const cascadeTables = blastRes.blast.cascade.map(c=>c.table)
    check('A3 cascade 含 order_items', cascadeTables.includes('order_items'), cascadeTables.join(','))
    check('A3 cascade 含 payments', cascadeTables.includes('payments'), cascadeTables.join(','))
    check('A3 无 FK 表不出现在 cascade', !cascadeTables.includes('big_values'), 'big_values not in '+cascadeTables.join(','))
    check('A3 blast 与星图一致（约束含 FK）', blastRes.blast.constraints.length>0, blastRes.blast.constraints.join(';').slice(0,80))
  }

  console.log(`\nRESULT: ${pass} pass / ${fail} fail`)
  process.exitCode = fail?1:0
}catch(e){
  console.log('FAIL:', String(e).slice(0,600))
  console.log(e.stack?.slice(0,800))
  process.exitCode=1
}finally{
  await browser.close().catch(()=>{})
  sidecar.kill()
}
