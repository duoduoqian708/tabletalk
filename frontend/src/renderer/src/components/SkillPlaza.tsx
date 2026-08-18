import { useCallback, useEffect, useState } from 'react'
import { createSkill, listSkills, removeSkill, updateSkill, type SkillCatalog, type SkillPublic } from '@renderer/api/skills'
import { toastMsg } from '@renderer/utils/toast'

const TOOL_HINT: Record<string, string> = {
  get_schema: '表结构摘要（只读）',
  describe_table: '单表列定义（只读）',
  run_query: '执行只读查询（过闸门）',
  run_dml: '写操作（必须人工确认，闸门兜底）',
  draft_ddl: 'DDL 草稿（永不执行）'
}

/** 技能广场：技能清单 + 启用/禁用 + 工具组合 + 自建简单技能。 */
export function SkillPlaza(): React.JSX.Element {
  const [cat, setCat] = useState<SkillCatalog | null>(null)
  const [busy, setBusy] = useState(false)
  const [openId, setOpenId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  // 新建表单
  const [name, setName] = useState('')
  const [desc, setDesc] = useState('')
  const [prompt, setPrompt] = useState('')
  const [triggers, setTriggers] = useState('')
  const [tools, setTools] = useState<string[]>(['run_query', 'get_schema'])
  const [readOnly, setReadOnly] = useState(true)
  const [error, setError] = useState('')

  const reload = useCallback(async () => {
    try {
      setCat(await listSkills())
    } catch (e) {
      toastMsg(`技能列表加载失败：${(e as Error).message}`)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  async function patch(id: string, p: Partial<Omit<SkillPublic, 'id' | 'builtin'>>): Promise<void> {
    setBusy(true)
    try {
      await updateSkill(id, p)
      await reload()
    } catch (e) {
      toastMsg(`保存失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string, name_: string): Promise<void> {
    if (!window.confirm(`删除自定义技能「${name_}」？`)) return
    setBusy(true)
    try {
      await removeSkill(id)
      await reload()
    } catch (e) {
      toastMsg(`删除失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  async function submit(): Promise<void> {
    if (!name.trim()) {
      setError('技能名称不能为空')
      return
    }
    if (tools.length === 0) {
      setError('至少勾选一个工具')
      return
    }
    setBusy(true)
    setError('')
    try {
      await createSkill({
        name: name.trim(),
        description: desc.trim(),
        system_prompt: prompt.trim(),
        tools,
        read_only: readOnly,
        enabled: true,
        triggers: triggers.split(/[,，]/).map((t) => t.trim()).filter(Boolean)
      })
      setName('')
      setDesc('')
      setPrompt('')
      setTriggers('')
      setTools(['run_query', 'get_schema'])
      setReadOnly(true)
      setCreating(false)
      await reload()
      toastMsg('技能已创建')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  function toggleTool(list: string[], t: string): string[] {
    return list.includes(t) ? list.filter((x) => x !== t) : [...list, t]
  }

  const toolNames = cat?.tools ?? []
  const skillList = cat?.skills ?? []

  return (
    <div className="skill-plaza">
      <div className="sec-h">技能广场<span className="sec-s mono">工具 × 技能 · 任意组合</span></div>
      <div className="sec-d">
        技能 = 编排原子工具的剧本。工具是积木，技能是玩法——组合只会收窄工具集，每个工具执行仍过统一安全闸门（写操作永远人工确认，AI 没有 DDL 执行工具）。
      </div>

      {skillList.map((s) => (
        <div key={s.id} className={`sp-item${openId === s.id ? ' open' : ''}`}>
          <div className="sp-head">
            <span className="sp-name">{s.name}</span>
            {s.builtin ? <span className="sp-badge b">内置</span> : <span className="sp-badge c">自定义</span>}
            {s.read_only ? <span className="sp-badge ro">只读</span> : <span className="sp-badge rw">读写</span>}
            {!s.enabled && <span className="sp-badge off">已禁用</span>}
            <span className="sp-desc">{s.description}</span>
            <span className="spacer" />
            <label className="sp-toggle" title={s.enabled ? '点击禁用' : '点击启用'}>
              <input
                type="checkbox"
                checked={s.enabled}
                disabled={busy}
                onChange={(e) => void patch(s.id, { enabled: e.target.checked })}
              />
              <span className="tg-track"><span className="tg-knob" /></span>
            </label>
            <button className="sp-more" onClick={() => setOpenId(openId === s.id ? null : s.id)}>
              {openId === s.id ? '收起 ▴' : '组合 ▾'}
            </button>
            {!s.builtin && (
              <button className="sp-del" disabled={busy} onClick={() => void remove(s.id, s.name)}>删除</button>
            )}
          </div>
          {openId === s.id && (
            <div className="sp-body">
              <div className="sp-row">
                <span className="sp-label">工具组合</span>
                <div className="sp-tools">
                  {toolNames.map((t) => (
                    <label key={t.name} className={`sp-tool${s.tools.includes(t.name) ? ' on' : ''}`}>
                      <input
                        type="checkbox"
                        checked={s.tools.includes(t.name)}
                        disabled={busy}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...s.tools, t.name]
                            : s.tools.filter((x) => x !== t.name)
                          void patch(s.id, { tools: next })
                        }}
                      />
                      <span className="sp-tool-n mono">{t.name}</span>
                      <span className="sp-tool-d">{TOOL_HINT[t.name] ?? t.description}</span>
                    </label>
                  ))}
                </div>
              </div>
              {s.triggers.length > 0 && (
                <div className="sp-row">
                  <span className="sp-label">触发词</span>
                  <span className="sp-trig mono">{s.triggers.join(' · ')}</span>
                </div>
              )}
              {s.system_prompt && (
                <div className="sp-row">
                  <span className="sp-label">提示词</span>
                  <pre className="sp-prompt">{s.system_prompt}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      ))}

      {!creating ? (
        <button className="sp-create" onClick={() => setCreating(true)}>＋ 自建简单技能</button>
      ) : (
        <div className="sp-form">
          <div className="sp-form-title">自建技能</div>
          <div className="sp-f-row">
            <label>名称</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：对账助手" />
          </div>
          <div className="sp-f-row">
            <label>描述</label>
            <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="技能能做什么（路由时给模型看）" />
          </div>
          <div className="sp-f-row">
            <label>触发词</label>
            <input value={triggers} onChange={(e) => setTriggers(e.target.value)} placeholder="逗号分隔，如：对账,回款" />
          </div>
          <div className="sp-f-row">
            <label>提示词</label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={3}
              placeholder="给模型的行为指导（可留空）"
            />
          </div>
          <div className="sp-f-row">
            <label>工具</label>
            <div className="sp-tools">
              {toolNames.map((t) => (
                <label key={t.name} className={`sp-tool${tools.includes(t.name) ? ' on' : ''}`}>
                  <input
                    type="checkbox"
                    checked={tools.includes(t.name)}
                    onChange={() => setTools((prev) => toggleTool(prev, t.name))}
                  />
                  <span className="sp-tool-n mono">{t.name}</span>
                </label>
              ))}
            </div>
          </div>
          <div className="sp-f-row">
            <label>只读</label>
            <label className="sp-toggle">
              <input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} />
              <span className="tg-track"><span className="tg-knob" /></span>
            </label>
            <span className="sp-hint">只读技能不能勾选写工具（后端强制校验）</span>
          </div>
          {error && <div className="sp-error">{error}</div>}
          <div className="sp-f-acts">
            <button className="gi-btn pri" disabled={busy} onClick={() => void submit()}>创建</button>
            <button className="gi-btn" onClick={() => { setCreating(false); setError('') }}>取消</button>
          </div>
        </div>
      )}
    </div>
  )
}
