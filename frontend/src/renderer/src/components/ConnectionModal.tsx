import { useEffect, useMemo, useState } from 'react'
import { testDraftConnection } from '@renderer/api/connections'
import { useConnections } from '@renderer/store/connections'

interface Props {
  open: boolean
  onClose: () => void
}

const DIALECTS = ['sqlite', 'postgres', 'mysql']
const DRAFT_KEY = 'cleared-conn-drafts-v1'

interface Draft {
  name: string
  dialect: string
  host: string
  port: string
  user: string
  password: string
  database: string
  file: string
  ssl: boolean
  readOnly: boolean
  sensitive: string
}

const EMPTY: Draft = {
  name: '', dialect: 'postgres', host: '127.0.0.1', port: '5432',
  user: '', password: '', database: '', file: '',
  ssl: false, readOnly: true, sensitive: '',
}

function loadDraft(): Draft {
  try {
    const raw = localStorage.getItem(DRAFT_KEY)
    if (raw) return { ...EMPTY, ...JSON.parse(raw) }
  } catch {
    /* ignore */
  }
  return EMPTY
}

export function ConnectionModal({ open, onClose }: Props): React.JSX.Element | null {
  const create = useConnections((s) => s.create)
  const [form, setForm] = useState<Draft>(loadDraft)
  const [testedAt, setTestedAt] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // 草稿：表单内容实时进浏览器缓存（未测/失败也保留，防止切换页面丢失）
  useEffect(() => {
    if (!open) return
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(form))
    } catch {
      /* ignore */
    }
  }, [form, open])

  if (!open) return null

  const isSqlite = form.dialect === 'sqlite'

  const set = <K extends keyof Draft>(k: K, v: Draft[K]): void => {
    setForm((f) => ({ ...f, [k]: v }))
    setTestedAt(null) // 任何字段改动 → 测试结果失效，需重新测试
  }

  const input = useMemo(() => ({
    name: form.name || '未命名连接',
    dialect: form.dialect,
    host: isSqlite ? '' : form.host,
    port: isSqlite ? null : Number(form.port) || undefined,
    user: isSqlite ? '' : form.user,
    password: isSqlite ? '' : form.password,
    database: isSqlite ? '' : form.database,
    file: isSqlite ? form.file : '',
    ssl: form.ssl,
    read_only: form.readOnly,
    sensitive: form.sensitive.split(',').map((s) => s.trim()).filter(Boolean),
  }), [form, isSqlite])

  async function handleTest(): Promise<void> {
    setTesting(true)
    setTestMsg(null)
    try {
      const r = await testDraftConnection(input)
      setTestedAt(r.ok ? JSON.stringify(input) : null)
      setTestMsg({
        ok: r.ok,
        text: r.ok
          ? `✓ 连接测试通过 · ${r.latency_ms}ms${isSqlite ? ' · 文件可读且可列出表' : ''}`
          : `✕ 测试失败：${r.error ?? '未知错误'}`
      })
    } catch (e) {
      setTestedAt(null)
      setTestMsg({ ok: false, text: `✕ 测试失败：${(e as Error).message}` })
    } finally {
      setTesting(false)
    }
  }

  // 保存闸门：测试通过 且 表单自测试后未改动
  const canSave = testedAt !== null && testedAt === JSON.stringify(input)

  async function handleSave(): Promise<void> {
    if (!canSave) return
    const cfg = await create(input)
    if (cfg) {
      try {
        localStorage.removeItem(DRAFT_KEY)
      } catch {
        /* ignore */
      }
      onClose()
    }
  }

  return (
    <div className="modal-mask open" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <div className="mh">
          <span className="t">新建连接</span>
          <button className="close" onClick={onClose}>✕</button>
        </div>
        <div className="mb">
          <div className="fld">
            <label>连接名称</label>
            <input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="my-connection" />
          </div>
          <div className="fld">
            <label>数据库</label>
            <select
              className="fld-input"
              value={form.dialect}
              onChange={(e) => set('dialect', e.target.value)}
              style={{ background: 'var(--bg-2)', border: '1px solid var(--line)', borderRadius: 8, padding: '9px 12px', color: 'var(--ink)', fontFamily: 'IBM Plex Mono', fontSize: 12 }}
            >
              {DIALECTS.map((d) => (
                <option key={d} value={d}>{d}</option>
              ))}
            </select>
          </div>
          {isSqlite ? (
            <div className="fld">
              <label>SQLite 文件路径</label>
              <input value={form.file} onChange={(e) => set('file', e.target.value)} placeholder="~/.cleared/demo.db" />
              <div className="note mono" style={{ fontFamily: 'IBM Plex Mono', fontSize: 10.5, color: 'var(--ink-dim)' }}>
                SQLite 无“库”概念：测试 = 文件可读且能列出表
              </div>
            </div>
          ) : (
            <>
              <div className="fld">
                <label>地址</label>
                <div className="row">
                  <input value={form.host} onChange={(e) => set('host', e.target.value)} placeholder="host" />
                  <input value={form.port} onChange={(e) => set('port', e.target.value)} placeholder="port" style={{ width: 90 }} />
                </div>
              </div>
              <div className="fld">
                <label>凭据</label>
                <div className="row">
                  <input value={form.user} onChange={(e) => set('user', e.target.value)} placeholder="user" />
                  <input value={form.password} onChange={(e) => set('password', e.target.value)} type="password" placeholder="password" />
                </div>
              </div>
              <div className="fld">
                <label>数据库名</label>
                <input value={form.database} onChange={(e) => set('database', e.target.value)} placeholder="database" />
              </div>
            </>
          )}
          <div className="fld">
            <div style={{ display: 'flex', gap: 20 }}>
              <label className="toggle-ssl">
                <input type="checkbox" checked={form.ssl} onChange={(e) => set('ssl', e.target.checked)} /> SSL / TLS
              </label>
              <label className="toggle-ssl">
                <input type="checkbox" checked={form.readOnly} onChange={(e) => set('readOnly', e.target.checked)} /> 只读连接
              </label>
            </div>
          </div>
          <div className="fld">
            <label>敏感名单（表/列 glob，逗号分隔；屏蔽项不进 AI 上下文与知识库）</label>
            <input value={form.sensitive} onChange={(e) => set('sensitive', e.target.value)} placeholder="payroll_*, *secret*" />
          </div>
          {testMsg && (
            <div className="note mono" style={{ fontFamily: 'IBM Plex Mono', fontSize: 10.5, color: testMsg.ok ? 'var(--accent)' : 'var(--danger, #e57)' }}>
              {testMsg.text}
            </div>
          )}
          {!canSave && testedAt !== null && (
            <div className="note" style={{ fontSize: 10.5, color: 'var(--ink-dim)' }}>
              表单已改动，需重新测试
            </div>
          )}
        </div>
        <div className="mf">
          <button className="btn ghost" onClick={onClose}>取消</button>
          <button className="btn tl" disabled={testing} onClick={handleTest}>
            {testing ? '测试中…' : '测试连接'}
          </button>
          <button className="btn save" disabled={!canSave} onClick={handleSave} title={canSave ? '' : '请先测试连接，通过后保存'}>
            保存连接
          </button>
        </div>
      </div>
    </div>
  )
}
