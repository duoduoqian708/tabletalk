import { useEffect, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { testConnection } from '@renderer/api/connections'
import { toastMsg } from '@renderer/utils/toast'
import { testGateway, testEmbedding } from '@renderer/api/ai'
import { getSettings, updateSettings } from '@renderer/api/settings'
import type { AiModelConfig, EmbeddingModelConfig, SettingsPublic, SettingsPatch } from '@renderer/api/settings'
import { SkillPlaza } from './SkillPlaza'
import { useI18n } from '@renderer/store/i18n'

interface Props {
  open: boolean
  onClose: () => void
  onNewConnection: () => void
  onEditConnection: (id: string) => void
}

const SECTIONS = [
  { key: 'dsm', ic: '⛁', labelKey: 'settings.section.dsm' },
  { key: 'theme', ic: '◐', labelKey: 'settings.section.theme' },
  { key: 'llm', ic: '◎', labelKey: 'settings.section.llm' },
  { key: 'skills', ic: '✦', labelKey: 'settings.section.skills' },
  { key: 'safety', ic: '▣', labelKey: 'settings.section.safety' },
  { key: 'privacy', ic: '◈', labelKey: 'settings.section.privacy' },
  { key: 'general', ic: '⚙', labelKey: 'settings.section.general' }
] as const

type Sec = (typeof SECTIONS)[number]['key']
type ModelTab = 'chat' | 'embedding'

const TEST_STATE_KEY = 'tabletalk-conn-test-'

interface TestState { ok: boolean; latency_ms?: number; error?: string; ts: number }

function loadTestState(id: string): TestState | null {
  try {
    const raw = localStorage.getItem(TEST_STATE_KEY + id)
    return raw ? (JSON.parse(raw) as TestState) : null
  } catch { return null }
}
function saveTestState(id: string, s: TestState): void {
  try { localStorage.setItem(TEST_STATE_KEY + id, JSON.stringify(s)) } catch { /* ignore */ }
}

/** 连接卡：目标信息（路径 / host·库）为主内容，敏感名单仅配置时展示；点卡即切为当前；设为默认即时点亮。 */
function ConnRow({ conn, onEdit, onRemove, onSetDefault, onSelect, isCurrent, isDefault }: {
  conn: { id: string; name: string; dialect: string; host: string; port: number | null; database: string; user: string; file: string; read_only?: boolean; sensitive?: string[] }
  onEdit: () => void
  onRemove: () => void
  onSetDefault: () => void
  onSelect: () => void
  isCurrent: boolean
  isDefault: boolean
}): React.JSX.Element {
  const { t } = useI18n()
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<TestState | null>(() => loadTestState(conn.id))

  async function handleTest(): Promise<void> {
    setTesting(true)
    try {
      const r = await testConnection(conn.id)
      const st: TestState = { ok: r.ok, latency_ms: r.latency_ms, error: r.error ?? undefined, ts: Date.now() }
      saveTestState(conn.id, st)
      setTest(st)
      toastMsg(r.ok
        ? `✓ ${t('conn.modal.testOk', { ms: r.latency_ms })}`
        : `✕ ${r.error ?? t('common.unknownError')}`)
    } catch (e) {
      const st: TestState = { ok: false, error: (e as Error).message, ts: Date.now() }
      saveTestState(conn.id, st)
      setTest(st)
      toastMsg((e as Error).message || t('common.unknownError'))
    } finally {
      setTesting(false)
    }
  }

  // 连接目标：SQLite 显文件路径；PG/MySQL 地址一行、库名一行（user 放 title）
  const isSqlite = conn.dialect === 'sqlite'
  const addr = isSqlite
    ? conn.file || '—'
    : (conn.host && conn.port ? `${conn.host}:${conn.port}` : (conn.host || '—'))
  const dbName = isSqlite ? '' : conn.database || '—'
  const targetTitle = isSqlite ? addr : `${addr} · db:${dbName} · user:${conn.user || '—'}`

  const sensList = (conn.sensitive ?? []).join(', ')
  const testCls = test ? (test.ok ? ' ok' : ' fail') : ''
  const testLabel = testing ? '…' : test ? (test.ok ? `✓ ${t('settings.conn.test')}` : `✕ ${t('settings.conn.test')}`) : t('settings.conn.test')
  return (
    <div
      className={`conn-row${isCurrent ? ' cur' : ''}`}
      onClick={onSelect}
      title={t('settings.conn.selectTitle')}
    >
      <div className="conn-main">
        <div className="conn-line1">
          <span className="conn-name">{conn.name}</span>
          <span className="conn-dialect mono">{conn.dialect}</span>
          {conn.read_only && <span className="ro-tag mono">{t('conn.readOnly')}</span>}
          {isDefault && <span className="def-tag mono">★ {t('settings.conn.isDefault')}</span>}
        </div>
        <div className="conn-target mono" title={targetTitle}>
          <span className="ct-ic">{isSqlite ? '▤' : '◈'}</span>
          <span className="ct-body">
            <span className="ct-line">{addr}</span>
            {dbName && <span className="ct-line sub">db:{dbName}</span>}
          </span>
        </div>
        {sensList && (
          <div className="conn-sens mono">
            <span className="sens-label">{t('settings.conn.sensitiveList')}：</span>
            <span className="sens-list" title={sensList}>{sensList}</span>
          </div>
        )}
      </div>
      <div className="conn-side">
        <div className="conn-actions" onClick={(e) => e.stopPropagation()}>
          <button
            className={`mini-btn test${testCls}`}
            disabled={testing}
            onClick={() => void handleTest()}
            title={test ? (test.ok ? `✓ ${test.latency_ms ?? ''}ms` : test.error) : t('settings.conn.test')}
          >
            {testLabel}
          </button>
          <button
            className={`mini-btn${isDefault ? ' set' : ''}`}
            onClick={onSetDefault}
            title={isDefault ? t('settings.conn.isDefaultTitle') : t('settings.conn.setDefaultTitle')}
          >
            {isDefault ? t('settings.conn.isDefault') : t('settings.conn.setDefault')}
          </button>
          <button className="mini-btn" onClick={onEdit}>{t('settings.conn.edit')}</button>
          <button className="mini-btn dang" onClick={onRemove}>{t('common.delete')}</button>
        </div>
      </div>
    </div>
  )
}

function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`
}

// 后端对已保存的 api_key 返回脱敏掩码（sk-abc•••xyz）——含 ••• 即表示"有存量 key，前端拿不到真值"
function isMaskedKey(k: unknown): boolean {
  return typeof k === 'string' && k.includes('•••')
}

const CAP_LABELS: Record<string, { i18nKey: string; cls: string }> = {
  connectivity: { i18nKey: 'settings.cap.connectivity', cls: 'ok' },
  function_calling: { i18nKey: 'settings.cap.fc', cls: 'fc' },
  reasoning: { i18nKey: 'settings.cap.reasoning', cls: 'reasoning' },
  streaming: { i18nKey: 'settings.cap.streaming', cls: 'ok' },
}

function CapBadges({ caps }: { caps?: { connectivity?: boolean; function_calling?: boolean; reasoning?: boolean | null; streaming?: boolean; context_window?: number | null; latency_ms?: number } }): React.JSX.Element {
  const { t } = useI18n()
  if (!caps) return <span className="cap-badge no">{t('settings.model.untested')}</span>
  const items: { key: string; ok: boolean | null | undefined; label: string; cls: string }[] = []
  for (const [k, v] of Object.entries(CAP_LABELS)) {
    const val = caps[k as keyof typeof caps]
    items.push({ key: k, ok: val as boolean | null | undefined, label: t(v.i18nKey), cls: v.cls })
  }
  return (
    <div className="mi-badges">
      {items.map((it) => (
        <span key={it.key} className={`cap-badge ${it.ok ? it.cls : 'no'}`} title={it.key}>
          {it.ok ? it.label : (it.ok == null && it.key === 'reasoning' ? t('settings.cap.untested') : t('settings.cap.none', { label: it.label }))}
        </span>
      ))}
      {caps.context_window && (
        <span className="cap-badge" title={t('settings.cap.contextWindow')}>{Math.round(caps.context_window / 1000)}K</span>
      )}
      {caps.latency_ms !== undefined && (
        <span className="cap-badge">{caps.latency_ms}ms</span>
      )}
    </div>
  )
}

export function SettingsDrawer({ open, onClose, onNewConnection, onEditConnection }: Props): React.JSX.Element | null {
  const [sec, setSec] = useState<Sec>('dsm')
  const { list, currentId, defaultId, remove, setDefault, select } = useConnections()
  const [closing, setClosing] = useState(false)
  const closeTimer = useRef<number | null>(null)

  // 关闭动画：先播放 setSlideOut，动画结束才通知父组件卸载
  function handleClose(): void {
    if (closing) return
    setClosing(true)
    closeTimer.current = window.setTimeout(() => {
      setClosing(false)
      onClose()
    }, 280)
  }
  useEffect(() => () => { if (closeTimer.current) window.clearTimeout(closeTimer.current) }, [])
  const [savedMsg, setSavedMsg] = useState('')
  const { locale, setLocale, t } = useI18n()

  // 主题：亮色（默认）/ 深色，localStorage 记忆
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    try { return (localStorage.getItem('tabletalk-theme') as 'light' | 'dark') || 'light' } catch { return 'light' }
  })
  const applyTheme = (t: 'light' | 'dark'): void => {
    setTheme(t)
    try { localStorage.setItem('tabletalk-theme', t) } catch { /* ignore */ }
    document.documentElement.setAttribute('data-theme', t)
  }

  // 平台级运行参数（安全闸门 / 通用）
  const [settings, setSettings] = useState<SettingsPublic | null>(null)
  const [maxRows, setMaxRows] = useState<number>(1000)
  const [poolSize, setPoolSize] = useState<number>(3)
  const [privacyMode, setPrivacyMode] = useState<string>('standard')
  const [runtime, setRuntime] = useState<{ data_dir: string; port: number; auth: string } | null>(null)
  const [loading, setLoading] = useState(false)
  const [alwaysShowSql, setAlwaysShowSql] = useState<boolean>(() => {
    try { return localStorage.getItem('tabletalk-sql-fold') === '0' } catch { return false }
  })
  const toggleAlwaysShowSql = (v: boolean): void => {
    setAlwaysShowSql(v)
    try { localStorage.setItem('tabletalk-sql-fold', v ? '0' : '1') } catch {}
  }
  const privacyDesc: Record<string, string> = {
    strict: '严格：仅结构（敏感表代号化），可完全离线，mock 全链路',
    standard: '标准：结构 + 脱敏聚合（默认，企业日常）',
    open: '开放：逐查询授权明文，个人/低敏场景',
  }
  async function persistPrivacy(mode: string): Promise<void> {
    try {
      await updateSettings({ privacy_mode: mode } as unknown as SettingsPatch)
      setSettings((s) => (s ? { ...(s as SettingsPublic), privacy_mode: mode } as SettingsPublic : s))
    } catch {}
  }

  // ---- 大模型：列表 + tab ----
  const [mTab, setMTab] = useState<ModelTab>('chat')
  const [aiModels, setAiModels] = useState<AiModelConfig[]>([])
  const [defaultAi, setDefaultAi] = useState('')
  const [embModels, setEmbModels] = useState<EmbeddingModelConfig[]>([])
  const [defaultEmb, setDefaultEmb] = useState('')

  // 编辑态：null = 关闭，'new' = 新增，id = 编辑某条
  const [editingChat, setEditingChat] = useState<string | null>(null)
  const [editingEmb, setEditingEmb] = useState<string | null>(null)
  const [editing, setEditing] = useState<Partial<AiModelConfig> | Partial<EmbeddingModelConfig>>({})
  const [testing, setTesting] = useState(false)
  const [testMsg, setTestMsg] = useState('')

  // 打开时加载配置
  useEffect(() => {
    if (!open) return
    let alive = true
    setLoading(true)
    void getSettings()
      .then((s) => {
        if (!alive) return
        setSettings(s)
        setAiModels(s.ai_models)
        setDefaultAi(s.default_ai_model)
        setEmbModels(s.embedding_models)
        setDefaultEmb(s.default_embedding_model)
        setMaxRows(s.query_max_rows ?? 1000)
        setPoolSize(s.pool_size ?? 3)
        setPrivacyMode(s.privacy_mode ?? 'standard')
        setRuntime(s.runtime ?? null)
      })
      .catch(() => undefined)
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [open])

  function startNewChat(): void {
    setEditingChat('new')
    setEditing({
      id: uid('llm'),
      name: '新模型',
      provider: 'cloud',
      base_url: '',
      api_key: '',
      model: '',
      temperature: 0.2,
      timeout: 120,
      reasoning: null,
    })
    setTestMsg('')
  }

  function startEditChat(m: AiModelConfig): void {
    setEditingChat(m.id)
    setEditing({ ...m })
    setTestMsg('')
  }

  function startNewEmb(): void {
    setEditingEmb('new')
    setEditing({
      id: uid('emb'),
      name: '新嵌入模型',
      provider: 'api',
      base_url: '',
      api_key: '',
      model: 'bge-m3',
    })
    setTestMsg('')
  }

  function startEditEmb(m: EmbeddingModelConfig): void {
    setEditingEmb(m.id)
    setEditing({ ...m })
    setTestMsg('')
  }

  function cancelEdit(): void {
    setEditingChat(null)
    setEditingEmb(null)
    setEditing({})
    setTestMsg('')
  }

  async function handleTest(): Promise<void> {
    setTesting(true)
    setTestMsg('')
    // 掩码 = 用户未改 key → 传空，让后端用已存的真实 key（避免把掩码当 key 打向 API）
    const effectiveKey = isMaskedKey(editing.api_key) ? '' : editing.api_key
    try {
      if (mTab === 'chat') {
        const r = await testGateway({
          provider: editing.provider as string,
          baseUrl: editing.base_url,
          apiKey: effectiveKey,
          model: editing.model,
        })
        if (r.ok) {
          const caps = r.capabilities
          setEditing((prev) => ({
            ...prev,
            last_test: { ok: true, latency_ms: r.latency_ms, capabilities: caps },
            // 测试探测到推理能力则自动标定（未知 None 时保留手动设定）
            reasoning: caps?.reasoning != null ? caps.reasoning : ((prev as Partial<AiModelConfig>).reasoning ?? null),
          }))
          const capTexts = []
          if (caps?.connectivity) capTexts.push(t('settings.cap.connectivity'))
          if (caps?.function_calling) capTexts.push(t('settings.cap.fc'))
          if (caps?.reasoning) capTexts.push(t('settings.cap.reasoning'))
          if (caps?.streaming) capTexts.push(t('settings.cap.streamingFull'))
          if (caps?.context_window) capTexts.push(`${Math.round(caps.context_window / 1000)}K`)
          setTestMsg(`${t('settings.testOk', { ms: r.latency_ms ?? '?' })}${capTexts.length ? ` · ${capTexts.join(' / ')}` : ''}`)
        } else {
          setTestMsg(t('settings.testFail', { error: r.error ?? t('common.unknownError') }))
        }
      } else {
        const r = await testEmbedding({
          provider: editing.provider as string,
          baseUrl: editing.base_url,
          apiKey: effectiveKey,
          model: editing.model,
        })
        if (r.ok) {
          setEditing((prev) => ({
            ...prev,
            last_test: { ok: true, dimensions: r.dimensions, latency_ms: r.latency_ms },
            dimensions: r.dimensions,
          }))
          setTestMsg(t('settings.embTestOk', { dims: r.dimensions ?? 0, ms: r.latency_ms ?? '?' }))
        } else {
          setTestMsg(t('settings.testFail', { error: r.error ?? t('common.unknownError') }))
        }
      }
    } catch (e) {
      setTestMsg(t('settings.testFail', { error: (e as Error).message }))
    } finally {
      setTesting(false)
    }
  }

  function saveEdit(): void {
    if (mTab === 'chat') {
      const id = editing.id!
      const exists = aiModels.some((m) => m.id === id)
      const next: AiModelConfig[] = exists
        ? aiModels.map((m) => (m.id === id ? { ...m, ...editing } as AiModelConfig : m))
        : [...aiModels, editing as AiModelConfig]
      const nextDefault = !defaultAi || (editingChat === 'new' && next.length === 1) ? id : defaultAi
      setAiModels(next)
      setDefaultAi(nextDefault)
      setEditingChat(null)
      persist(next, nextDefault)
    } else {
      const id = editing.id!
      const exists = embModels.some((m) => m.id === id)
      const next: EmbeddingModelConfig[] = exists
        ? embModels.map((m) => (m.id === id ? { ...m, ...editing } as EmbeddingModelConfig : m))
        : [...embModels, editing as EmbeddingModelConfig]
      const nextDefault = !defaultEmb || (editingEmb === 'new' && next.length === 1) ? id : defaultEmb
      setEmbModels(next)
      setDefaultEmb(nextDefault)
      setEditingEmb(null)
      persist(undefined, undefined, next, nextDefault)
    }
    setEditing({})
    setTestMsg('')
  }

  // 改动即存：模型列表 / 默认 / 增删改 每一次操作都立即落盘
  async function persist(
    models: AiModelConfig[] = aiModels,
    def = defaultAi,
    embs: EmbeddingModelConfig[] = embModels,
    defEmb = defaultEmb,
  ): Promise<boolean> {
    try {
      const patch: SettingsPatch = {
        ai_models: models.map((m) => ({ ...m })),
        default_ai_model: def,
        embedding_models: embs.map((m) => ({ ...m })),
        default_embedding_model: defEmb,
      }
      if (settings && settings.query_max_rows !== maxRows) patch.query_max_rows = maxRows
      if (settings && settings.pool_size !== poolSize) patch.pool_size = poolSize
      await updateSettings(patch)
      setSettings((s) => (s ? { ...(s as SettingsPublic), ...(patch as SettingsPublic) } : s))
      setSavedMsg(t('settings.saveSuccess'))
      return true
    } catch (e) {
      setSavedMsg(t('settings.saveFail', { msg: (e as Error).message }))
      return false
    }
  }

  function removeChatModel(id: string): void {
    const next = aiModels.filter((m) => m.id !== id)
    const nextDefault = defaultAi === id ? (next[0]?.id ?? '') : defaultAi
    setAiModels(next)
    if (defaultAi === id) setDefaultAi(nextDefault)
    persist(next, nextDefault)
  }

  function removeEmbModel(id: string): void {
    const next = embModels.filter((m) => m.id !== id)
    const nextDefault = defaultEmb === id ? (next[0]?.id ?? '') : defaultEmb
    setEmbModels(next)
    if (defaultEmb === id) setDefaultEmb(nextDefault)
    persist(undefined, undefined, next, nextDefault)
  }

  if (!open) return null

  return (
    <div className="set-mask" onClick={(e) => e.target === e.currentTarget && handleClose()}>
      <aside className={`set-drawer${closing ? ' closing' : ''}`}>
        <div className="set-head">
          <span className="set-title">{t('settings.titleFull')}</span>
          <span className="set-sub mono">{t('settings.sub')}</span>
          <button className="set-x" onClick={handleClose}>✕</button>
        </div>

        <div className="set-body">
          <nav className="set-rail">
            {SECTIONS.map((s) => (
              <button key={s.key} className={`sr-it${sec === s.key ? ' on' : ''}`} onClick={() => setSec(s.key)}>
                <span className="sr-ic">{s.ic}</span>{t(s.labelKey)}
              </button>
            ))}
          </nav>

          <div className="set-content">
            {sec === 'dsm' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.dsm')}<span className="sec-s mono">{t('settings.dsm.sub')}</span></div>
                <div className="sec-d">{t('settings.dsm.desc')}</div>
                <div className="conn-list">
                  {list.map((c) => (
                    <ConnRow
                      key={c.id}
                      conn={c}
                      onEdit={() => onEditConnection(c.id)}
                      onRemove={() => void remove(c.id)}
                      onSetDefault={() => setDefault(c.id)}
                      onSelect={() => select(c.id)}
                      isCurrent={c.id === currentId}
                      isDefault={c.id === defaultId}
                    />
                  ))}
                  {list.length === 0 && <div className="mpage-empty">{t('settings.dsm.empty')}</div>}
                  <button className="conn-add" onClick={onNewConnection}>＋ {t('settings.conn.addConnection')}</button>
                </div>
              </section>
            )}

            {sec === 'theme' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.theme')}<span className="sec-s mono">{t('settings.theme.sub')}</span></div>
                <div className="sec-d">{t('settings.theme.desc')}</div>
                <div className="theme-row">
                  {(['light', 'dark'] as const).map((th) => (
                    <button
                      key={th}
                      className={`theme-opt${theme === th ? ' on' : ''}`}
                      onClick={() => applyTheme(th)}
                    >
                      <span className="theme-opt-swatch" style={{ background: th === 'light' ? '#f4f5f7' : '#08090d' }} />
                      <span className="theme-opt-name">{th === 'light' ? t('settings.theme.light') : t('settings.theme.dark')}</span>
                      {theme === th && <span className="theme-opt-check mono">✓</span>}
                    </button>
                  ))}
                </div>
                <div className="set-row">
                  <label>{t('settings.language')}</label>
                  <div className="seg">
                    <button className={`seg-b${locale === 'zh-CN' ? ' on' : ''}`} onClick={() => setLocale('zh-CN')}>中文</button>
                    <button className={`seg-b${locale === 'en-US' ? ' on' : ''}`} onClick={() => setLocale('en-US')}>English</button>
                  </div>
                  <span className="set-hint">{t('settings.languageHint')}</span>
                </div>
              </section>
            )}

            {sec === 'llm' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.llm')}<span className="sec-s mono">{t('settings.llm.sub')}</span></div>
                <div className="sec-d">{t('settings.llm.desc')}</div>

                <div className="model-tabs">
                  <button className={`model-tab${mTab === 'chat' ? ' on' : ''}`} onClick={() => { setMTab('chat'); cancelEdit() }}>
                    {t('settings.llm.chatTab')}
                  </button>
                  <button className={`model-tab${mTab === 'embedding' ? ' on' : ''}`} onClick={() => { setMTab('embedding'); cancelEdit() }}>
                    {t('settings.llm.embTab')}
                  </button>
                </div>

                {mTab === 'chat' && (
                  <>
                    <div className="model-list">
                      {aiModels.map((m) => (
                        <div key={m.id} className={`model-item${m.id === defaultAi ? ' cur' : ''}`}>
                            <div className="mi-head">
                              <span className="mi-name">{m.name}</span>
                              <span className="mi-model">{m.model || '—'}</span>
                              <div className="mi-actions">
                                <button className={`mini-btn set${m.id === defaultAi ? ' cur' : ''}`} onClick={() => { setDefaultAi(m.id); persist(aiModels, m.id) }}>
                                {m.id === defaultAi ? t('settings.model.current') : t('settings.model.setDefault')}
                              </button>
                              <button className="mini-btn" onClick={() => startEditChat(m)}>{t('settings.model.edit')}</button>
                              {!m.builtin && aiModels.length > 1 && (
                                <button className="mini-btn dang" onClick={() => removeChatModel(m.id)}>{t('common.delete')}</button>
                              )}
                            </div>
                          </div>
                          <div className="mi-cap">
                            <CapBadges caps={m.last_test?.capabilities} />
                          </div>
                        </div>
                      ))}
                      {aiModels.length === 0 && <div className="model-empty">{t('settings.llm.emptyChat')}</div>}
                    </div>

                    {editingChat && (
                      <div className="model-edit">
                        <div className="me-row">
                          <label>Provider</label>
                          <input value={editing.name ?? ''} onChange={(e) => setEditing({ ...editing, name: e.target.value })} placeholder={t('settings.model.namePlaceholder')} />
                        </div>
                        {editing.provider !== 'mock' && (
                          <>
                            <div className="me-row">
                              <label>Base URL</label>
                              <input value={editing.base_url ?? ''} onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
                            </div>
                            <div className="me-row">
                              <label>{t('settings.model.modelId')}</label>
                              <input value={editing.model ?? ''} onChange={(e) => setEditing({ ...editing, model: e.target.value })} placeholder="gpt-4o / claude-sonnet-4.5" />
                            </div>
                            <div className="me-row">
                              <label>API Key</label>
                              <input type="password" value={isMaskedKey(editing.api_key) ? '' : (editing.api_key ?? '')} onChange={(e) => setEditing({ ...editing, api_key: e.target.value })} placeholder={isMaskedKey(editing.api_key) ? t('settings.model.keySaved', { key: editing.api_key ?? '' }) : t('settings.model.keyNew')} />
                            </div>
                          </>
                        )}
                        <div className="me-row">
                          <label>{t('settings.model.temperature')}</label>
                          <input type="range" className="temp-slider" min="0" max="2" step="0.05" value={(editing as Partial<AiModelConfig>).temperature ?? 0.2} onChange={(e) => setEditing({ ...(editing as Partial<AiModelConfig>), temperature: parseFloat(e.target.value) })} />
                          <span className="temp-val mono">{((editing as Partial<AiModelConfig>).temperature ?? 0.2).toFixed(2)}</span>
                          <span className="hint" style={{ flex: 1 }}>{t('settings.model.tempHint')}</span>
                        </div>
                        <div className="me-row">
                          <label>{t('settings.model.reasoningLabel')}</label>
                          <span className={`re-status${(editing as Partial<AiModelConfig>).reasoning == null ? '' : ((editing as Partial<AiModelConfig>).reasoning ? ' on' : ' off')}`}>
                            {(editing as Partial<AiModelConfig>).reasoning == null
                              ? t('settings.model.untested')
                              : ((editing as Partial<AiModelConfig>).reasoning ? t('settings.model.reasoningOn') : t('settings.model.reasoningOff'))}
                          </span>
                        </div>
                        <div className="me-actions">
                          <button className="btn tl" disabled={testing || (editing.provider !== 'mock' && !editing.base_url?.trim())} onClick={() => void handleTest()}>
                            {testing ? t('conn.modal.testing') : t('conn.modal.test')}
                          </button>
                          <span className="spacer" />
                          <button className="mini-btn" onClick={cancelEdit}>{t('common.cancel')}</button>
                          <button className="btn save" onClick={saveEdit}>{editingChat === 'new' ? t('settings.model.add') : t('common.save')}</button>
                        </div>
                        {testMsg && <div className={`me-test${testMsg.startsWith('✓') ? ' ok' : ' bad'}`}>{testMsg}</div>}
                      </div>
                    )}

                    {!editingChat && (
                      <button className="model-add" onClick={startNewChat}>＋ {t('settings.llm.addChat')}</button>
                    )}
                  </>
                )}

                {mTab === 'embedding' && (
                  <>
                    <div className="model-list">
                      {embModels.map((m) => (
                        <div key={m.id} className={`model-item${m.id === defaultEmb ? ' cur' : ''}`}>
                          <div className="mi-head">
                            <span className="mi-name">{m.name}</span>
                            <span className="mi-provider">{m.provider}</span>
                            <span className="mi-model">{m.model || '—'}</span>
                            <div className="mi-actions">
                              <button className={`mini-btn set${m.id === defaultEmb ? ' cur' : ''}`} onClick={() => { setDefaultEmb(m.id); persist(undefined, undefined, embModels, m.id) }}>
                                {m.id === defaultEmb ? t('settings.model.current') : t('settings.model.setDefault')}
                              </button>
                              <button className="mini-btn" onClick={() => startEditEmb(m)}>{t('settings.model.edit')}</button>
                              {embModels.length > 1 && (
                                <button className="mini-btn dang" onClick={() => removeEmbModel(m.id)}>{t('common.delete')}</button>
                              )}
                            </div>
                          </div>
                          <div className="mi-cap">
                            <div className="mi-badges">
                              {m.last_test?.ok ? (
                                <span className="cap-badge ok">{t('settings.cap.dims', { n: m.last_test.dimensions ?? '?' })}</span>
                              ) : (
                                <span className="cap-badge no">{t('settings.model.untested')}</span>
                              )}
                              {m.last_test?.latency_ms !== undefined && (
                                <span className="cap-badge">{m.last_test.latency_ms}ms</span>
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                      {embModels.length === 0 && <div className="model-empty">{t('settings.llm.emptyEmb')}</div>}
                    </div>

                    {editingEmb && (
                      <div className="model-edit">
                        <div className="me-row">
                          <label>{t('settings.model.name')}</label>
                          <input value={editing.name ?? ''} onChange={(e) => setEditing({ ...editing, name: e.target.value })} placeholder={t('settings.model.namePlaceholderEmb')} />
                        </div>
                        <div className="me-row">
                          <label>Provider</label>
                          <select value={editing.provider ?? 'api'} onChange={(e) => setEditing({ ...editing, provider: e.target.value })}>
                            <option value="api">{t('settings.model.apiProvider')}</option>
                          </select>
                        </div>
                        {editing.provider !== 'hash' && (
                          <>
                            <div className="me-row">
                              <label>Base URL</label>
                              <input value={editing.base_url ?? ''} onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
                            </div>
                            <div className="me-row">
                              <label>{t('settings.model.modelId')}</label>
                              <input value={editing.model ?? ''} onChange={(e) => setEditing({ ...editing, model: e.target.value })} placeholder="text-embedding-3-small / bge-m3" />
                            </div>
                            <div className="me-row">
                              <label>API Key</label>
                              <input type="password" value={isMaskedKey(editing.api_key) ? '' : (editing.api_key ?? '')} onChange={(e) => setEditing({ ...editing, api_key: e.target.value })}                               placeholder={isMaskedKey(editing.api_key) ? t('settings.model.keySaved', { key: editing.api_key ?? '' }) : t('settings.model.keyNew')} />
                            </div>
                          </>
                        )}
                        <div className="me-actions">
                          <button className="btn tl" disabled={testing || (editing.provider !== 'hash' && !editing.base_url?.trim())} onClick={() => void handleTest()}>
                            {testing ? t('conn.modal.testing') : t('conn.modal.test')}
                          </button>
                          <span className="spacer" />
                          <button className="mini-btn" onClick={cancelEdit}>{t('common.cancel')}</button>
                          <button className="btn save" onClick={saveEdit}>{editingEmb === 'new' ? t('settings.model.add') : t('common.save')}</button>
                        </div>
                        {testMsg && <div className={`me-test${testMsg.startsWith('✓') ? ' ok' : ' bad'}`}>{testMsg}</div>}
                      </div>
                    )}

                    {!editingEmb && (
                      <button className="model-add" onClick={startNewEmb}>＋ {t('settings.llm.addEmb')}</button>
                    )}
                  </>
                )}
              </section>
            )}

            {sec === 'safety' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.safety')}<span className="sec-s mono">{t('settings.safety.sub')}</span></div>
                <div className="sec-d">{t('settings.safety.desc')}</div>
                <div className="panel">
                  <div className="p-h">{t('settings.safety.gateParams')}<span className="p-s mono">{t('settings.safety.runtimeOverride')}</span></div>
                  <div className="p-b">
                    <div className="set-row inline"><span className="sr-l">{t('settings.safety.maxRows')}</span><input type="number" min={1} value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value) || 1)} onBlur={() => { void persist(); }} /></div>
                    <div className="set-row inline"><span className="sr-l">{t('settings.safety.poolSize')}</span><input type="number" min={1} value={poolSize} onChange={(e) => setPoolSize(Number(e.target.value) || 1)} onBlur={() => { void persist(); }} /><span className="hint" style={{ marginLeft: 6 }}>{t('settings.safety.poolHint')}</span></div>
                  </div>
                </div>
              </section>
            )}

            {sec === 'privacy' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.privacy')}<span className="sec-s mono">{t('settings.privacy.sub')}</span></div>
                <div className="sec-d">{t('settings.privacy.desc')}</div>
                <div className="panel">
                  <div className="p-h">三档隐私<span className="p-s mono">strict / standard / open</span></div>
                  <div className="p-b">
                    <div className="set-row">
                      <label className="sr-l">隐私模式</label>
                      <select value={privacyMode} onChange={(e) => { setPrivacyMode(e.target.value); void persistPrivacy(e.target.value) }}>
                        <option value="strict">严格（纯结构，可离线）</option>
                        <option value="standard">标准（脱敏聚合）</option>
                        <option value="open">开放（明文，需逐查询授权）</option>
                      </select>
                    </div>
                    <div className="hint" style={{ marginTop: 8 }}>{privacyDesc[privacyMode]}</div>
                    {privacyMode === 'strict' && (
                      <div className="hint" style={{ marginTop: 8, color: 'var(--amber)' }}>严格档：一键离线（自动切 mock，禁用云端），销售演示 30 秒离线可用</div>
                    )}
                  </div>
                </div>
              </section>
            )}

            {sec === 'skills' && (
              <section className="set-sec">
                <div className="sec-d mono">{t('skill.floorNote')}</div>
                <SkillPlaza />
              </section>
            )}

            {sec === 'general' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.general')}<span className="sec-s mono">{t('settings.general.sub')}</span></div>
                <div className="sec-d">{t('settings.general.desc')}</div>
                <div className="panel">
                  <div className="p-h">{t('settings.general.service')}<span className="p-s mono">{t('settings.general.serviceSub')}</span></div>
                  <div className="p-b">
                    {loading || !runtime ? (
                      <div className="sd-hint">{t('common.loading')}</div>
                    ) : (
                      <>
                        <div className="set-row readonly inline"><span className="sr-l">{t('settings.general.dataDir')}</span><input type="text" value={runtime.data_dir} readOnly /></div>
                        <div className="set-row readonly inline"><span className="sr-l">{t('settings.general.port')}</span><input type="text" value={String(runtime.port)} readOnly /></div>
                        <div className="set-row readonly inline"><span className="sr-l">{t('settings.general.auth')}</span><input type="text" value={runtime.auth} readOnly /></div>
                        <div className="set-row readonly inline"><span className="sr-l">{t('settings.general.maxConn')}</span><input type="text" value={String(poolSize)} readOnly /></div>
                      </>
                    )}
                  </div>
                </div>
                <div className="panel" style={{ marginTop: 12 }}>
                  <div className="p-h">SQL 显示<span className="p-s mono">开发者视角</span></div>
                  <div className="p-b">
                    <div className="set-row inline">
                      <span className="sr-l">始终显示 SQL</span>
                      <label className="tg" style={{ display: 'inline-flex', alignItems: 'center', cursor: 'pointer' }}>
                        <input type="checkbox" checked={alwaysShowSql} onChange={(e) => toggleAlwaysShowSql(e.target.checked)} style={{ marginRight: 6 }} />
                        <span>{alwaysShowSql ? '已开启（默认展开）' : '默认折叠为“显示查询”'}</span>
                      </label>
                    </div>
                  </div>
                </div>
              </section>
            )}

            <div className="save-bar">
              {savedMsg && <span className={`test-result${savedMsg.startsWith('✓') ? ' ok' : ' bad'}`}>{savedMsg}</span>}
            </div>
          </div>
        </div>
      </aside>
    </div>
  )
}
