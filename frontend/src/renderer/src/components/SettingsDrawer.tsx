import { Fragment, useEffect, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { testConnection } from '@renderer/api/connections'
import { toastMsg } from '@renderer/utils/toast'
import { loadTestState, saveTestState, type ConnTestState } from '@renderer/utils/connTestState'
import { testGateway, testEmbedding, getBuiltinProviders, getBuiltinEmbeddingProviders, fetchUpstreamModels, type BuiltinProvider } from '@renderer/api/ai'
import { getSettings, updateSettings } from '@renderer/api/settings'
import type { SensitiveEntry } from '@renderer/api/types'
import type { AiModelConfig, EmbeddingModelConfig, SettingsPublic, SettingsPatch } from '@renderer/api/settings'
import { getSchema } from '@renderer/api/schema'
import { useI18n } from '@renderer/store/i18n'
import { getGraphFontLevel, setGraphFontLevel, GRAPH_FONT_LEVELS } from '@renderer/lib/graphFont'
import { Dropdown, type DropdownOption } from './Dropdown'
import { CloseBtn } from './ui/buttons'
import { IconCheck, IconPlus } from './ui/icons'

interface Props {
  open: boolean
  onClose: () => void
  onNewConnection: () => void
  onEditConnection: (id: string) => void
  /** 打开时落到指定分区（如 'llm' 引导去配置大模型）；不传保持上次位置 */
  initialSec?: Sec
  /** 默认向量模型切换后用户选"现在重构"：关抽屉并导航到知识库页（reembed 自动触发） */
  onNavigateToKnowledge?: () => void
}

const SECTIONS = [
  { key: 'dsm', ic: '⛁', labelKey: 'settings.section.dsm' },
  { key: 'llm', ic: '◎', labelKey: 'settings.section.llm' },
  { key: 'privacy', ic: '◈', labelKey: 'settings.section.privacy' },
  { key: 'general', ic: '⚙', labelKey: 'settings.section.general' }
] as const

type Sec = (typeof SECTIONS)[number]['key']
type ModelTab = 'chat' | 'embedding'

/** 敏感名单（表/列过滤）UI 暂时屏蔽：与 ConnectionModal 的 SENSITIVE_UI_ENABLED 同步，想明白后再开 */
const SENSITIVE_UI_ENABLED = false

/** 连接卡：目标信息（路径 / host·库）为主内容，敏感名单仅配置时展示；点卡即切为当前；设为默认即时点亮。 */
function ConnRow({ conn, onEdit, onRemove, onSetDefault, onSelect, isCurrent, isDefault }: {
  conn: { id: string; name: string; dialect: string; host: string; port: number | null; database: string; user: string; file: string; read_only?: boolean; sensitive?: SensitiveEntry[] }
  onEdit: () => void
  onRemove: () => void
  onSetDefault: () => void
  onSelect: () => void
  isCurrent: boolean
  isDefault: boolean
}): React.JSX.Element {
  const { t } = useI18n()
  const [testing, setTesting] = useState(false)
  const [test, setTest] = useState<ConnTestState | null>(() => loadTestState(conn.id))

  async function handleTest(): Promise<void> {
    setTesting(true)
    try {
      const r = await testConnection(conn.id)
      const st: ConnTestState = { ok: r.ok, latency_ms: r.latency_ms, error: r.error ?? undefined, ts: Date.now() }
      saveTestState(conn.id, st)
      setTest(st)
      toastMsg(r.ok
        ? t('conn.modal.testOk', { ms: r.latency_ms })
        : (r.error ?? t('common.unknownError')))
    } catch (e) {
      const st: ConnTestState = { ok: false, error: (e as Error).message, ts: Date.now() }
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

  const sensList = (conn.sensitive ?? []).map((e) =>
    typeof e === 'string' ? e
      : ((e.columns ?? []).length ? `${e.table}:${(e.columns ?? []).join(',')}` : e.table)
  ).join(', ')
  const testLabel = testing ? '…' : t('settings.conn.test')
  return (
    <div
      className={`conn-row${isCurrent ? ' cur' : ''}`}
      onClick={onSelect}
      title={t('settings.conn.selectTitle')}
    >
      <div className="conn-main">
        <div className="conn-line1">
          <span className="conn-name">{conn.name}</span>
        </div>
        <div className="conn-tags">
          <span className="conn-dialect mono">{conn.dialect}</span>
          <span className={`ro-tag mono${conn.read_only ? ' on' : ''}`}>{t('conn.readOnly')}</span>
          <span
            className={`ok-tag mono${test && test.ok ? ' on' : ''}`}
            title={test && !test.ok ? test.error ?? '' : (test?.ok ? `${test.latency_ms ?? ''}ms` : '')}
          >{t('settings.conn.testOkTag')}</span>
        </div>
        <div className="conn-target mono" title={targetTitle}>
          <span className="ct-ic">{isSqlite ? '▤' : '◈'}</span>
          <span className="ct-body">
            <span className="ct-line">{addr}</span>
            {dbName && <span className="ct-line sub">db:{dbName}</span>}
          </span>
        </div>
        {SENSITIVE_UI_ENABLED && sensList && (
          <div className="conn-sens mono">
            <span className="sens-label">{t('settings.conn.sensitiveList')}：</span>
            <span className="sens-list" title={sensList}>{sensList}</span>
          </div>
        )}
      </div>
      <div className="conn-side">
        <div className="conn-actions" onClick={(e) => e.stopPropagation()}>
          <button
            className={`mini-btn${isDefault ? ' set' : ''}`}
            onClick={onSetDefault}
            title={isDefault ? t('settings.conn.isDefaultTitle') : t('settings.conn.setDefaultTitle')}
          >
            {isDefault ? t('settings.conn.isDefault') : t('settings.conn.setDefault')}
          </button>
          <button className="mini-btn" onClick={onEdit}>{t('settings.conn.edit')}</button>
          <button
            className="mini-btn test"
            disabled={testing}
            onClick={() => void handleTest()}
            title={test ? (test.ok ? `${test.latency_ms ?? ''}ms` : test.error) : t('settings.conn.test')}
          >
            {testLabel}
          </button>
          <button className="mini-btn dang" onClick={onRemove}>{t('common.delete')}</button>
        </div>
      </div>
    </div>
  )
}

function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`
}

/** 隐私档四选一（互斥单选）：open / standard / strict / custom */
const PRIVACY_MODES = ['open', 'standard', 'strict', 'custom'] as const

/** 自定义档选择器缓存的 schema：按连接懒加载；cols 带列注释（无注释为空串） */
type PvCol = { name: string; comment: string }
type PvSchemaState = { state: 'loading' | 'error' | 'ready'; tables: { name: string; columns: PvCol[] }[] }

/** 通用区卡片：可选图标 + 标题 + 右侧 mono 副标 + 主体；视觉语言对齐 model-item / conn-row。 */
function GenCard({ ic, title, sub, children }: {
  ic?: string
  title: string
  sub?: string
  children: React.ReactNode
}): React.JSX.Element {
  return (
    <div className="gen-card">
      <div className="gc-head">
        {ic && <span className="gc-ic">{ic}</span>}
        <span className="gc-title">{title}</span>
        {sub && <span className="gc-sub mono">{sub}</span>}
      </div>
      <div className="gc-body">{children}</div>
    </div>
  )
}

// 后端对已保存的 api_key 返回脱敏掩码（sk-abc•••xyz）——含 ••• 即表示"有存量 key，前端拿不到真值"
function isMaskedKey(k: unknown): boolean {
  return typeof k === 'string' && k.includes('•••')
}

/** 向量嵌入合规接入点（OpenAI 兼容 /embeddings）。硬必需：未配置则知识库不可用。 */
const VECTOR_PROVIDERS: { name: string; model: string; url: string }[] = [
  { name: '硅基流动', model: 'BAAI/bge-m3', url: 'https://cloud.siliconflow.cn' },
  { name: '阿里云百炼', model: 'text-embedding-v3', url: 'https://bailian.console.aliyun.com' },
  { name: '智谱', model: 'embedding-3', url: 'https://bigmodel.cn' },
  { name: 'Jina', model: 'jina-embeddings-v3', url: 'https://jina.ai/embeddings/' },
  { name: '本地 Ollama', model: '/v1/embeddings', url: 'https://ollama.com' },
]

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

export function SettingsDrawer({ open, onClose, onNewConnection, onEditConnection, initialSec, onNavigateToKnowledge }: Props): React.JSX.Element | null {
  const [sec, setSec] = useState<Sec>('dsm')
  const { list, currentId, defaultId, remove, setDefault, select, update } = useConnections()
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
  // 打开时可按引导跳指定分区（仅本次打开生效）
  useEffect(() => {
    if (open && initialSec) setSec(initialSec)
  }, [open, initialSec])
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
  // 图上节点字号档位（1..5），localStorage 记忆
  const [graphFont, setGraphFont] = useState<number>(() => getGraphFontLevel())
  const applyGraphFont = (l: number): void => { setGraphFontLevel(l); setGraphFont(l) }

  // 平台级运行参数（安全闸门 / 通用）
  const [settings, setSettings] = useState<SettingsPublic | null>(null)
  const [maxRows, setMaxRows] = useState<number>(1000)
  const [poolSize, setPoolSize] = useState<number>(3)
  const [privacyMode, setPrivacyMode] = useState<string>('standard')
  // 隐私档切换二次确认：null = 无待切换；确认后才 persistPrivacy
  const [privacyConfirm, setPrivacyConfirm] = useState<null | { to: string }>(null)
  async function persistPrivacy(mode: string): Promise<void> {
    try {
      await updateSettings({ privacy_mode: mode } as unknown as SettingsPatch)
      setSettings((s) => (s ? { ...(s as SettingsPublic), privacy_mode: mode } as SettingsPublic : s))
      setPrivacyMode(mode)
      toastMsg(t('settings.privacyCards.toastSwitched', { mode: t(`settings.privacyCards.${mode}.title`) }))
    } catch (e) {
      toastMsg((e as Error).message || t('common.unknownError'))
    }
  }
  function askPrivacy(to: string): void {
    if (to === privacyMode) return
    setPrivacyConfirm({ to })
  }

  // ---- 自定义档：按数据源折叠的字段选择器（勾选即存连接 sensitive） ----
  const [pvOpenConn, setPvOpenConn] = useState<string | null>(null)
  const [pvOpenTable, setPvOpenTable] = useState<string | null>(null)
  const [pvSchema, setPvSchema] = useState<Record<string, PvSchemaState>>({})

  function sensOf(connId: string): SensitiveEntry[] {
    return list.find((c) => c.id === connId)?.sensitive ?? []
  }
  /** 数据源条第二行：SQLite 显文件路径；PG/MySQL 显 host:port · 库名 */
  function pvcConnTarget(conn: { dialect: string; host: string; port: number | null; database: string; file: string }): string {
    if (conn.dialect === 'sqlite') return conn.file || '—'
    const addr = conn.host ? (conn.port ? `${conn.host}:${conn.port}` : conn.host) : '—'
    return conn.database ? `${addr} · ${conn.database}` : addr
  }
  function tableEntry(connId: string, table: string): { table: string; columns?: string[] } | null {
    return (sensOf(connId).filter((e): e is { table: string; columns?: string[] } => typeof e !== 'string' && e.table === table))[0] ?? null
  }
  /** 数据源条统计：整表条目算 1 表；列条目按列数累加；旧 glob 字符串条目按 1 表计 */
  function connCount(connId: string): { tables: number; cols: number } {
    let tables = 0, cols = 0
    for (const e of sensOf(connId)) {
      if (typeof e === 'string') { tables += 1; continue }
      if (e.columns && e.columns.length) cols += e.columns.length
      else tables += 1
    }
    return { tables, cols }
  }
  /** 表条摘要：整表=「整表 · N 字段」；部分=前3列名+…；无=0 */
  function tableSummary(connId: string, table: string, totalCols: number): string {
    const entry = tableEntry(connId, table)
    if (!entry) return '0'
    if (!entry.columns || entry.columns.length === 0) {
      return t('settings.privacyCards.custom.sumWhole', { n: totalCols })
    }
    const cs = entry.columns
    if (cs.length <= 3) return cs.join(', ')
    return `${cs.slice(0, 3).join(', ')} …+${cs.length - 3}`
  }
  function customTotals(): { tables: number; cols: number } {
    let tables = 0, cols = 0
    for (const conn of list) {
      for (const e of conn.sensitive ?? []) {
        if (typeof e === 'string') { tables += 1; continue }
        if (e.columns && e.columns.length) cols += e.columns.length
        else tables += 1
      }
    }
    return { tables, cols }
  }
  async function ensureSchema(connId: string): Promise<void> {
    const cur = pvSchema[connId]
    if (cur && (cur.state === 'ready' || cur.state === 'loading')) return
    setPvSchema((s) => ({ ...s, [connId]: { state: 'loading', tables: [] } }))
    try {
      const sc = await getSchema(connId)
      const tabs = (sc.tables ?? []).map((tb) => ({
        name: tb.name,
        columns: (sc.columns ?? []).filter((c) => c.table === tb.name).map((c) => ({ name: c.name, comment: c.comment || '' })),
      }))
      setPvSchema((s) => ({ ...s, [connId]: { state: 'ready', tables: tabs } }))
    } catch {
      setPvSchema((s) => ({ ...s, [connId]: { state: 'error', tables: [] } }))
    }
  }
  function setSens(connId: string, next: SensitiveEntry[]): void {
    void update(connId, { sensitive: next })
  }
  function toggleWholeTable(connId: string, table: string): void {
    const has = tableEntry(connId, table) !== null
    const others = sensOf(connId).filter((e) => !(typeof e !== 'string' && e.table === table))
    setSens(connId, has ? others : [...others, { table }])
  }
  function toggleCol(connId: string, table: string, col: string): void {
    const entry = tableEntry(connId, table)
    const allCols = (pvSchema[connId]?.tables.find((x) => x.name === table)?.columns ?? []).map((c) => c.name)
    let cols: string[]
    if (entry && !entry.columns) {
      // 整表选中 → 取消一列 = 除该列外全选
      cols = allCols.filter((c) => c !== col)
    } else {
      const set = new Set(entry?.columns ?? [])
      if (set.has(col)) set.delete(col); else set.add(col)
      cols = [...set]
    }
    const others = sensOf(connId).filter((e) => !(typeof e !== 'string' && e.table === table))
    if (cols.length === 0) { setSens(connId, others); return }
    if (allCols.length && cols.length === allCols.length) { setSens(connId, [...others, { table }]); return }
    setSens(connId, [...others, { table, columns: cols }])
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
  const [testOk, setTestOk] = useState(true)  // 模型测试结果状态（样式判定，不再靠文案符号）
  // 测试步骤圆点：[连通性, FC, 流式, 上下文窗口] — null=等待, true=通过, false=失败
  const [testSteps, setTestSteps] = useState<(boolean | null)[]>([null, null, null, null])
  // 内置供应商清单 + 上游模型列表拉取状态（仅 chat 面板）
  const [builtinProviders, setBuiltinProviders] = useState<BuiltinProvider[]>([])
  const [builtinEmbProviders, setBuiltinEmbProviders] = useState<BuiltinProvider[]>([])
  const [modelList, setModelList] = useState<{ state: 'idle' | 'loading' | 'ok' | 'fail'; models: string[]; msg: string }>({ state: 'idle', models: [], msg: '' })
  // 默认向量模型 model 字段变更 → 二次确认（暂挂保存的模型 id）
  const [embSwitchAsk, setEmbSwitchAsk] = useState<string | null>(null)
  // 自定义名称编辑态：选"自定义"后锁住，清空输入框时不会跳走；选其他内置条目时退出
  const [customNameMode, setCustomNameMode] = useState(false)

  // 删除二次确认：null = 无待删，命中后弹确认框，确认才真正删除
  const [rmTarget, setRmTarget] = useState<null | { kind: 'conn' | 'chat' | 'emb'; id: string; label: string }>(null)
  function askRemoveConn(c: { id: string; name: string }): void { setRmTarget({ kind: 'conn', id: c.id, label: c.name }) }
  function askRemoveChat(m: AiModelConfig): void { setRmTarget({ kind: 'chat', id: m.id, label: providerLabel(m) }) }
  function askRemoveEmb(m: EmbeddingModelConfig): void { setRmTarget({ kind: 'emb', id: m.id, label: providerLabel(m) }) }
  function doRemove(): void {
    const rm = rmTarget
    if (!rm) return
    if (rm.kind === 'chat') removeChatModel(rm.id)
    else if (rm.kind === 'emb') removeEmbModel(rm.id)
    else void remove(rm.id)
    setRmTarget(null)
  }

  // 打开时加载配置
  useEffect(() => {
    if (!open) return
    let alive = true
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
      })
      .catch(() => undefined)
    void getBuiltinProviders()
      .then((ps) => { if (alive) setBuiltinProviders(ps) })
      .catch(() => undefined)
    void getBuiltinEmbeddingProviders()
      .then((ps) => { if (alive) setBuiltinEmbProviders(ps) })
      .catch(() => undefined)
    return () => { alive = false }
  }, [open])

  function startNewChat(): void {
    setEditingChat('new')
    setEditing({
      id: uid('llm'),
      provider: '',
      base_url: '',
      api_key: '',
      model: '',
      temperature: 0.2,
      timeout: 120,
      reasoning: null,
    })
    setTestMsg('')
    setModelList({ state: 'idle', models: [], msg: '' })
    setCustomNameMode(false)
  }

  function startEditChat(m: AiModelConfig): void {
    setEditingChat(m.id)
    setEditing({ ...m })
    setTestMsg('')
    setModelList({ state: 'idle', models: [], msg: '' })
    // 自由命名模型（provider 不在内置列表里）→ 锁住自定义名称输入
    setCustomNameMode(isCustomProv(builtinProviders, m.provider))
  }

  function startNewEmb(): void {
    setEditingEmb('new')
    setEditing({
      id: uid('emb'),
      provider: '',
      base_url: '',
      api_key: '',
      model: '',
    })
    setTestMsg('')
    setCustomNameMode(false)
  }

  function startEditEmb(m: EmbeddingModelConfig): void {
    setEditingEmb(m.id)
    setEditing({ ...m })
    setTestMsg('')
    setCustomNameMode(isCustomProv(builtinEmbProviders, m.provider))
  }

  function cancelEdit(): void {
    setEditingChat(null)
    setEditingEmb(null)
    setEditing({})
    setTestMsg('')
    setModelList({ state: 'idle', models: [], msg: '' })
    setCustomNameMode(false)
  }

  // ---- 供应商下拉 + 模型列表拉取（chat / embedding 面板共用） ----

  /** 存储的 provider 值 → 下拉选中 key：内置条目按 display 匹配（同名双条目时 URL 对号），历史自由命名原样作 key。 */
  function matchBuiltinKey(providers: BuiltinProvider[], provider: unknown, baseUrl: unknown): string {
    const v = String(provider ?? '').trim()
    if (!v) return ''
    const url = String(baseUrl ?? '').trim()
    const both = providers.find((b) => b.name === v && b.base_url && b.base_url === url)
    if (both) return both.display
    const byName = providers.filter((b) => b.name === v)
    if (byName.length) return byName[0].display
    return v
  }

  function providerOptions(providers: BuiltinProvider[], baseUrl?: unknown): DropdownOption[] {
    const opts: DropdownOption[] = providers.map((b) => ({ value: b.display, label: b.display, hint: b.base_url || undefined }))
    const cur = matchBuiltinKey(providers, editing.provider, baseUrl ?? editing.base_url)
    if (cur && !opts.some((o) => o.value === cur)) {
      // 历史自由命名（如"火山方舟"）：保留原值，不悄悄改写用户数据
      opts.push({ value: cur, label: cur, hint: String(baseUrl ?? editing.base_url ?? '') || undefined })
    }
    return opts
  }

  function applyProviderPick(providers: BuiltinProvider[], key: string, resetFetch: boolean): void {
    const hit = providers.find((b) => b.display === key)
    setEditing((prev) => {
      if (!hit) return { ...prev, provider: key }
      const firstHint = hit.models.split('|').map((s) => s.trim()).filter(Boolean)[0] ?? ''
      return {
        ...prev,
        provider: hit.name,
        base_url: hit.base_url || prev.base_url,
        model: prev.model ? prev.model : firstHint,
      }
    })
    if (resetFetch) setModelList({ state: 'idle', models: [], msg: '' })
  }

  function onProviderChange(key: string): void {
    applyProviderPick(builtinProviders, key, true)
    const hit = builtinProviders.find((b) => b.display === key)
    // 内置供应商 reasoning 由 adapter 层文档声明背书，选中即标定（免测）；custom 需实测不预设
    if (hit && hit.name !== 'custom') {
      setEditing((prev) => ({ ...prev, reasoning: true }))
      setCustomNameMode(false)
    } else {
      // 选中"自定义"或自由命名（历史 raw key）→ 锁住名称输入，清空不跳走
      setCustomNameMode(true)
    }
  }

  function onEmbProviderChange(key: string): void {
    applyProviderPick(builtinEmbProviders, key, false)
    const hit = builtinEmbProviders.find((b) => b.display === key)
    if (hit?.dimensions) {
      const dims = parseInt(hit.dimensions, 10)
      if (dims > 0) {
        setEditing((prev) => (
          { ...prev, dimensions: (prev as Partial<EmbeddingModelConfig>).dimensions ?? dims } as typeof prev
        ))
      }
    }
    // 非自定义 → 退出自定义名称模式；自定义 → 锁住（清空不跳走）
    setCustomNameMode(!hit || hit.name === 'custom')
  }

  /** 选中"自定义"或历史自由命名 → 显示自定义名称输入（名称存 provider 字段，别名表兜底归一 custom）。 */
  function isCustomProv(providers: BuiltinProvider[], prov: unknown): boolean {
    const v = String(prov ?? '').trim()
    if (!v || v === 'mock') return false
    return v === 'custom' || !providers.some((b) => b.name === v)
  }

  async function handleFetchModels(): Promise<void> {
    const baseUrl = String(editing.base_url ?? '').trim()
    if (!baseUrl) return
    setModelList({ state: 'loading', models: [], msg: '' })
    // 掩码 key = 未改 → 传空；编辑已存模型时后端用 model_id 回退取存 key
    const effectiveKey = isMaskedKey(editing.api_key) ? '' : (editing.api_key ?? '')
    const mid = editingChat && editingChat !== 'new' ? editingChat : undefined
    try {
      const r = await fetchUpstreamModels({ baseUrl, apiKey: effectiveKey, modelId: mid })
      if (r.ok && r.models?.length) setModelList({ state: 'ok', models: r.models, msg: '' })
      else setModelList({ state: 'idle', models: [], msg: '' })  // 静默回退自由输入（不挤变形、不显示错误码）
    } catch {
      setModelList({ state: 'idle', models: [], msg: '' })
    }
  }

  async function handleTest(): Promise<void> {
    setTesting(true)
    setTestMsg('')
    setTestSteps([null, null, null, null])
    // 掩码 = 用户未改 key → 传空，让后端用已存的真实 key（避免把掩码当 key 打向 API）
    const effectiveKey = isMaskedKey(editing.api_key) ? '' : editing.api_key
    // SSE 步骤索引映射
    const stepIdx: Record<string, number> = { ping: 0, fc: 1, streaming: 2, context_window: 3 }
    const onStep = (step: string, ok: boolean): void => {
      const idx = stepIdx[step]
      if (idx != null) setTestSteps((prev) => { const next = [...prev]; next[idx] = ok; return next })
    }
    try {
      if (mTab === 'chat') {
        const r = await testGateway({
          model_id: editingChat === 'new' ? undefined : (editingChat ?? undefined),
          provider: editing.provider as string,
          baseUrl: editing.base_url,
          apiKey: effectiveKey,
          model: editing.model,
          onStep,
        })
        if (r.ok) {
          const caps = r.capabilities
          setEditing((prev) => ({
            ...prev,
            last_test: { ok: true, latency_ms: r.latency_ms, capabilities: caps },
            reasoning: caps?.reasoning != null ? caps.reasoning : ((prev as Partial<AiModelConfig>).reasoning ?? null),
          }))
          // done 事件兜底：确保所有步骤状态与 capabilities 一致
          setTestSteps([
            caps?.connectivity ?? false,
            caps?.function_calling ?? false,
            caps?.streaming ?? false,
            caps?.context_window != null && caps.context_window > 0,
          ])
          const capTexts = []
          if (caps?.connectivity) capTexts.push(t('settings.cap.connectivity'))
          if (caps?.function_calling) capTexts.push(t('settings.cap.fc'))
          if (caps?.reasoning) capTexts.push(t('settings.cap.reasoning'))
          if (caps?.streaming) capTexts.push(t('settings.cap.streamingFull'))
          if (caps?.context_window) capTexts.push(`${Math.round(caps.context_window / 1000)}K`)
          setTestMsg(`${t('settings.testOk', { ms: r.latency_ms ?? '?' })}${capTexts.length ? ` · ${capTexts.join(' / ')}` : ''}`)
          setTestOk(true)
        } else {
          setTestSteps([false, null, null, null])
          setTestMsg(t('settings.testFail', { error: r.error ?? t('common.unknownError') }))
          setTestOk(false)
        }
      } else {
        const r = await testEmbedding({
          model_id: editingEmb === 'new' ? undefined : (editingEmb ?? undefined),
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
          setTestSteps([true, r.dimensions != null && r.dimensions > 0])
          setTestMsg(t('settings.embTestOk', { dims: r.dimensions ?? 0, ms: r.latency_ms ?? '?' }))
          setTestOk(true)
        } else {
          setTestSteps([false, null])
          setTestMsg(t('settings.testFail', { error: r.error ?? t('common.unknownError') }))
          setTestOk(false)
        }
      }
    } catch (e) {
      setTestMsg(t('settings.testFail', { error: (e as Error).message }))
      setTestOk(false)
    } finally {
      setTesting(false)
    }
  }

  // 展示与保存以「供应商名称」为唯一标识；name 字段仅为旧格式兼容，保存时同步为供应商名
  function providerLabel(p: Partial<AiModelConfig> | Partial<EmbeddingModelConfig>, fallback?: string): string {
    const v = String(p.provider ?? '').trim()
    if (!v) return fallback || ''
    // 规范名 → 内置显示名（volcano → 火山…）；历史自由命名原样展示
    const hit = builtinProviders.find((b) => b.name === v)
    return hit ? hit.display : v
  }

  function saveEdit(): void {
    if (mTab === 'chat') {
      const id = editing.id!
      const old = aiModels.find((m) => m.id === id)
      const entry = { ...editing, name: providerLabel(editing, old?.name) } as AiModelConfig
      const next: AiModelConfig[] = old
        ? aiModels.map((m) => (m.id === id ? entry : m))
        : [...aiModels, entry]
      const nextDefault = !defaultAi || (editingChat === 'new' && next.length === 1) ? id : defaultAi
      setAiModels(next)
      setDefaultAi(nextDefault)
      setEditingChat(null)
      persist(next, nextDefault)
      setEditing({})
      setTestMsg('')
    } else {
      const id = editing.id!
      const old = embModels.find((m) => m.id === id)
      // 默认向量模型 model 变更（改显示名/URL 不算）→ 二次确认是否立即重嵌，暂挂保存
      if (old && defaultEmb === id && String(old.model ?? '') !== String(editing.model ?? '')) {
        setEmbSwitchAsk(id)
        return
      }
      doSaveEmb()
    }
  }

  /** 实际保存向量模型（embSwitchAsk 确认后也走这里）。 */
  function doSaveEmb(): void {
    const id = editing.id!
    const old = embModels.find((m) => m.id === id)
    const entry = { ...editing, name: providerLabel(editing, old?.name) } as EmbeddingModelConfig
    const next: EmbeddingModelConfig[] = old
      ? embModels.map((m) => (m.id === id ? entry : m))
      : [...embModels, entry]
    const nextDefault = !defaultEmb || (editingEmb === 'new' && next.length === 1) ? id : defaultEmb
    setEmbModels(next)
    setDefaultEmb(nextDefault)
    setEditingEmb(null)
    setEditing({})
    setTestMsg('')
    persist(undefined, undefined, next, nextDefault)
  }

  /** 确认框「现在重构」：保存 → 关抽屉 → 导航知识库页（进入即自动 reembed）。 */
  function embSwitchRebuild(): void {
    setEmbSwitchAsk(null)
    doSaveEmb()
    onNavigateToKnowledge?.()
  }

  /** 确认框「不用」：仅保存，留在设置页（重嵌在下次进入知识库时自动触发）。 */
  function embSwitchLater(): void {
    setEmbSwitchAsk(null)
    doSaveEmb()
  }

  // 改动即存：每次操作都立即落盘。连续操作各自异步 PUT，乱序到达会互相覆盖（先发后达丢更新），
  // 因此所有落盘经 persistChain 串行排队，保证按操作顺序到达后端。
  const persistChain = useRef<Promise<unknown>>(Promise.resolve())
  async function doPersist(
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
      return true
    } catch (e) {
      return false
    }
  }
  function persist(
    models: AiModelConfig[] = aiModels,
    def = defaultAi,
    embs: EmbeddingModelConfig[] = embModels,
    defEmb = defaultEmb,
  ): Promise<boolean> {
    const run = persistChain.current.then(() => doPersist(models, def, embs, defEmb))
    persistChain.current = run.catch(() => undefined)
    return run
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

  // 编辑面板：attached=贴在对应卡片下方（编辑态），缺省=列表尾部独立展开（新建态）
  const chatEditPanel = (mod?: string): React.JSX.Element => (
    <div className={`model-edit${mod ? ` ${mod}` : ''}`}>
      <div className="me-row">
        <label>{t('settings.model.providerName')}</label>
        <Dropdown
          value={matchBuiltinKey(builtinProviders, editing.provider, editing.base_url)}
          options={providerOptions(builtinProviders)}
          onChange={onProviderChange}
          placeholder={t('settings.model.providerPh')}
        />
      </div>
      {(isCustomProv(builtinProviders, editing.provider) || customNameMode) && (
        <div className="me-row">
          <label>{t('settings.model.customName')}</label>
          <input autoComplete="off" value={editing.provider ?? ''} onChange={(e) => setEditing({ ...editing, provider: e.target.value })} placeholder="如 my-vllm / 私有网关" />
        </div>
      )}
      <div className="me-row">
        <label>Base URL</label>
        <input autoComplete="off" value={editing.base_url ?? ''} onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
      </div>
      <div className="me-row">
        <label>{t('settings.model.modelId')}</label>
        {modelList.state === 'ok' ? (
          <Dropdown
            value={modelList.models.includes(String(editing.model ?? '')) ? String(editing.model ?? '') : ''}
            options={[
              { value: '', label: t('settings.model.modelManual') },
              ...modelList.models.map((m) => ({ value: m, label: m })),
            ]}
            onChange={(v) => {
              if (v === '') { setModelList({ state: 'idle', models: [], msg: '' }); return }
              setEditing((prev) => ({ ...prev, model: v }))
            }}
            placeholder={t('settings.model.chooseModel')}
          />
        ) : (
          <>
            <input autoComplete="off" value={editing.model ?? ''} onChange={(e) => setEditing({ ...editing, model: e.target.value })} placeholder="gpt-4o / deepseek-v4-flash" />
            <button
              type="button"
              className="mini-btn"
              disabled={modelList.state === 'loading' || !String(editing.base_url ?? '').trim()}
              onClick={() => void handleFetchModels()}
            >
              {modelList.state === 'loading' ? t('settings.model.fetching') : t('settings.model.fetchModels')}
            </button>
          </>
        )}
      </div>
      <div className="me-row">
        <label>API Key</label>
        <input type="password" autoComplete="new-password" value={isMaskedKey(editing.api_key) ? '' : (editing.api_key ?? '')} onChange={(e) => setEditing({ ...editing, api_key: e.target.value })} placeholder={isMaskedKey(editing.api_key) ? t('settings.model.keySaved', { key: editing.api_key ?? '' }) : t('settings.model.keyNew')} />
      </div>
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
        <button className="btn tl" disabled={testing || !editing.base_url?.trim()} onClick={() => void handleTest()}>
          {testing ? t('conn.modal.testing') : t('conn.modal.test')}
        </button>
        <span className="test-dots">
          {testSteps.map((s, i) => (
            <span key={i} className={`test-dot${s === true ? ' ok' : s === false ? ' fail' : ''}`} title={['连通性','Function Calling','流式输出','上下文窗口'][i]}>
              {s === true ? '●' : s === false ? '●' : '○'}
            </span>
          ))}
        </span>
        <span className="spacer" />
        <button className="mini-btn" onClick={cancelEdit}>{t('common.cancel')}</button>
        <button className="btn save" onClick={saveEdit}>{editingChat === 'new' ? t('settings.model.add') : t('common.save')}</button>
      </div>
      {testMsg && <div className={`me-test${testOk ? ' ok' : ' bad'}`}>{testMsg}</div>}
    </div>
  )

  const embEditPanel = (mod?: string): React.JSX.Element => (
    <div className={`model-edit${mod ? ` ${mod}` : ''}`}>
      <div className="me-row">
        <label>{t('settings.model.providerName')}</label>
        <Dropdown
          value={matchBuiltinKey(builtinEmbProviders, editing.provider, editing.base_url)}
          options={providerOptions(builtinEmbProviders)}
          onChange={onEmbProviderChange}
          placeholder={t('settings.model.providerPh')}
        />
      </div>
      {(isCustomProv(builtinEmbProviders, editing.provider) || customNameMode) && (
        <div className="me-row">
          <label>{t('settings.model.customName')}</label>
          <input autoComplete="off" value={editing.provider ?? ''} onChange={(e) => setEditing({ ...editing, provider: e.target.value })} placeholder="如 my-vllm / 私有网关" />
        </div>
      )}
      <div className="me-row">
        <label>Base URL</label>
        <input autoComplete="off" value={editing.base_url ?? ''} onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
      </div>
      <div className="me-row">
        <label>{t('settings.model.modelId')}</label>
        <input autoComplete="off" value={editing.model ?? ''} onChange={(e) => setEditing({ ...editing, model: e.target.value })} placeholder="text-embedding-3-small / bge-m3" />
      </div>
      <div className="me-row">
        <label>API Key</label>
        <input type="password" autoComplete="new-password" value={isMaskedKey(editing.api_key) ? '' : (editing.api_key ?? '')} onChange={(e) => setEditing({ ...editing, api_key: e.target.value })}
          placeholder={isMaskedKey(editing.api_key) ? t('settings.model.keySaved', { key: editing.api_key ?? '' }) : t('settings.model.keyNew')} />
      </div>
      <div className="me-actions">
        <button className="btn tl" disabled={testing || !editing.base_url?.trim()} onClick={() => void handleTest()}>
          {testing ? t('conn.modal.testing') : t('conn.modal.test')}
        </button>
        <span className="test-dots">
          {testSteps.map((s, i) => (
            <span key={i} className={`test-dot${s === true ? ' ok' : s === false ? ' fail' : ''}`} title={['连通性','维度探测'][i] ?? ''}>
              {s === true ? '●' : s === false ? '●' : '○'}
            </span>
          ))}
        </span>
        <span className="spacer" />
        <button className="mini-btn" onClick={cancelEdit}>{t('common.cancel')}</button>
        <button className="btn save" onClick={saveEdit}>{editingEmb === 'new' ? t('settings.model.add') : t('common.save')}</button>
      </div>
      <div className="me-prov mono">
        {t('settings.llm.embProviders')}
        {VECTOR_PROVIDERS.map((p) => (
          <a key={p.name} href={p.url} target="_blank" rel="noreferrer">{p.name}</a>
        ))}
      </div>
      {testMsg && <div className={`me-test${testOk ? ' ok' : ' bad'}`}>{testMsg}</div>}
    </div>
  )

  if (!open) return null

  return (
    <div className="set-mask" onClick={(e) => e.target === e.currentTarget && handleClose()}>
      <aside className={`set-drawer${closing ? ' closing' : ''}`}>
        <div className="set-head">
          <span className="set-title">{t('settings.titleFull')}</span>
          <span className="set-sub mono">{t('settings.sub')}</span>
          <CloseBtn className="set-x" title={t('common.close')} onClick={handleClose} />
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
                <div className="conn-list">
                  {list.map((c) => (
                    <ConnRow
                      key={c.id}
                      conn={c}
                      onEdit={() => onEditConnection(c.id)}
                      onRemove={() => askRemoveConn(c)}
                      onSetDefault={() => setDefault(c.id)}
                      onSelect={() => select(c.id)}
                      isCurrent={c.id === currentId}
                      isDefault={c.id === defaultId}
                    />
                  ))}
                  {list.length === 0 && <div className="mpage-empty">{t('settings.dsm.empty')}</div>}
                  <button className="conn-add" onClick={onNewConnection}><IconPlus size={11} /> {t('settings.conn.addConnection')}</button>
                </div>
              </section>
            )}

            {sec === 'llm' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.llm')}<span className="sec-s mono">{t('settings.llm.sub')}</span></div>

                <div className="model-tabs">
                  <button className={`model-tab${mTab === 'chat' ? ' on' : ''}`} onClick={() => { setMTab('chat'); cancelEdit() }}>
                    {t('settings.llm.chatTab')}<span className="req-star">*</span>
                  </button>
                  <button className={`model-tab${mTab === 'embedding' ? ' on' : ''}`} onClick={() => { setMTab('embedding'); cancelEdit() }}>
                    {t('settings.llm.embTab')}<span className="req-star">*</span>
                  </button>
                </div>

                {mTab === 'chat' && (
                  <>
                    <div className="model-list">
                      {aiModels.map((m) => (
                        <Fragment key={m.id}>
                          <div className={`model-item${m.id === defaultAi ? ' cur' : ''}`}>
                            <div className="mi-head">
                              <span className="mi-name">{providerLabel(m)}</span>
                              <span className="mi-model">{m.model || '—'}</span>
                              <div className="mi-actions">
                                <button className={`mini-btn set${m.id === defaultAi ? ' cur' : ''}`} onClick={() => { setDefaultAi(m.id); persist(aiModels, m.id) }}>
                                {m.id === defaultAi ? t('settings.model.current') : t('settings.model.setDefault')}
                              </button>
                              <button className="mini-btn" onClick={() => startEditChat(m)}>{t('settings.model.edit')}</button>
                              {!m.builtin && aiModels.length > 1 && (
                                <button className="mini-btn dang" onClick={() => askRemoveChat(m)}>{t('common.delete')}</button>
                              )}
                            </div>
                          </div>
                          <div className="mi-cap">
                            <CapBadges caps={m.last_test?.capabilities} />
                          </div>
                          </div>
                          {editingChat === m.id && chatEditPanel('attached')}
                        </Fragment>
                      ))}
                      {aiModels.length === 0 && <div className="model-empty">{t('settings.llm.emptyChat')}</div>}
                    </div>

                    {editingChat === 'new' && chatEditPanel()}

                    {!editingChat && (
                      <button className="model-add" onClick={startNewChat}><IconPlus size={11} /> {t('settings.llm.addChat')}</button>
                    )}
                  </>
                )}

                {mTab === 'embedding' && (
                  <>
                    <div className="model-list">
                      {embModels.map((m) => (
                        <Fragment key={m.id}>
                          <div className={`model-item${m.id === defaultEmb ? ' cur' : ''}`}>
                            <div className="mi-head">
                              <span className="mi-name">{providerLabel(m)}</span>
                              <span className="mi-model">{m.model || '—'}</span>
                              <div className="mi-actions">
                                <button className={`mini-btn set${m.id === defaultEmb ? ' cur' : ''}`} onClick={() => { setDefaultEmb(m.id); persist(undefined, undefined, embModels, m.id) }}>
                                  {m.id === defaultEmb ? t('settings.model.current') : t('settings.model.setDefault')}
                                </button>
                                <button className="mini-btn" onClick={() => startEditEmb(m)}>{t('settings.model.edit')}</button>
                                {embModels.length > 1 && (
                                  <button className="mini-btn dang" onClick={() => askRemoveEmb(m)}>{t('common.delete')}</button>
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
                          {editingEmb === m.id && embEditPanel('attached')}
                        </Fragment>
                      ))}
                      {embModels.length === 0 && <div className="model-empty">{t('settings.llm.emptyEmb')}</div>}
                    </div>

                    {editingEmb === 'new' && embEditPanel()}

                    {!editingEmb && (
                      <button className="model-add" onClick={startNewEmb}><IconPlus size={11} /> {t('settings.llm.addEmb')}</button>
                    )}
                  </>
                )}
              </section>
            )}

            {sec === 'privacy' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.privacy')}<span className="sec-s mono">{t('settings.privacy.sub')}</span></div>
                <div className="gen-cards">
                  {PRIVACY_MODES.map((mode) => {
                    const isCur = privacyMode === mode
                    const totals = customTotals()
                    const customEmpty = mode === 'custom' && totals.tables === 0 && totals.cols === 0
                    return (
                      <div key={mode} className={`pvc-card${isCur ? ' cur' : ''}`}>
                        <div className="pvc-head">
                          <span className="pvc-title">{t(`settings.privacyCards.${mode}.title`)}</span>
                          {isCur
                            ? <span className="pvc-cur mono"><IconCheck size={10} /> {t('settings.privacyCards.current')}</span>
                            : <button className="btn tl pvc-btn" disabled={customEmpty} onClick={() => askPrivacy(mode)}>{t('settings.privacyCards.enable')}</button>}
                        </div>
                        <div className="pvc-desc">{t(`settings.privacyCards.${mode}.desc`)}</div>
                        {mode === 'standard' && (
                          <div className="pvc-detail mono">{t('settings.privacyCards.standardDetail')}</div>
                        )}
                        {mode === 'custom' && (
                          <>
                            <div className="pvc-detail mono">
                              {t('settings.privacyCards.custom.count', { tables: totals.tables, cols: totals.cols })}
                              {customEmpty && <span className="pvc-warn"> · {t('settings.privacyCards.custom.empty')}</span>}
                            </div>
                            <div className="pvc-picker">
                              {list.map((conn) => {
                                  const open = pvOpenConn === conn.id
                                  const sc = pvSchema[conn.id]
                                  const cc = connCount(conn.id)
                                  return (
                                    <div key={conn.id} className={`pvc-conn${open ? ' open' : ''}`}>
                                      <button
                                        className="pvc-conn-head"
                                        onClick={() => { const next = open ? null : conn.id; setPvOpenConn(next); setPvOpenTable(null); if (next) void ensureSchema(next) }}
                                      >
                                        <span className="pvc-caret">{open ? '▾' : '▸'}</span>
                                        <span className="pvc-conn-main">
                                          <span className="pvc-conn-l1">
                                            <span className="pvc-conn-name">{conn.name}</span>
                                            <span className="mono pvc-conn-dialect">{conn.dialect}</span>
                                            <span className="mono pvc-conn-cnt">{t('settings.privacyCards.custom.connCount', { tables: cc.tables, cols: cc.cols })}</span>
                                          </span>
                                          <span className="pvc-conn-l2 mono" title={pvcConnTarget(conn)}>
                                            {pvcConnTarget(conn)}
                                          </span>
                                        </span>
                                      </button>
                                      {open && (
                                        <div className="pvc-conn-body">
                                          {!sc && <div className="pvc-loading">{t('settings.privacyCards.custom.loading')}</div>}
                                          {sc?.state === 'loading' && <div className="pvc-loading">{t('settings.privacyCards.custom.loading')}</div>}
                                          {sc?.state === 'error' && <div className="pvc-loading">{t('settings.privacyCards.custom.loadFail')}</div>}
                                          {sc?.state === 'ready' && sc.tables.length === 0 && <div className="pvc-loading">{t('settings.privacyCards.custom.noTables')}</div>}
                                          {sc?.state === 'ready' && sc.tables.map((tb) => {
                                            const entry = tableEntry(conn.id, tb.name)
                                            const whole = entry != null && (!entry.columns || entry.columns.length === 0)
                                            const colSet = new Set(entry?.columns ?? [])
                                            const tOpen = pvOpenTable === tb.name
                                            return (
                                              <div key={tb.name} className="pvc-tbl">
                                                <div className="pvc-tbl-row">
                                                  <label className="pvc-check">
                                                    <input type="checkbox" checked={!!whole} onChange={() => toggleWholeTable(conn.id, tb.name)} />
                                                    <span className="pvc-tbl-name mono">{tb.name}</span>
                                                  </label>
                                                  <span className="pvc-tbl-sum mono" title={tableSummary(conn.id, tb.name, tb.columns.length)}>{tableSummary(conn.id, tb.name, tb.columns.length)}</span>
                                                  {tb.columns.length > 0 && (
                                                    <button className="pvc-caret-btn" onClick={() => setPvOpenTable(tOpen ? null : tb.name)}>{tOpen ? '▾' : '▸'}</button>
                                                  )}
                                                </div>
                                                {tOpen && tb.columns.length > 0 && (
                                                  <div className="pvc-cols">
                                                    {tb.columns.map((col) => (
                                                      <label key={col.name} className="pvc-check pvc-col" title={col.comment || col.name}>
                                                        <input type="checkbox" checked={whole || colSet.has(col.name)} onChange={() => toggleCol(conn.id, tb.name, col.name)} />
                                                        <span className="pvc-col-name mono">{col.name}</span>
                                                        <span className="pvc-col-cmt">{col.comment || '—'}</span>
                                                      </label>
                                                    ))}
                                                  </div>
                                                )}
                                              </div>
                                            )
                                          })}
                                        </div>
                                      )}
                                    </div>
                                  )
                                })}
                                {list.length === 0 && <div className="pvc-loading">{t('settings.privacyCards.custom.noConn')}</div>}
                            </div>
                          </>
                        )}
                      </div>
                    )
                  })}
                </div>
              </section>
            )}

            {sec === 'general' && (
              <section className="set-sec">
                <div className="sec-h">{t('settings.section.general')}<span className="sec-s mono">{t('settings.general.sub')}</span></div>
                <div className="gen-cards">
                  <GenCard title={t('settings.section.theme')}>
                    <div className="p-b">
                      <div className="theme-row">
                        {(['light', 'dark'] as const).map((th) => (
                          <button
                            key={th}
                            className={`theme-opt${theme === th ? ' on' : ''}`}
                            onClick={() => applyTheme(th)}
                          >
                            <span className="theme-opt-swatch" style={{ background: th === 'light' ? '#f4f5f7' : '#08090d' }} />
                            <span className="theme-opt-name">{th === 'light' ? t('settings.theme.light') : t('settings.theme.dark')}</span>
                            {theme === th && <span className="theme-opt-check mono"><IconCheck size={10} /></span>}
                          </button>
                        ))}
                      </div>
                      <div className="set-row">
                        <label>{t('settings.language')}</label>
                        <div className="seg">
                          <button className={`seg-b${locale === 'zh-CN' ? ' on' : ''}`} onClick={() => setLocale('zh-CN')}>
                            <span className="seg-tag">中</span>{t('settings.lang.zh')}
                          </button>
                          <button className={`seg-b${locale === 'en-US' ? ' on' : ''}`} onClick={() => setLocale('en-US')}>
                            <span className="seg-tag">EN</span>{t('settings.lang.en')}
                          </button>
                        </div>
                        <span className="set-hint">{t('settings.languageHint')}</span>
                      </div>
                      <div className="set-row set-row-col">
                        <label>{t('settings.graphFont')}</label>
                        <div className="font-preview-row">
                          {GRAPH_FONT_LEVELS.map((px, i) => {
                            const lv = i + 1
                            return (
                              <button
                                key={lv}
                                className={`font-preview-pill${graphFont === lv ? ' on' : ''}`}
                                onClick={() => applyGraphFont(lv)}
                              >
                                <span className="font-preview-text" style={{ fontSize: `${px}px` }}>users</span>
                              </button>
                            )
                          })}
                        </div>
                        <span className="set-hint">{t('settings.graphFontHint')}</span>
                      </div>
                    </div>
                  </GenCard>

                  <GenCard title={t('settings.general.cardRuntime')}>
                    <div className="p-b">
                      <div className="set-row inline"><span className="sr-l">{t('settings.general.maxRows')}</span><input type="number" min={1} value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value) || 1)} onBlur={() => { void persist(); }} /></div>
                      <div className="set-row inline"><span className="sr-l">{t('settings.general.poolSize')}</span><input className="gen-num-sm" type="number" min={1} value={poolSize} onChange={(e) => setPoolSize(Number(e.target.value) || 1)} onBlur={() => { void persist(); }} /><span className="hint" style={{ marginLeft: 6 }}>{t('settings.general.poolHint')}</span></div>
                    </div>
                  </GenCard>

                  <GenCard title={t('settings.general.cardService')}>
                    <div className="p-b">
                      <div className="set-row inline"><span className="sr-l">{t('settings.general.dataDir')}</span><span className="mono gen-kv" title={settings?.runtime?.data_dir ?? ''}>{settings?.runtime?.data_dir ?? '—'}</span></div>
                      <div className="set-row inline"><span className="sr-l">{t('settings.general.port')}</span><span className="mono gen-kv">{settings?.runtime?.port ?? '—'}</span></div>
                      <div className="set-row inline"><span className="sr-l">{t('settings.general.auth')}</span><span className="mono gen-kv">{settings?.runtime?.auth ?? '—'}</span></div>
                      <div className="hint" style={{ marginTop: 8 }}>{t('settings.general.serviceHint')}</div>
                    </div>
                  </GenCard>
                </div>
              </section>
            )}
          </div>
        </div>
      </aside>

      {privacyConfirm && (
        <div className="modal-mask open" onClick={(e) => { if (e.target === e.currentTarget) setPrivacyConfirm(null) }}>
          <div className="modal confirm" style={{ width: 440 }}>
            <div className="mh">
              <span className="t">⚠ {t('settings.privacyCards.confirmTitle')}</span>
              <CloseBtn className="close" title={t('common.close')} onClick={() => setPrivacyConfirm(null)} />
            </div>
            <div className="mb">
              <div className="del-text">
                {t(`settings.privacyCards.${privacyConfirm.to}.confirm`)}
              </div>
            </div>
            <div className="mf">
              <button className="btn" onClick={() => setPrivacyConfirm(null)}>{t('common.cancel')}</button>
              <button className="btn save" onClick={() => { const to = privacyConfirm.to; setPrivacyConfirm(null); void persistPrivacy(to) }}>
                {t('settings.privacyCards.confirmYes', { mode: t(`settings.privacyCards.${privacyConfirm.to}.title`) })}
              </button>
            </div>
          </div>
        </div>
      )}

      {embSwitchAsk && (
        <div className="modal-mask open" onClick={(e) => { if (e.target === e.currentTarget) setEmbSwitchAsk(null) }}>
          <div className="modal confirm" style={{ width: 400 }}>
            <div className="mh">
              <span className="t">{t('settings.embSwitch.title')}</span>
              <CloseBtn className="close" title={t('common.close')} onClick={() => setEmbSwitchAsk(null)} />
            </div>
            <div className="mb">
              <div className="del-text">{t('settings.embSwitch.body')}</div>
            </div>
            <div className="mf">
              <button className="btn" onClick={() => setEmbSwitchAsk(null)}>{t('common.cancel')}</button>
              <button className="btn" onClick={embSwitchLater}>{t('settings.embSwitch.no')}</button>
              <button className="btn save" onClick={embSwitchRebuild}>{t('settings.embSwitch.yes')}</button>
            </div>
          </div>
        </div>
      )}
      {rmTarget && (
        <div className="modal-mask open" onClick={(e) => { if (e.target === e.currentTarget) setRmTarget(null) }}>
          <div className="modal confirm" style={{ width: 400 }}>
            <div className="mh">
              <span className="t">⚠ {t('settings.del.title')}</span>
              <CloseBtn className="close" title={t('common.close')} onClick={() => setRmTarget(null)} />
            </div>
            <div className="mb">
              <div className="del-text">
                {rmTarget.kind === 'conn'
                  ? t('settings.del.conn', { name: rmTarget.label })
                  : t(rmTarget.kind === 'chat' ? 'settings.del.modelChat' : 'settings.del.modelEmb', { name: rmTarget.label })}
              </div>
              {rmTarget.kind === 'conn'
                ? rmTarget.id === defaultId && <div className="del-note">{t('settings.del.connDefault')}</div>
                : rmTarget.id === (rmTarget.kind === 'chat' ? defaultAi : defaultEmb) && <div className="del-note">{t('settings.del.modelDefault')}</div>}
            </div>
            <div className="mf">
              <button className="btn" onClick={() => setRmTarget(null)}>{t('common.cancel')}</button>
              <button className="btn danger" onClick={doRemove}>{t('common.delete')}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
