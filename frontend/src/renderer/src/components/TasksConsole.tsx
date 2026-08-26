import { useCallback, useEffect, useMemo, useState } from 'react'
import { request } from '@renderer/api/client'
import { useConnections } from '@renderer/store/connections'
import { toastMsg } from '@renderer/utils/toast'

/* ═══════════════════════════════════════════════
   定时任务控制台（重设计）
   - 顶部：标题 + 「实时数据 / 预览 Mock」切换 + 新建任务
   - 统计条：总数 / 运行中 / 已停用 / 最近执行
   - 任务卡片：cron 徽标 + 友好频率 + 自然语言意图 + 连接 + 状态胶囊
     · 启停 / 立即运行 / 删除 / 展开看 SQL
   - 预览 Mock：本地假数据（可交互：启停/删除/立即跑），不触后端
   ═══════════════════════════════════════════════ */

interface Task {
  id: string
  name: string
  cron: string
  connection_id: string
  sql?: string | null
  natural_query?: string | null
  enabled: number
  created_at: string
  last_run_at?: string | null
}

const API = '/api/v1/tasks'

/** cron 简易友好化（只读常见 5 段格式，其余回原文） */
function friendlyCron(cron: string): string {
  const c = (cron || '').trim().split(/\s+/)
  if (c.length !== 5) return cron || ''
  const [min, hour, , , dow] = c
  if (min === '*/30') return '每 30 分钟'
  if (min === '*/5') return '每 5 分钟'
  const hh = hour.padStart(2, '0')
  const mm = min.padStart(2, '0')
  if (dow === '*') return `每天 ${hh}:${mm}`
  const d = ['日', '一', '二', '三', '四', '五', '六'][parseInt(dow, 10) % 7]
  return `每周${d} ${hh}:${mm}`
}

