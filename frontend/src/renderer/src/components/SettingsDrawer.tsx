import { useEffect, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { testGateway, testEmbedding } from '@renderer/api/ai'
import { getSettings, updateSettings } from '@renderer/api/settings'
import type { AiModelConfig, EmbeddingModelConfig, SettingsPublic, SettingsPatch } from '@renderer/api/settings'

interface Props {
  open: boolean
  onClose: () => void
  onNewConnection: () => void
}

const SECTIONS = [
  { key: 'dsm', ic: '⛁', label: '数据源管理' },
  { key: 'theme', ic: '◐', label: '主题风格' },
  { key: 'llm', ic: '◎', label: '大模型接入' },
  { key: 'safety', ic: '▣', label: '安全参数' },
  { key: 'general', ic: '⚙', label: '通用' }
] as const

type Sec = (typeof SECTIONS)[number]['key']
type ModelTab = 'chat' | 'embedding'

function uid(prefix: string): string {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`
}

// 后端对已保存的 api_key 返回脱敏掩码（sk-abc•••xyz）——含 ••• 即表示"有存量 key，前端拿不到真值"
function isMaskedKey(k: unknown): boolean {
  return typeof k === 'string' && k.includes('•••')
}

const CAP_LABELS: Record<string, { label: string; cls: string }> = {
  connectivity: { label: '连通', cls: 'ok' },
  function_calling: { label: 'FC', cls: 'fc' },
  reasoning: { label: '推理', cls: 'reasoning' },
  streaming: { label: '流', cls: 'ok' },
}

function CapBadges({ caps }: { caps?: { connectivity?: boolean; function_calling?: boolean; reasoning?: boolean | null; streaming?: boolean; context_window?: number | null; latency_ms?: number } }): React.JSX.Element {
  if (!caps) return <span className="cap-badge no">未测试</span>
  const items: { key: string; ok: boolean | null | undefined; label: string; cls: string }[] = []
  for (const [k, v] of Object.entries(CAP_LABELS)) {
    const val = caps[k as keyof typeof caps]
    items.push({ key: k, ok: val as boolean | null | undefined, label: v.label, cls: v.cls })
  }
  return (
    <div className="mi-badges">
      {items.map((it) => (
        <span key={it.key} className={`cap-badge ${it.ok ? it.cls : 'no'}`} title={it.key}>
          {it.ok ? it.label : (it.ok == null && it.key === 'reasoning' ? '未探测' : `无${it.label}`)}
        </span>
      ))}
      {caps.context_window && (
        <span className="cap-badge" title="上下文窗口">{Math.round(caps.context_window / 1000)}K</span>
      )}
      {caps.latency_ms !== undefined && (
        <span className="cap-badge">{caps.latency_ms}ms</span>
      )}
    </div>
  )
}

export function SettingsDrawer({ open, onClose, onNewConnection }: Props): React.JSX.Element | null {
  const [sec, setSec] = useState<Sec>('dsm')
  const { list, currentId, select, remove } = useConnections()
  const [savedMsg, setSavedMsg] = useState('')

  // 平台级运行参数（安全闸门 / 通用）
  const [settings, setSettings] = useState<SettingsPublic | null>(null)
  const [maxRows, setMaxRows] = useState<number>(1000)
  const [poolSize, setPoolSize] = useState<number>(3)
  const [runtime, setRuntime] = useState<{ data_dir: string; port: number; auth: string } | null>(null)
  const [loading, setLoading] = useState(false)

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
          if (caps?.connectivity) capTexts.push('连通')
          if (caps?.function_calling) capTexts.push('FC')
          if (caps?.reasoning) capTexts.push('推理')
          if (caps?.streaming) capTexts.push('流式')
          if (caps?.context_window) capTexts.push(`${Math.round(caps.context_window / 1000)}K`)
          setTestMsg(`✓ 连接成功 · ${r.latency_ms ?? '?'}ms${capTexts.length ? ` · ${capTexts.join(' / ')}` : ''}`)
        } else {
          setTestMsg(`✗ 测试失败：${r.error ?? '未知错误'}`)
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
          setTestMsg(`✓ 连接成功 · ${r.dimensions} 维 · ${r.latency_ms ?? '?'}ms`)
        } else {
          setTestMsg(`✗ 测试失败：${r.error ?? '未知错误'}`)
        }
      }
    } catch (e) {
      setTestMsg(`✗ 测试失败：${(e as Error).message}`)
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
      setSavedMsg('✓ 已保存')
      return true
    } catch (e) {
      setSavedMsg(`✗ 保存失败：${(e as Error).message}`)
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
    <div className="set-mask" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <aside className="set-drawer">
        <div className="set-head">
          <span className="set-title">系统设置</span>
          <span className="set-sub mono">平台级 · 与数据源无关</span>
          <button className="set-x" onClick={onClose}>✕</button>
        </div>

        <div className="set-body">
          <nav className="set-rail">
            {SECTIONS.map((s) => (
              <button key={s.key} className={`sr-it${sec === s.key ? ' on' : ''}`} onClick={() => setSec(s.key)}>
                <span className="sr-ic">{s.ic}</span>{s.label}
              </button>
            ))}
          </nav>

          <div className="set-content">
            {sec === 'dsm' && (
              <section className="set-sec">
                <div className="sec-h">数据源管理<span className="sec-s mono">连接注册表</span></div>
                <div className="sec-d">平台级连接清单。顶栏「数据源切换」是全局锚，切换后所有模块跟随；这里的连接本身与数据源无关。</div>
                <div className="conn-list">
                  {list.map((c) => (
                    <div key={c.id} className={`conn-row${c.id === currentId ? ' cur' : ''}`}>
                      <span className={`st${c.id === currentId ? ' live' : ' idle'}`} />
                      <span className="cn mono">{c.name}</span>
                      <span className="cd mono">{c.dialect}</span>
                      {c.read_only && <span className="ro-tag mono">只读</span>}
                      <span className="spacer" />
                      <button className="mini-btn set" onClick={() => select(c.id)}>设为当前</button>
                      <button className="mini-btn dang" onClick={() => remove(c.id)}>删除</button>
                    </div>
                  ))}
                  {list.length === 0 && <div className="mpage-empty">暂无连接</div>}
                  <button className="conn-add" onClick={onNewConnection}>＋ 添加连接</button>
                </div>
              </section>
            )}

            {sec === 'theme' && (
              <section className="set-sec">
                <div className="sec-h">主题风格<span className="sec-s mono">视觉方向 · 当前锁定</span></div>
                <div className="sec-d">产品视觉方向当前锁定为北欧极简（唯一开发方向），此处预留多风格切换位。</div>
                <div className="theme-card">
                  <div className="tc-swatch" />
                  <div className="tc-info">
                    <div className="tcn">北欧极简</div>
                    <div className="tcd mono">近白底 · 纯白面板 · 细边框 · 蓝强调 · 大留白</div>
                    <div className="tc-pal">
                      <i style={{ background: '#fbfbfc', border: '1px solid var(--line-strong)' }} />
                      <i style={{ background: '#ffffff', border: '1px solid var(--line-strong)' }} />
                      <i style={{ background: '#0a6dff' }} />
                      <i style={{ background: '#c8871e' }} />
                      <i style={{ background: '#d4453a' }} />
                    </div>
                  </div>
                  <span className="badge allow" style={{ marginLeft: 'auto' }}>已锁定</span>
                </div>
              </section>
            )}

            {sec === 'llm' && (
              <section className="set-sec">
                <div className="sec-h">大模型接入<span className="sec-s mono">文本推理 + 向量嵌入</span></div>
                <div className="sec-d">任意 OpenAI 兼容端点（云端 API / Ollama / vLLM / 私有网关）。隐私：行数据默认不回传模型。</div>

                <div className="model-tabs">
                  <button className={`model-tab${mTab === 'chat' ? ' on' : ''}`} onClick={() => { setMTab('chat'); cancelEdit() }}>
                    文本推理模型
                  </button>
                  <button className={`model-tab${mTab === 'embedding' ? ' on' : ''}`} onClick={() => { setMTab('embedding'); cancelEdit() }}>
                    向量嵌入模型
                  </button>
                </div>

                {mTab === 'chat' && (
                  <>
                    <div className="model-list">
                      {aiModels.map((m) => (
                        <div key={m.id} className={`model-item${m.id === defaultAi ? ' cur' : ''}`}>
                          <div className="mi-head">
                            <span className="mi-name">{m.name}</span>
                            <span className="mi-provider">{m.provider}</span>
                            <span className="mi-model">{m.model || '—'}</span>
                            <div className="mi-actions">
                              <button className={`mini-btn set${m.id === defaultAi ? ' cur' : ''}`} onClick={() => { setDefaultAi(m.id); persist(aiModels, m.id) }}>
                                {m.id === defaultAi ? '当前' : '设为默认'}
                              </button>
                              <button className="mini-btn" onClick={() => startEditChat(m)}>编辑</button>
                              {!m.builtin && aiModels.length > 1 && (
                                <button className="mini-btn dang" onClick={() => removeChatModel(m.id)}>删除</button>
                              )}
                            </div>
                          </div>
                          <div className="mi-cap">
                            <CapBadges caps={m.last_test?.capabilities} />
                          </div>
                        </div>
                      ))}
                      {aiModels.length === 0 && <div className="model-empty">暂无文本模型</div>}
                    </div>

                    {editingChat && (
                      <div className="model-edit">
                        <div className="me-row">
                          <label>名称</label>
                          <input value={editing.name ?? ''} onChange={(e) => setEditing({ ...editing, name: e.target.value })} placeholder="我的模型" />
                        </div>
                        <div className="me-row">
                          <label>Provider</label>
                          <select value={editing.provider ?? 'cloud'} onChange={(e) => setEditing({ ...editing, provider: e.target.value })}>
                            <option value="cloud">云端 API</option>
                            <option value="local">本地 / 私有网关</option>
                            <option value="mock">Mock（内置）</option>
                          </select>
                        </div>
                        {editing.provider !== 'mock' && (
                          <>
                            <div className="me-row">
                              <label>Base URL</label>
                              <input value={editing.base_url ?? ''} onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
                            </div>
                            <div className="me-row">
                              <label>模型 ID</label>
                              <input value={editing.model ?? ''} onChange={(e) => setEditing({ ...editing, model: e.target.value })} placeholder="gpt-4o / claude-sonnet-4.5" />
                            </div>
                            <div className="me-row">
                              <label>API Key</label>
                              <input type="password" value={isMaskedKey(editing.api_key) ? '' : (editing.api_key ?? '')} onChange={(e) => setEditing({ ...editing, api_key: e.target.value })} placeholder={isMaskedKey(editing.api_key) ? `已保存 ${editing.api_key} · 留空则不变` : '粘贴新 key，留空 = 免 key 端点'} />
                            </div>
                          </>
                        )}
                        <div className="me-row">
                          <label>温度</label>
                          <input type="range" className="temp-slider" min="0" max="2" step="0.05" value={(editing as Partial<AiModelConfig>).temperature ?? 0.2} onChange={(e) => setEditing({ ...(editing as Partial<AiModelConfig>), temperature: parseFloat(e.target.value) })} />
                          <span className="temp-val mono">{((editing as Partial<AiModelConfig>).temperature ?? 0.2).toFixed(2)}</span>
                          <span className="hint" style={{ flex: 1 }}>越低越稳定，越高越有创意</span>
                        </div>
                        <div className="me-row">
                          <label>推理思考</label>
                          <span className={`re-status${(editing as Partial<AiModelConfig>).reasoning == null ? '' : ((editing as Partial<AiModelConfig>).reasoning ? ' on' : ' off')}`}>
                            {(editing as Partial<AiModelConfig>).reasoning == null
                              ? '未测试 · 测试连接时自动探测'
                              : ((editing as Partial<AiModelConfig>).reasoning ? '支持 · 回答前会先思考' : '不支持')}
                          </span>
                          <span className="hint" style={{ flex: 1 }}>该模型推理能力由「测试连接」自动探测，只读展示</span>
                        </div>
                        <div className="me-actions">
                          <button className="btn tl" disabled={testing || (editing.provider !== 'mock' && !editing.base_url?.trim())} onClick={() => void handleTest()}>
                            {testing ? '测试中…' : '测试连接'}
                          </button>
                          <span className="spacer" />
                          <button className="mini-btn" onClick={cancelEdit}>取消</button>
                          <button className="btn save" onClick={saveEdit}>{editingChat === 'new' ? '添加' : '保存'}</button>
                        </div>
                        {testMsg && <div className={`me-test${testMsg.startsWith('✓') ? ' ok' : ' bad'}`}>{testMsg}</div>}
                      </div>
                    )}

                    {!editingChat && (
                      <button className="model-add" onClick={startNewChat}>＋ 添加文本模型</button>
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
                                {m.id === defaultEmb ? '当前' : '设为默认'}
                              </button>
                              <button className="mini-btn" onClick={() => startEditEmb(m)}>编辑</button>
                              {embModels.length > 1 && (
                                <button className="mini-btn dang" onClick={() => removeEmbModel(m.id)}>删除</button>
                              )}
                            </div>
                          </div>
                          <div className="mi-cap">
                            <div className="mi-badges">
                              {m.last_test?.ok ? (
                                <span className="cap-badge ok">{m.last_test.dimensions ?? '?'} 维</span>
                              ) : (
                                <span className="cap-badge no">未测试</span>
                              )}
                              {m.last_test?.latency_ms !== undefined && (
                                <span className="cap-badge">{m.last_test.latency_ms}ms</span>
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                      {embModels.length === 0 && <div className="model-empty">暂无嵌入模型</div>}
                    </div>

                    {editingEmb && (
                      <div className="model-edit">
                        <div className="me-row">
                          <label>名称</label>
                          <input value={editing.name ?? ''} onChange={(e) => setEditing({ ...editing, name: e.target.value })} placeholder="我的嵌入模型" />
                        </div>
                        <div className="me-row">
                          <label>Provider</label>
                          <select value={editing.provider ?? 'api'} onChange={(e) => setEditing({ ...editing, provider: e.target.value })}>
                            <option value="api">API（OpenAI 兼容）</option>
                            <option value="hash">Hash（离线）</option>
                          </select>
                        </div>
                        {editing.provider !== 'hash' && (
                          <>
                            <div className="me-row">
                              <label>Base URL</label>
                              <input value={editing.base_url ?? ''} onChange={(e) => setEditing({ ...editing, base_url: e.target.value })} placeholder="https://api.example.com/v1" />
                            </div>
                            <div className="me-row">
                              <label>模型 ID</label>
                              <input value={editing.model ?? ''} onChange={(e) => setEditing({ ...editing, model: e.target.value })} placeholder="text-embedding-3-small / bge-m3" />
                            </div>
                            <div className="me-row">
                              <label>API Key</label>
                              <input type="password" value={isMaskedKey(editing.api_key) ? '' : (editing.api_key ?? '')} onChange={(e) => setEditing({ ...editing, api_key: e.target.value })} placeholder={isMaskedKey(editing.api_key) ? `已保存 ${editing.api_key} · 留空则不变` : '粘贴新 key，留空 = 免 key 端点'} />
                            </div>
                          </>
                        )}
                        <div className="me-actions">
                          <button className="btn tl" disabled={testing || (editing.provider !== 'hash' && !editing.base_url?.trim())} onClick={() => void handleTest()}>
                            {testing ? '测试中…' : '测试连接'}
                          </button>
                          <span className="spacer" />
                          <button className="mini-btn" onClick={cancelEdit}>取消</button>
                          <button className="btn save" onClick={saveEdit}>{editingEmb === 'new' ? '添加' : '保存'}</button>
                        </div>
                        {testMsg && <div className={`me-test${testMsg.startsWith('✓') ? ' ok' : ' bad'}`}>{testMsg}</div>}
                      </div>
                    )}

                    {!editingEmb && (
                      <button className="model-add" onClick={startNewEmb}>＋ 添加嵌入模型</button>
                    )}
                  </>
                )}
              </section>
            )}

            {sec === 'safety' && (
              <section className="set-sec">
                <div className="sec-h">安全参数<span className="sec-s mono">闸门 · 行数上限 · 连接池</span></div>
                <div className="sec-d">安全闸门是平台级本地规则引擎（模型无关，AI 与手动同闸门）；此处配运行参数。</div>
                <div className="panel">
                  <div className="p-h">闸门参数<span className="p-s mono">运行时覆盖</span></div>
                  <div className="p-b">
                    <div className="set-row inline"><span className="sr-l">单次查询最大行数</span><input type="number" min={1} value={maxRows} onChange={(e) => setMaxRows(Number(e.target.value) || 1)} onBlur={() => { void persist(); }} /></div>
                    <div className="set-row inline"><span className="sr-l">连接池大小</span><input type="number" min={1} value={poolSize} onChange={(e) => setPoolSize(Number(e.target.value) || 1)} onBlur={() => { void persist(); }} /><span className="hint" style={{ marginLeft: 6 }}>SQLite 固定 1 · PG/MySQL 默认 3</span></div>
                  </div>
                </div>
              </section>
            )}

            {sec === 'general' && (
              <section className="set-sec">
                <div className="sec-h">通用<span className="sec-s mono">本地 sidecar · 同源托管</span></div>
                <div className="sec-d">本地 AI 优先数据库查询工具（浏览器访问 · 本机 sidecar 同源托管）。</div>
                <div className="panel">
                  <div className="p-h">服务<span className="p-s mono">本机 · 127.0.0.1</span></div>
                  <div className="p-b">
                    {loading || !runtime ? (
                      <div className="sd-hint">加载中…</div>
                    ) : (
                      <>
                        <div className="set-row readonly inline"><span className="sr-l">数据目录</span><input type="text" value={runtime.data_dir} readOnly /></div>
                        <div className="set-row readonly inline"><span className="sr-l">服务端口</span><input type="text" value={String(runtime.port)} readOnly /></div>
                        <div className="set-row readonly inline"><span className="sr-l">鉴权方式</span><input type="text" value={runtime.auth} readOnly /></div>
                        <div className="set-row readonly inline"><span className="sr-l">最大连接数</span><input type="text" value={String(poolSize)} readOnly /></div>
                      </>
                    )}
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
