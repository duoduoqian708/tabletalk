import { useCallback, useEffect, useState } from 'react'

interface Task {
  id: string; name: string; cron: string; connection_id: string
  sql?: string; natural_query?: string; enabled: number; created_at: string; last_run_at?: string
}

const API = '/api/v1/tasks'

export function TasksConsole(): React.JSX.Element {
  const [tasks, setTasks] = useState<Task[]>([])
  const [showCreate, setShowCreate] = useState(false)
  const [form, setForm] = useState({ name: '', cron: '', natural_query: '', connection_id: '' })

  const load = useCallback(async () => {
    try {
      const r = await fetch(API, { headers: { 'X-TableTalk-Token': localStorage.getItem('tt_token') || '' } })
      const d = await r.json()
      setTasks(d.tasks || [])
    } catch { /* */ }
  }, [])

  useEffect(() => { void load() }, [load])

  async function create() {
    if (!form.name || !form.cron) return
    await fetch(API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': localStorage.getItem('tt_token') || '' },
      body: JSON.stringify({ ...form, connection_id: form.connection_id || 'demo' }),
    })
    setShowCreate(false)
    setForm({ name: '', cron: '', natural_query: '', connection_id: '' })
    void load()
  }

  async function remove(id: string) {
    if (!window.confirm('Delete this task?')) return
    await fetch(`${API}/${id}`, {
      method: 'DELETE',
      headers: { 'X-TableTalk-Token': localStorage.getItem('tt_token') || '' },
    })
    void load()
  }

  return (
    <div style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>定时任务</h2>
        <button onClick={() => setShowCreate(!showCreate)} style={{ padding: '6px 16px', borderRadius: 6, background: 'var(--accent)', color: '#fff', border: 'none', cursor: 'pointer' }}>
          {showCreate ? '取消' : '+ 新建任务'}
        </button>
      </div>

      {showCreate && (
        <div style={{ background: 'var(--surface)', borderRadius: 8, padding: 16, marginBottom: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <input placeholder="任务名称" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} style={{ padding: '8px 12px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--fg)' }} />
          <input placeholder="cron 表达式（如 0 9 * * *）" value={form.cron} onChange={e => setForm({ ...form, cron: e.target.value })} style={{ padding: '8px 12px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--fg)' }} />
          <input placeholder="自然语言查询（如：统计今日订单）" value={form.natural_query} onChange={e => setForm({ ...form, natural_query: e.target.value })} style={{ padding: '8px 12px', borderRadius: 6, border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--fg)' }} />
          <button onClick={create} style={{ padding: '8px 16px', borderRadius: 6, background: 'var(--accent)', color: '#fff', border: 'none', cursor: 'pointer', alignSelf: 'flex-start' }}>创建</button>
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {tasks.length === 0 && <div style={{ color: 'var(--muted)', fontSize: 14 }}>暂无定时任务</div>}
        {tasks.map(task => (
          <div key={task.id} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 16px', background: 'var(--surface)', borderRadius: 8 }}>
            <div>
              <div style={{ fontWeight: 500 }}>{task.name}</div>
              <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 2 }}>
                <code style={{ background: 'var(--bg)', padding: '2px 6px', borderRadius: 4 }}>{task.cron}</code>
                {task.natural_query && <span style={{ marginLeft: 8 }}>{task.natural_query}</span>}
              </div>
              {task.last_run_at && <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>上次执行: {task.last_run_at}</div>}
            </div>
            <button onClick={() => remove(task.id)} style={{ padding: '4px 12px', borderRadius: 4, background: 'transparent', color: 'var(--danger, #e44)', border: '1px solid var(--danger, #e44)', cursor: 'pointer', fontSize: 12 }}>删除</button>
          </div>
        ))}
      </div>
    </div>
  )
}