const nowStr = (): string => {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/* ── 预览 Mock 数据（可交互） ── */
const MOCK_TASKS: Task[] = [
  {
    id: 'mock-1', name: '每日销售日报', cron: '0 9 * * *', connection_id: '',
    natural_query: '统计昨日各品类销售额与订单量，按金额降序', sql: 'SELECT c.name, SUM(od.amount) AS gmv, COUNT(*) AS orders\nFROM order_detail od JOIN category c ON c.id = od.cat_id\nWHERE od.created_at >= date(\'now\', \'-1 day\') GROUP BY c.id ORDER BY gmv DESC',
    enabled: 1, created_at: '2026-08-18T09:00:00', last_run_at: '2026-08-24 09:00:31',
  },
  {
    id: 'mock-2', name: '库存预警扫描', cron: '*/30 * * * *', connection_id: '',
    natural_query: '检查低于安全库存的商品，写入预警表', sql: 'SELECT sku, name, stock, safety_stock\nFROM product WHERE stock < safety_stock',
    enabled: 1, created_at: '2026-08-19T14:20:00', last_run_at: '2026-08-24 08:30:02',
  },
  {
    id: 'mock-3', name: '用户活跃周报', cron: '0 10 * * 1', connection_id: '',
    natural_query: '统计本周 DAU / MAU 与新增用户数', sql: 'SELECT COUNT(DISTINCT user_id) AS dau\nFROM user_log WHERE date(ts) = date(\'now\')',
    enabled: 0, created_at: '2026-08-20T11:05:00', last_run_at: null,
  },
  {
    id: 'mock-4', name: '异常订单自检', cron: '0 */4 * * *', connection_id: '',
    natural_query: '定位金额异常或长时间未支付的订单', sql: 'SELECT id, amount, status FROM orders\nWHERE status = \'pending\' AND created_at < datetime(\'now\', \'-2 hour\') AND amount > 10000',
    enabled: 1, created_at: '2026-08-21T16:40:00', last_run_at: '2026-08-24 07:59:47',
  },
]

const CRON_PRESETS = [
  { label: '每天 09:00', cron: '0 9 * * *' },
  { label: '每 30 分钟', cron: '*/30 * * * *' },
  { label: '每周一 10:00', cron: '0 10 * * 1' },
]

export function TasksConsole(): React.JSX.Element {
  const connList = useConnections((s) => s.list)
  const connName = useCallback((id: string) => connList.find((c) => c.id === id)?.name ?? '—', [connList])

  const [tasks, setTasks] = useState<Task[]>([])
  const [mock, setMock] = useState(false)
  const [mockTasks, setMockTasks] = useState<Task[]>(MOCK_TASKS)
  const [showCreate, setShowCreate] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState({ name: '', cron: '', natural_query: '', connection_id: '' })

  const load = useCallback(async () => {
    try {
      const d = await request<{ tasks: Task[] }>(API)
      setTasks(d.tasks || [])
    } catch { setTasks([]) }
  }, [])

  useEffect(() => { void load() }, [load])

  const showing = mock ? mockTasks : tasks

  const stats = useMemo(() => {
    const on = showing.filter((t) => t.enabled === 1).length
    return {
      total: showing.length,
      running: on,
      stopped: showing.length - on,
      recent: showing.filter((t) => t.last_run_at).length,
    }
  }, [showing])

  async function create(): Promise<void> {
    if (!form.name.trim() || !form.cron.trim()) {
      toastMsg('任务名称与 cron 表达式必填')
      return
    }
    setBusy(true)
    try {
      await request<Task>(API, {
        method: 'POST',
        body: JSON.stringify({
          name: form.name.trim(), cron: form.cron.trim(),
          natural_query: form.natural_query.trim() || null,
          connection_id: form.connection_id || 'demo',
        }),
      })
      setShowCreate(false)
      setForm({ name: '', cron: '', natural_query: '', connection_id: '' })
      toastMsg('任务已创建')
      void load()
    } catch (e) {
      toastMsg(`创建失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  async function toggle(task: Task): Promise<void> {
    const next = task.enabled === 1 ? 0 : 1
    if (mock) {
      setMockTasks((ts) => ts.map((t) => (t.id === task.id ? { ...t, enabled: next } : t)))
      return
    }
    try {
      await request<unknown>(`${API}/${task.id}`, { method: 'PUT', body: JSON.stringify({ enabled: next === 1 }) })
      setTasks((ts) => ts.map((t) => (t.id === task.id ? { ...t, enabled: next } : t)))
    } catch (e) {
      toastMsg(`切换失败：${(e as Error).message}`)
    }
  }

  async function runNow(task: Task): Promise<void> {
    if (mock) {
      setMockTasks((ts) => ts.map((t) => (t.id === task.id ? { ...t, last_run_at: nowStr() } : t)))
      toastMsg(`「${task.name}」已触发执行`)
      return
    }
    setBusy(true)
    try {
      const r = await request<{ status?: string; message?: string; sql?: string }>(`${API}/${task.id}/run`, { method: 'POST' })
      toastMsg(`执行完成：${(r as { status?: string }).status ?? 'ok'}`)
      void load()
    } catch (e) {
      toastMsg(`执行失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  async function remove(task: Task): Promise<void> {
    if (!window.confirm(`删除任务「${task.name}」？`)) return
    if (mock) {
      setMockTasks((ts) => ts.filter((t) => t.id !== task.id))
      return
    }
    try {
      await request<unknown>(`${API}/${task.id}`, { method: 'DELETE' })
      void load()
    } catch (e) {
      toastMsg(`删除失败：${(e as Error).message}`)
    }
  }

  const toggleExpand = (id: string): void => {
    setExpanded((s) => {
      const n = new Set(s)
      if (n.has(id)) n.delete(id)
      else n.add(id)
      return n
    })
  }

  return (
    <div className="tk-page">
      {/* ═══ 顶部 ═══ */}
      <div className="tk-top">
        <div className="tk-top-l">
          <h1 className="tk-title">定时任务</h1>
          <p className="tk-sub">SELECT-only 无人值守执行 · 每次运行写入审计（blocked_unsafe / scheduled_exec）</p>
        </div>
        <div className="tk-top-r">
          <div className="seg">
            <button className={`seg-b${!mock ? ' on' : ''}`} onClick={() => setMock(false)}>
              <span className="tk-seg-dot live" /> 实时数据
            </button>
            <button className={`seg-b${mock ? ' on' : ''}`}
              onClick={() => { setMock(true); setMockTasks(MOCK_TASKS) }}>
              <span className="tk-seg-dot mock" /> 预览 Mock
            </button>
          </div>
          <button className="tk-new" onClick={() => setShowCreate(true)}>＋ 新建任务</button>
        </div>
      </div>

      {/* ═══ 统计条 ═══ */}
      <div className="tk-stats">
        <div className="tk-stat">
          <span className="tk-stat-n">{stats.total}</span><span className="tk-stat-l">任务总数</span>
        </div>
        <div className="tk-stat">
          <span className="tk-stat-n ok">{stats.running}</span><span className="tk-stat-l">运行中</span>
        </div>
        <div className="tk-stat">
          <span className="tk-stat-n off">{stats.stopped}</span><span className="tk-stat-l">已停用</span>
        </div>
        <div className="tk-stat">
          <span className="tk-stat-n dim">{stats.recent}</span><span className="tk-stat-l">最近有执行</span>
        </div>
        {mock && <span className="tk-mock-badge">预览模式 · 不与后端交互</span>}
      </div>

      {/* ═══ 任务列表 ═══ */}
      <div className="tk-list">
        {showing.length === 0 && (
          <div className="tk-empty">
            <div className="tk-empty-g">◷</div>
            <div>{mock ? '没有预览任务了，点「预览 Mock」重置？' : '暂无定时任务'}</div>
            <div className="tk-empty-sub">
              {mock
                ? '用过一次的 Mock 任务不会自动恢复 —— 切换两次「预览 Mock」可重置示例'
                : '新建一个任务，或切到「预览 Mock」看看版式效果'}
            </div>
          </div>
        )}

        {showing.map((task) => {
          const on = task.enabled === 1
          const open = expanded.has(task.id)
          return (
            <div key={task.id} className={`tk-card${open ? ' open' : ''}`}>
              <div className="tk-card-head" onClick={() => toggleExpand(task.id)}>
                <span className="tk-glyph">◷</span>
                <span className="tk-name">{task.name}</span>
                <span className="tk-cron mono">{task.cron}</span>
                <span className="tk-freq">{friendlyCron(task.cron)}</span>
                <span className="tk-conn mono" title="连接">{connName(task.connection_id)}</span>
                <span className={`tk-status ${on ? 'on' : 'off'}`}>
                  <span className="tk-status-dot" />{on ? '运行中' : '已停用'}
                </span>
                <span className="spacer" />
                <button
                  className={`mini-btn ${on ? 'dang' : ''}`}
                  onClick={(e) => { e.stopPropagation(); void toggle(task) }}
                  title={on ? '停用' : '启用'}
                >{on ? '停用' : '启用'}</button>
                <button className="mini-btn set" onClick={(e) => { e.stopPropagation(); void runNow(task) }} disabled={busy} title="立即运行">▶ 运行</button>
                <button className="mini-btn dang" onClick={(e) => { e.stopPropagation(); void remove(task) }} title="删除">删除</button>
                <span className="tk-chev">{open ? '▾' : '▸'}</span>
              </div>

              {(task.natural_query || open) && (
                <div className="tk-card-body">
                  {task.natural_query && (
                    <div className="tk-nq"><span className="tk-nq-ic">🎯</span>{task.natural_query}</div>
                  )}
                  <div className="tk-meta mono">
                    <span>上次执行 <b className={task.last_run_at ? '' : 'never'}>{task.last_run_at ?? '从未运行'}</b></span>
                    <span className="tk-meta-sep">·</span>
                    <span>创建于 {task.created_at?.replace('T', ' ').slice(0, 16)}</span>
                  </div>
                  {open && task.sql && (
                    <pre className="tk-sql mono">{task.sql}</pre>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* ═══ 新建任务弹窗 ═══ */}
      {showCreate && (
        <div className="tk-mask" onClick={() => setShowCreate(false)}>
          <div className="tk-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="tk-dlg-head">
              新建定时任务
              <button className="kbh-close" onClick={() => setShowCreate(false)}>✕</button>
            </div>
            <div className="tk-dlg-body">
              <label className="tk-field">
                <span>任务名称</span>
                <input autoFocus value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="如：每日销售日报" maxLength={40} />
              </label>
              <label className="tk-field">
                <span>cron 表达式</span>
                <input value={form.cron} onChange={(e) => setForm({ ...form, cron: e.target.value })}
                  placeholder="如：0 9 * * *" className="mono" />
                <div className="tk-presets">
                  {CRON_PRESETS.map((p) => (
                    <button key={p.cron} type="button" className="tk-preset mono" onClick={() => setForm({ ...form, cron: p.cron })}>
                      {p.label}
                    </button>
                  ))}
                </div>
              </label>
              <label className="tk-field">
                <span>目标连接</span>
                <select value={form.connection_id} onChange={(e) => setForm({ ...form, connection_id: e.target.value })}>
                  <option value="">无连接（默认 demo）</option>
                  {connList.map((c) => (
                    <option key={c.id} value={c.id}>{c.name}</option>
                  ))}
                </select>
              </label>
              <label className="tk-field">
                <span>自然语言意图（选填，便于控制台阅读）</span>
                <textarea rows={2} value={form.natural_query} onChange={(e) => setForm({ ...form, natural_query: e.target.value })}
                  placeholder="如：统计昨日各品类销售……（自动转 SELECT，不做延迟生成）" />
              </label>
            </div>
            <div className="tk-dlg-foot">
              <button className="btn ghost" onClick={() => setShowCreate(false)}>取消</button>
              <button className="btn save" disabled={busy} onClick={() => void create()}>
                {busy ? '创建中…' : '创建任务'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}