import { useState } from 'react'
import { testConnection } from '@renderer/api/connections'
import { useConnections } from '@renderer/store/connections'

interface Props {
  open: boolean
  onClose: () => void
}

const DIALECTS = ['sqlite', 'postgres', 'mysql']

export function ConnectionModal({ open, onClose }: Props): React.JSX.Element | null {
  const create = useConnections((s) => s.create)
  const [name, setName] = useState('')
  const [dialect, setDialect] = useState('postgres')
  const [host, setHost] = useState('127.0.0.1')
  const [port, setPort] = useState('5432')
  const [user, setUser] = useState('')
  const [password, setPassword] = useState('')
  const [database, setDatabase] = useState('')
  const [file, setFile] = useState('')
  const [ssl, setSsl] = useState(false)
  const [readOnly, setReadOnly] = useState(true)
  const [testing, setTesting] = useState(false)
  const [testMsg, setTestMsg] = useState('')

  if (!open) return null

  const isSqlite = dialect === 'sqlite'

  async function handleTest(): Promise<void> {
    setTesting(true)
    setTestMsg('')
    try {
      const cfg = await create({
        name: name || 'test-conn',
        dialect,
        host: isSqlite ? '' : host,
        port: isSqlite ? null : Number(port) || undefined,
        user: isSqlite ? '' : user,
        password: isSqlite ? '' : password,
        database: isSqlite ? '' : database,
        file: isSqlite ? file : '',
        ssl,
        read_only: readOnly
      })
      if (cfg) {
        const r = await testConnection(cfg.id)
        setTestMsg(r.ok ? `连接测试通过 · ${r.latency_ms}ms` : `测试失败：${r.error ?? '未知错误'}`)
      }
    } finally {
      setTesting(false)
    }
  }

  async function handleSave(): Promise<void> {
    await create({
      name: name || '未命名连接',
      dialect,
      host: isSqlite ? '' : host,
      port: isSqlite ? null : Number(port) || undefined,
      user: isSqlite ? '' : user,
      password: isSqlite ? '' : password,
      database: isSqlite ? '' : database,
      file: isSqlite ? file : '',
      ssl,
      read_only: readOnly
    })
    onClose()
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
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="my-connection" />
          </div>
          <div className="fld">
            <label>数据库</label>
            <select
              className="fld-input"
              value={dialect}
              onChange={(e) => setDialect(e.target.value)}
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
              <input value={file} onChange={(e) => setFile(e.target.value)} placeholder="~/.cleared/demo.db" />
            </div>
          ) : (
            <>
              <div className="fld">
                <label>地址</label>
                <div className="row">
                  <input value={host} onChange={(e) => setHost(e.target.value)} placeholder="host" />
                  <input value={port} onChange={(e) => setPort(e.target.value)} placeholder="port" style={{ width: 90 }} />
                </div>
              </div>
              <div className="fld">
                <label>凭据</label>
                <div className="row">
                  <input value={user} onChange={(e) => setUser(e.target.value)} placeholder="user" />
                  <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" placeholder="password" />
                </div>
              </div>
              <div className="fld">
                <label>数据库名</label>
                <input value={database} onChange={(e) => setDatabase(e.target.value)} placeholder="database" />
              </div>
            </>
          )}
          <div className="fld">
            <div style={{ display: 'flex', gap: 20 }}>
              <label className="toggle-ssl">
                <input type="checkbox" checked={ssl} onChange={(e) => setSsl(e.target.checked)} /> SSL / TLS
              </label>
              <label className="toggle-ssl">
                <input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} /> 只读连接
              </label>
            </div>
          </div>
          {testMsg && (
            <div className="note mono" style={{ fontFamily: 'IBM Plex Mono', fontSize: 10.5, color: 'var(--ink-dim)' }}>
              {testMsg}
            </div>
          )}
        </div>
        <div className="mf">
          <button className="btn ghost" onClick={onClose}>取消</button>
          <button className="btn tl" disabled={testing} onClick={handleTest}>
            {testing ? '测试中…' : '测试连接'}
          </button>
          <button className="btn save" onClick={handleSave}>保存连接</button>
        </div>
      </div>
    </div>
  )
}
