import { useEffect, useMemo, useState } from 'react'
import { testDraftConnection } from '@renderer/api/connections'
import { useConnections } from '@renderer/store/connections'
import { useI18n } from '@renderer/store/i18n'

interface Props {
  open: boolean
  onClose: () => void
  /** 传入连接 id 时为编辑模式（加载既有配置，保存走 update）；否则新建 */
  editId?: string | null
}

const DIALECTS = ['sqlite', 'postgres', 'mysql']
const DRAFT_KEY = 'tabletalk-conn-drafts-v1'
/** 编辑模式密码占位：表示"已保存"，真值永不出网；留空=沿用已存密码测试/保存 */
const PWD_PLACEHOLDER = '••••••••'

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

export function ConnectionModal({ open, onClose, editId }: Props): React.JSX.Element | null {
  const create = useConnections((s) => s.create)
  const update = useConnections((s) => s.update)
  const list = useConnections((s) => s.list)
  const { t } = useI18n()
  const [form, setForm] = useState<Draft>(loadDraft)
  const [testedAt, setTestedAt] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // 编辑模式：加载既有连接配置（密码为掩码时留空，保存不覆盖）
  useEffect(() => {
    if (!open || !editId) return
    const c = list.find((x) => x.id === editId)
    if (!c) return
    setForm({
      name: c.name ?? '',
      dialect: c.dialect ?? 'postgres',
      host: c.host ?? '',
      port: c.port != null ? String(c.port) : '',
      user: c.user ?? '',
      password: c.password ? PWD_PLACEHOLDER : '',  // 有已存密码 → *** 占位，真值不出网
      database: c.database ?? '',
      file: c.file ?? '',
      ssl: c.ssl ?? false,
      readOnly: c.read_only ?? false,
      sensitive: (c.sensitive ?? []).join(', '),
    })
    setTestedAt(null)
    setTestMsg(null)
  }, [open, editId, list])

  const isSqlite = form.dialect === 'sqlite'

  const input = useMemo(() => ({
    name: form.name || '未命名连接',
    dialect: form.dialect,
    host: isSqlite ? '' : form.host,
    port: isSqlite ? null : Number(form.port) || undefined,
    user: isSqlite ? '' : form.user,
    password: isSqlite ? '' : (editId && form.password === PWD_PLACEHOLDER ? '' : form.password),  // 占位 → 空：沿用已存
    database: isSqlite ? '' : form.database,
    file: isSqlite ? form.file : '',
    ssl: form.ssl,
    read_only: form.readOnly,
    sensitive: form.sensitive.split(',').map((s) => s.trim()).filter(Boolean),
  }), [form, isSqlite])

  const set = <K extends keyof Draft>(k: K, v: Draft[K]): void => {
    setForm((f) => ({ ...f, [k]: v }))
    setTestedAt(null) // 任何字段改动 → 测试结果失效，需重新测试
  }

  // 草稿：新建模式实时进浏览器缓存；编辑模式不回写草稿（避免覆盖用户上次的新建草稿）
  useEffect(() => {
    if (!open || editId) return
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify(form))
    } catch {
      /* ignore */
    }
  }, [form, open, editId])

  if (!open) return null

  async function handleTest(): Promise<void> {
    setTesting(true)
    setTestMsg(null)
    try {
      const r = await testDraftConnection(input, { savedConnId: editId })
      setTestedAt(r.ok ? JSON.stringify(input) : null)
      setTestMsg({
        ok: r.ok,
        text: r.ok
          ? t('conn.modal.testOk', { ms: r.latency_ms }) + (isSqlite ? t('conn.modal.testOkSqlite') : '')
          : t('conn.modal.testFail', { error: r.error ?? t('common.unknownError') })
      })
    } catch (e) {
      setTestedAt(null)
      setTestMsg({ ok: false, text: t('conn.modal.testFail', { error: (e as Error).message }) })
    } finally {
      setTesting(false)
    }
  }

  // 保存闸门：测试通过 且 表单自测试后未改动
  const canSave = testedAt !== null && testedAt === JSON.stringify(input)

  async function handleSave(): Promise<void> {
    if (!canSave) return
    if (editId) {
      // 编辑模式：密码留空表示不修改（掩码不可回传覆盖）
      const patch: import('@renderer/api/connections').ConnectionInput = { ...input }
      if (!patch.password) delete patch.password
      await update(editId, patch)
      onClose()
      return
    }
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
            <span className="t">{editId ? t('conn.modal.titleEdit') : t('conn.modal.title')}</span>
            <button className="close" onClick={onClose}>✕</button>
          </div>
          <div className="mb">
            <div className="fld">
              <label>{t('conn.modal.name')}</label>
              <input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="my-connection" />
            </div>
            <div className="fld">
              <label>{t('conn.modal.database')}</label>
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
              <label>{t('conn.modal.sqlitePath')}</label>
              <input value={form.file} onChange={(e) => set('file', e.target.value)} placeholder="~/.tabletalk/demo.db" />
              <div className="note mono" style={{ fontFamily: 'IBM Plex Mono', fontSize: 12, color: 'var(--ink-dim)' }}>
                {t('conn.modal.sqliteNote')}
              </div>
            </div>
          ) : (
            <>
              <div className="fld">
                <label>{t('conn.modal.address')}</label>
                <div className="row">
                  <input value={form.host} onChange={(e) => set('host', e.target.value)} placeholder="host" />
                  <input value={form.port} onChange={(e) => set('port', e.target.value)} placeholder="port" style={{ width: 90 }} />
                </div>
              </div>
              <div className="fld">
                <label>{t('conn.modal.credentials')}</label>
                <div className="row">
                  <input value={form.user} onChange={(e) => set('user', e.target.value)} placeholder="user" />
                  <input
                    value={form.password}
                    onChange={(e) => set('password', e.target.value)}
                    onFocus={(e) => { if (form.password === PWD_PLACEHOLDER) set('password', '') }}
                    type="password"
                    placeholder={form.password === PWD_PLACEHOLDER ? t('conn.modal.pwdSaved') : 'password'}
                  />
                </div>
              </div>
              <div className="fld">
                <label>{t('conn.modal.databaseName')}</label>
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
                <input type="checkbox" checked={form.readOnly} onChange={(e) => set('readOnly', e.target.checked)} /> {t('conn.modal.readOnly')}
              </label>
            </div>
          </div>
            <div className="fld">
              <label>{t('conn.modal.sensitive')}</label>
            <input value={form.sensitive} onChange={(e) => set('sensitive', e.target.value)} placeholder="payroll_*, *secret*" />
          </div>
          {testMsg && (
            <div className="note mono" style={{ fontFamily: 'IBM Plex Mono', fontSize: 12, color: testMsg.ok ? 'var(--accent)' : 'var(--danger, #e57)' }}>
              {testMsg.text}
            </div>
          )}
          {!canSave && testedAt !== null && (
            <div className="note" style={{ fontSize: 12, color: 'var(--ink-dim)' }}>
              {t('conn.modal.formChanged')}
            </div>
          )}
        </div>
        <div className="mf">
          <button className="btn ghost" onClick={onClose}>{t('common.cancel')}</button>
          <button className="btn tl" disabled={testing} onClick={handleTest}>
            {testing ? t('conn.modal.testing') : t('conn.modal.test')}
          </button>
          <button className="btn save" disabled={!canSave} onClick={handleSave} title={canSave ? '' : t('conn.modal.saveHint')}>
            {t('conn.modal.save')}
          </button>
        </div>
      </div>
    </div>
  )
}
