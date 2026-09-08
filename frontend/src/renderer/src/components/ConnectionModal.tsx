import { useEffect, useMemo, useRef, useState } from 'react'
import { testDraftConnection } from '@renderer/api/connections'
import { getSchema } from '@renderer/api/schema'
import { Dropdown } from './Dropdown'
import type { SensitiveEntry } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useI18n } from '@renderer/store/i18n'
import { saveTestState, type ConnTestState } from '@renderer/utils/connTestState'
import { CloseBtn } from './ui/buttons'

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
/** 敏感名单（表/列过滤）UI 暂时屏蔽：交互逻辑未想清楚前不开放编辑入口，后端逻辑保留，开启时置 true 即可 */
const SENSITIVE_UI_ENABLED = false

interface Draft {
  name: string
  dialect: string
  host: string
  port: string
  user: string
  password: string
  database: string
  file: string
  readOnly: boolean
  sensitive: SensitiveEntry[]
}

const EMPTY: Draft = {
  name: '', dialect: 'postgres', host: '127.0.0.1', port: '5432',
  user: '', password: '', database: '', file: '',
  readOnly: true, sensitive: [],
}

function loadDraft(): Draft {
  try {
    const raw = localStorage.getItem(DRAFT_KEY)
    if (raw) {
      const src = JSON.parse(raw) as Record<string, unknown>
      const loaded: Draft = { ...EMPTY }
      for (const k of Object.keys(loaded) as (keyof Draft)[]) {
        if (src[k] !== undefined) (loaded as unknown as Record<string, unknown>)[k] = src[k]
      }
      // 兼容旧草稿：sensitive 存的是逗号分隔字符串 → 转字符串条目（精确名结构由新编辑器产生）
      if (typeof src.sensitive === 'string') {
        loaded.sensitive = src.sensitive.split(',').map((s) => s.trim()).filter(Boolean)
      }
      return loaded
    }
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
  // 测试状态唯一键：每次测试生成，表单任何改动即失效；保存闸门 = 有有效测试键且快照未变
  const [testKey, setTestKey] = useState<string | null>(null)
  const lastTest = useRef<ConnTestState | null>(null)
  const [testing, setTesting] = useState(false)
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string } | null>(null)
  // 敏感名单（段8）：编辑模式拉取 schema 供下拉选表/勾列；不可用回退手动输入
  const [schemaTables, setSchemaTables] = useState<{ name: string; columns: string[] }[] | null>(null)
  const [addTable, setAddTable] = useState('')
  const [addWhole, setAddWhole] = useState(true)
  const [addCols, setAddCols] = useState<string[]>([])
  const [addColText, setAddColText] = useState('')

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
      readOnly: c.read_only ?? false,
      sensitive: (c.sensitive ?? []).map((e) =>
        typeof e === 'string' ? e : { table: e.table, columns: e.columns ?? [] }),
    })
    setTestedAt(null)
    setTestKey(null)
    setTestMsg(null)
    setAddCols([])
    setAddColText('')
  }, [open, editId, list])

  // 敏感名单选表：编辑模式下从已存连接拉 schema（新连接尚未落库 → 手动输入兜底）
  useEffect(() => {
    if (!open || !editId) {
      setSchemaTables(null)
      return
    }
    let alive = true
    setSchemaTables(null)
    getSchema(editId).then((s: { tables?: { name: string }[]; columns?: { table: string; name: string }[] }) => {
      if (!alive) return
      const tabs = (s?.tables ?? []).map((t) => ({
        name: t.name,
        columns: (s?.columns ?? []).filter((c) => c.table === t.name).map((c) => c.name),
      }))
      setSchemaTables(tabs)
      if (tabs.length) setAddTable(tabs[0].name)
    }).catch(() => { /* 连接不可用 → 手动输入表名/列 */ })
    return () => { alive = false }
  }, [open, editId])

  const isSqlite = form.dialect === 'sqlite'

  const input = useMemo(() => ({
    name: form.name || t('conn.modal.unnamed'),
    dialect: form.dialect,
    host: isSqlite ? '' : form.host,
    port: isSqlite ? null : Number(form.port) || undefined,
    user: isSqlite ? '' : form.user,
    password: isSqlite ? '' : (editId && form.password === PWD_PLACEHOLDER ? '' : form.password),  // 占位 → 空：沿用已存
    database: isSqlite ? '' : form.database,
    file: isSqlite ? form.file : '',
    read_only: form.readOnly,
    sensitive: form.sensitive.map((e) =>
      typeof e === 'string' ? e : { table: e.table, columns: e.columns ?? [] }),
  }), [form, isSqlite])

  const set = <K extends keyof Draft>(k: K, v: Draft[K]): void => {
    setForm((f) => ({ ...f, [k]: v }))
    setTestedAt(null) // 任何字段改动 → 测试结果失效，需重新测试
    setTestKey(null)
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
      const st: ConnTestState = { ok: r.ok, latency_ms: r.latency_ms, error: r.error ?? undefined, ts: Date.now() }
      lastTest.current = st
      if (r.ok) {
        setTestedAt(JSON.stringify(input))
        setTestKey(`t${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`)  // 唯一键定位本次测试状态
        if (editId) saveTestState(editId, st)  // 持久化：卡片「测试通过」标签据此点亮
      } else {
        setTestedAt(null)
        setTestKey(null)
        if (editId) saveTestState(editId, st)
      }
      setTestMsg({
        ok: r.ok,
        text: r.ok
          ? t('conn.modal.testOk', { ms: r.latency_ms }) + (isSqlite ? t('conn.modal.testOkSqlite') : '')
          : t('conn.modal.testFail', { error: r.error ?? t('common.unknownError') })
      })
    } catch (e) {
      setTestedAt(null)
      setTestKey(null)
      setTestMsg({ ok: false, text: t('conn.modal.testFail', { error: (e as Error).message }) })
    } finally {
      setTesting(false)
    }
  }

  // 保存闸门：测试通过（唯一键有效）且 表单自测试后未改动
  const canSave = testKey !== null && testedAt !== null && testedAt === JSON.stringify(input)

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
      if (lastTest.current) saveTestState(cfg.id, lastTest.current)  // 新建落 id 后再持久化测试状态
      try {
        localStorage.removeItem(DRAFT_KEY)
      } catch {
        /* ignore */
      }
      onClose()
    }
  }

  // 敏感名单（段8）：结构化条目 添加/移除
  function entryLabel(e: SensitiveEntry): string {
    if (typeof e === 'string') return e  // 旧 glob 名单
    const cols = e.columns ?? []
    return cols.length ? `${e.table} : ${cols.join(', ')}` : `${e.table} · ${t('conn.wholeTable')}`
  }

  function addSensitiveEntry(): void {
    const tbl = addTable.trim()
    if (!tbl) return
    if (schemaTables) {
      const entry: SensitiveEntry = addWhole
        ? { table: tbl }
        : { table: tbl, columns: addCols }
      set('sensitive', [...form.sensitive, entry])
    } else {
      // 无 schema 回退：列用逗号文本
      const cols = addWhole ? [] : addColText.split(',').map((s) => s.trim()).filter(Boolean)
      set('sensitive', [...form.sensitive, cols.length ? { table: tbl, columns: cols } : { table: tbl }])
    }
    setAddCols([])
    setAddColText('')
    setAddWhole(true)
  }

  function removeSensitiveAt(i: number): void {
    set('sensitive', form.sensitive.filter((_, j) => j !== i))
  }

  return (
    <div className="modal-mask open" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
          <div className="mh">
            <span className="t">{editId ? t('conn.modal.titleEdit') : t('conn.modal.title')}</span>
            <CloseBtn className="close" title={t('common.close')} onClick={onClose} />
          </div>
          <div className="mb">
            <div className="fld">
              <label>{t('conn.modal.name')}</label>
              <input value={form.name} onChange={(e) => set('name', e.target.value)} placeholder="my-connection" />
            </div>
            <div className="fld">
              <label>{t('conn.modal.database')}</label>
              <Dropdown
                style={{ width: '100%' }}
                value={form.dialect}
                options={DIALECTS.map((d) => ({ value: d, label: d }))}
                onChange={(v) => set('dialect', v)}
              />
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
                <input type="checkbox" checked={form.readOnly} onChange={(e) => set('readOnly', e.target.checked)} /> {t('conn.modal.readOnly')}
              </label>
            </div>
          </div>
          {SENSITIVE_UI_ENABLED && (
            <div className="fld">
              <label>{t('conn.modal.sensitive')}</label>
              <div className="sen-box">
                {form.sensitive.length === 0 && (
                  <div className="sen-empty mono">{t('conn.modal.sensitiveEmpty')}</div>
                )}
                {form.sensitive.map((e, i) => (
                  <span key={i} className={`sen-chip${typeof e === 'string' ? ' glob' : ''}`}>
                    {typeof e === 'string' && <em className="sen-glob-tag">glob</em>}
                    {entryLabel(e)}
                    <CloseBtn className="sen-x" onClick={() => removeSensitiveAt(i)} />
                  </span>
                ))}
              </div>
              <div className="sen-add">
                <div className="sen-add-row">
                  {schemaTables ? (
                    <Dropdown
                      style={{ flex: 1, minWidth: 0 }}
                      value={addTable}
                      placeholder={`— ${t('conn.modal.sensitivePickTable')} —`}
                      options={[
                        { value: '', label: `— ${t('conn.modal.sensitivePickTable')} —` },
                        ...schemaTables.map((t) => ({ value: t.name, label: t.name })),
                      ]}
                      onChange={(v) => { setAddTable(v); setAddCols([]) }}
                    />
                  ) : (
                    <input value={addTable} onChange={(e) => setAddTable(e.target.value)} placeholder={t('conn.modal.sensitiveTable')} style={{ flex: 1, minWidth: 0 }} />
                  )}
                  <label className="sen-whole">
                    <input type="checkbox" checked={addWhole} onChange={(e) => setAddWhole(e.target.checked)} />
                    {t('conn.modal.sensitiveWhole')}
                  </label>
                  <button type="button" className="btn ghost" disabled={!addTable.trim()} onClick={addSensitiveEntry}>
                    {t('conn.modal.sensitiveAdd')}
                  </button>
                </div>
                {!addWhole && schemaTables && addTable && (
                  <div className="sen-cols">
                    {schemaTables.find((t) => t.name === addTable)?.columns.map((c) => (
                      <label key={c} className="sen-col">
                        <input
                          type="checkbox"
                          checked={addCols.includes(c)}
                          onChange={(e) => setAddCols((prev) => e.target.checked ? [...prev, c] : prev.filter((x) => x !== c))}
                        /> {c}
                      </label>
                    ))}
                  </div>
                )}
                {!addWhole && !schemaTables && (
                  <input value={addColText} onChange={(e) => setAddColText(e.target.value)} placeholder={t('conn.modal.sensitiveCols')} />
                )}
              </div>
          </div>
          )}
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
