import { useEffect, useState } from 'react'
import type { HealthStatus } from '@shared/types'
import { useConnections } from '@renderer/store/connections'
import { useKnowledge } from '@renderer/store/knowledge'
import { listAudit } from '@renderer/api/audit'
import type { AuditEntry } from '@renderer/api/types'
import { KnowledgeGraph } from './KnowledgeGraph'

function Badge({ v }: { v: string }): React.JSX.Element {
  const m: Record<string, [string, string]> = {
    allow: ['allow', '放行'],
    review: ['review', '需确认'],
    block: ['block', '拦截'],
    executed: ['exec', '已执行']
  }
  const [cls, label] = m[v] ?? ['manual', v]
  return <span className={`badge ${cls}`}>{label}</span>
}

/* ==================== 安全闸门 ==================== */
export function GatePage({ health }: { health: HealthStatus | null }): React.JSX.Element {
  const current = useConnections((s) => s.list.find((c) => c.id === s.currentId))
  const [recents, setRecents] = useState<AuditEntry[]>([])

  useEffect(() => {
    let alive = true
    void listAudit(current?.name).then((r) => alive && setRecents(r.slice(0, 8))).catch(() => undefined)
    return () => { alive = false }
  }, [current?.name])

  return (
    <div className="mpage">
      <div className="mpage-head">
        <h1>安全闸门 <span className="badge exec">armed</span></h1>
        <p>本地规则引擎，模型无关 —— AI 生成与手动执行的 SQL 走同一条闸门。DDL 是硬边界：AI 永不执行。</p>
      </div>
      <div className="mpage-body">
        <div className="tiers">
          <div className="tier read">
            <div className="t-h"><div className="t-ic">⌕</div><div className="t-t">读 READ</div><div className="t-st">放行</div></div>
            <div className="t-why">READ-ONLY · 直接执行</div>
            <div className="t-kw"><span>SELECT</span><span>SHOW</span><span>EXPLAIN</span><span>PRAGMA</span></div>
            <div className="t-d">查询直接放行，自动注入行数上限，标记只读。</div>
            <div className="t-rule">自动 LIMIT · 只读标记</div>
          </div>
          <div className="tier write">
            <div className="t-h"><div className="t-ic">✎</div><div className="t-t">写 DML</div><div className="t-st">需确认</div></div>
            <div className="t-why">WRITE · GATE REQUIRED</div>
            <div className="t-kw"><span>INSERT</span><span>UPDATE</span><span>DELETE</span></div>
            <div className="t-d">无 WHERE 直接拦截；有 WHERE 先预览受影响行数，需显式确认才执行。</div>
            <div className="t-rule">无 WHERE → BLOCK · 受影响行预览</div>
          </div>
          <div className="tier ddl">
            <div className="t-h"><div className="t-ic">▧</div><div className="t-t">结构 DDL</div><div className="t-st">仅手动</div></div>
            <div className="t-why">MANUAL ONLY · AI BLOCKED</div>
            <div className="t-kw"><span>CREATE</span><span>ALTER</span><span>DROP</span><span>TRUNCATE</span></div>
            <div className="t-d">AI 的工具有 DDL 草稿但永不执行；手动执行需红色强确认。</div>
            <div className="t-rule">draft_ddl 只出草稿 · 红色确认</div>
          </div>
        </div>

        <div className="panel">
          <div className="p-h">规则表<span className="p-s">app/safety · 纯逻辑 · 单测覆盖</span></div>
          <table className="rule-table">
            <thead><tr><th style={{ width: 210 }}>规则</th><th style={{ width: 90 }}>适用</th><th style={{ width: 80 }}>判定</th><th>说明</th></tr></thead>
            <tbody>
              <tr><td>读查询自动注入行数上限</td><td>SELECT</td><td><Badge v="allow" /></td><td>SQL 层注入 <span className="sql-k">LIMIT cap+1</span>，不只是截断传输</td></tr>
              <tr><td>写操作无 WHERE</td><td>UPDATE / DELETE</td><td><Badge v="block" /></td><td>未带条件直接拦截，绝不执行</td></tr>
              <tr><td>写操作受影响行预览</td><td>INSERT / UPDATE / DELETE</td><td><Badge v="review" /></td><td>同 WHERE 的 COUNT 预览，需显式 <span className="sql-k">confirm</span></td></tr>
              <tr><td>结构变更硬边界</td><td>CREATE / ALTER / DROP / TRUNCATE</td><td><span className="badge manual">MANUAL</span></td><td>AI 只生成 <span className="sql-k">draft_ddl</span> 草稿送编辑器，无 DDL 执行工具</td></tr>
              <tr><td>解析失败兜底</td><td>任意 SQL</td><td><Badge v="review" /></td><td>解析失败默认按"写"处理，绝不 ALLOW</td></tr>
            </tbody>
          </table>
        </div>

        <div className="panel">
          <div className="p-h">最近判定<span className="p-s">{recents.length} 条</span></div>
          <div className="rec-list">
            {recents.map((r, i) => (
              <div className="rec" key={i}>
                <Badge v={r.verdict} />
                <span className="sql">{r.sql}</span>
                <span className="src">{r.origin === 'ai' ? 'AI' : '手动'}</span>
                <span className="tme mono">{r.ts.slice(11, 19)} · {r.elapsed_ms}ms</span>
              </div>
            ))}
            {recents.length === 0 && <div className="mpage-empty">暂无审计记录</div>}
          </div>
        </div>
      </div>
    </div>
  )
}

/* ==================== 知识图谱 ==================== */
export function GraphPage(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { overview, loading, load } = useKnowledge()

  useEffect(() => {
    if (currentId) void load(currentId)
  }, [currentId, load])

  return (
    <div className="mpage">
      <div className="mpage-head">
        <h1>知识图谱 <span className="db-chip mono">{connName}</span></h1>
        <p>FK 关系网络，叠加已确认领域标签。点击节点查看表详情与关联；高亮 = 命中当前查询的表。</p>
      </div>
      <div className="mpage-body">
        <div className="panel graph-panel">
          {loading && !overview ? (
            <div className="mpage-empty mono">加载中…</div>
          ) : overview ? (
            <>
              <KnowledgeGraph overview={overview} />
              <div className="g-legend">
                <span><i style={{ background: 'var(--teal)' }} />已确认标签</span>
                <span><i style={{ border: '1.5px solid var(--teal)', background: 'transparent' }} />命中查询</span>
                <span><i style={{ background: 'var(--line-strong)' }} />FK 关系</span>
              </div>
            </>
          ) : (
            <div className="mpage-empty">先连接一个数据源，构建知识库后查看图谱。</div>
          )}
        </div>
      </div>
    </div>
  )
}

/* ==================== 审计 ==================== */
const FILTERS = [
  { key: '', label: '全部' },
  { key: 'allow', label: '放行' },
  { key: 'review', label: '确认' },
  { key: 'block', label: '拦截' },
  { key: 'executed', label: '已执行' }
]

export function AuditPage(): React.JSX.Element {
  const current = useConnections((s) => s.list.find((c) => c.id === s.currentId))
  const [entries, setEntries] = useState<AuditEntry[]>([])
  const [f, setF] = useState('')

  useEffect(() => {
    let alive = true
    void listAudit(current?.name).then((r) => alive && setEntries(r)).catch(() => undefined)
    return () => { alive = false }
  }, [current?.name])

  const rows = f ? entries.filter((e) => e.verdict === f) : entries

  return (
    <div className="mpage">
      <div className="mpage-head">
        <h1>审计 <span className="db-chip mono">{current?.name ?? '—'}</span></h1>
        <p>每条执行语句 JSONL 记录：语句、判定、时间戳。来源 AI / 手动 均可追溯。</p>
      </div>
      <div className="mpage-body">
        <div className="filter-pills">
          {FILTERS.map((x) => (
            <button key={x.key} className={`fp${f === x.key ? ' on' : ''}`} onClick={() => setF(x.key)}>
              {x.label}
            </button>
          ))}
        </div>
        <div className="panel">
          <table className="audit-table">
            <thead><tr><th>时间</th><th>判定</th><th className="sql">语句</th><th>时长</th><th>来源</th></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td className="tme mono">{r.ts}</td>
                  <td><Badge v={r.verdict} /></td>
                  <td className="sql mono">{r.sql}</td>
                  <td className="ms mono">{r.elapsed_ms}ms</td>
                  <td className="ms mono">{r.origin === 'ai' ? 'AI' : '手动'}</td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr><td colSpan={5} className="mpage-empty" style={{ textAlign: 'center', padding: 26 }}>该分类暂无记录</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
