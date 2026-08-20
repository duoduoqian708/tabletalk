import { useCallback, useEffect, useState } from 'react'
import { createSkill, listSkills, removeSkill, updateSkill, type SkillCatalog, type SkillPublic } from '@renderer/api/skills'
import { useI18n } from '@renderer/store/i18n'
import { toastMsg } from '@renderer/utils/toast'

const TOOL_HINT: Record<string, string> = {
  get_schema: 'skill.tool.get_schema',
  describe_table: 'skill.tool.describe_table',
  run_query: 'skill.tool.run_query',
  run_dml: 'skill.tool.run_dml',
  draft_ddl: 'skill.tool.draft_ddl',
}

/** 技能广场：技能清单 + 启用/禁用 + 工具组合 + 自建简单技能。 */
export function SkillPlaza(): React.JSX.Element {
  const { t } = useI18n()
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
      toastMsg(t('skill.loadFail', { msg: (e as Error).message }))
    }
  }, [t])

  useEffect(() => {
    void reload()
  }, [reload])

  async function patch(id: string, p: Partial<Omit<SkillPublic, 'id' | 'builtin'>>): Promise<void> {
    setBusy(true)
    try {
      await updateSkill(id, p)
      await reload()
    } catch (e) {
      toastMsg(t('skill.saveFail', { msg: (e as Error).message }))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string, name_: string): Promise<void> {
    if (!window.confirm(t('skill.confirmDelete', { name: name_ }))) return
    setBusy(true)
    try {
      await removeSkill(id)
      await reload()
    } catch (e) {
      toastMsg(t('skill.deleteFail', { msg: (e as Error).message }))
    } finally {
      setBusy(false)
    }
  }

  async function submit(): Promise<void> {
    if (!name.trim()) {
      setError('skill.nameEmpty')
      return
    }
    if (tools.length === 0) {
      setError('skill.needTool')
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
      toastMsg(t('skill.created'))
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
      <div className="sec-h">{t('skill.title')}<span className="sec-s mono">{t('skill.subtitle')}</span></div>
      <div className="sec-d">
        {t('skill.descLong')}
      </div>

      {skillList.map((s) => (
        <div key={s.id} className={`sp-item${openId === s.id ? ' open' : ''}`}>
          <div className="sp-head">
            <span className="sp-name">{s.name}</span>
            {s.builtin ? <span className="sp-badge b">{t('skill.builtin')}</span> : <span className="sp-badge c">{t('skill.custom')}</span>}
            {s.read_only ? <span className="sp-badge ro">{t('skill.readOnly')}</span> : <span className="sp-badge rw">{t('skill.readWrite')}</span>}
            {!s.enabled && <span className="sp-badge off">{t('skill.disabled')}</span>}
            <span className="sp-desc">{s.description}</span>
            <span className="spacer" />
            <label className="sp-toggle" title={s.enabled ? t('skill.disableTitle') : t('skill.enableTitle')}>
              <input
                type="checkbox"
                checked={s.enabled}
                disabled={busy}
                onChange={(e) => void patch(s.id, { enabled: e.target.checked })}
              />
              <span className="tg-track"><span className="tg-knob" /></span>
            </label>
            <button className="sp-more" onClick={() => setOpenId(openId === s.id ? null : s.id)}>
              {openId === s.id ? `${t('skill.collapse')} ▴` : `${t('skill.expand')} ▾`}
            </button>
            {!s.builtin && (
              <button className="sp-del" disabled={busy} onClick={() => void remove(s.id, s.name)}>{t('common.delete')}</button>
            )}
          </div>
          {openId === s.id && (
            <div className="sp-body">
              <div className="sp-row">
                <span className="sp-label">{t('skill.toolCombo')}</span>
                <div className="sp-tools">
                  {toolNames.map((tool) => (
                    <label key={tool.name} className={`sp-tool${s.tools.includes(tool.name) ? ' on' : ''}`}>
                      <input
                        type="checkbox"
                        checked={s.tools.includes(tool.name)}
                        disabled={busy}
                        onChange={(e) => {
                          const next = e.target.checked
                            ? [...s.tools, tool.name]
                            : s.tools.filter((x) => x !== tool.name)
                          void patch(s.id, { tools: next })
                        }}
                      />
                      <span className="sp-tool-n mono">{tool.name}</span>
                      <span className="sp-tool-d">{TOOL_HINT[tool.name] ? t(TOOL_HINT[tool.name]) : tool.description}</span>
                    </label>
                  ))}
                </div>
              </div>
              {s.triggers.length > 0 && (
                <div className="sp-row">
                  <span className="sp-label">{t('skill.triggers')}</span>
                  <span className="sp-trig mono">{s.triggers.join(' · ')}</span>
                </div>
              )}
              {s.system_prompt && (
                <div className="sp-row">
                  <span className="sp-label">{t('skill.prompt')}</span>
                  <pre className="sp-prompt">{s.system_prompt}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      ))}

      {!creating ? (
        <button className="sp-create" onClick={() => setCreating(true)}>＋ {t('skill.createSimple')}</button>
      ) : (
        <div className="sp-form">
          <div className="sp-form-title">{t('skill.createTitle')}</div>
          <div className="sp-f-row">
            <label>{t('skill.name')}</label>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t('skill.namePlaceholder')} />
          </div>
          <div className="sp-f-row">
            <label>{t('skill.description')}</label>
            <input value={desc} onChange={(e) => setDesc(e.target.value)} placeholder={t('skill.descPlaceholder')} />
          </div>
          <div className="sp-f-row">
            <label>{t('skill.triggers')}</label>
            <input value={triggers} onChange={(e) => setTriggers(e.target.value)} placeholder={t('skill.triggersPlaceholder')} />
          </div>
          <div className="sp-f-row">
            <label>{t('skill.prompt')}</label>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={3}
              placeholder={t('skill.promptPlaceholder')}
            />
          </div>
          <div className="sp-f-row">
            <label>{t('skill.tools')}</label>
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
            <label>{t('skill.readOnly')}</label>
            <label className="sp-toggle">
              <input type="checkbox" checked={readOnly} onChange={(e) => setReadOnly(e.target.checked)} />
              <span className="tg-track"><span className="tg-knob" /></span>
            </label>
            <span className="sp-hint">{t('skill.readOnlyHint')}</span>
          </div>
          {error && <div className="sp-error">{t(error)}</div>}
          <div className="sp-f-acts">
            <button className="gi-btn pri" disabled={busy} onClick={() => void submit()}>{t('skill.create')}</button>
            <button className="gi-btn" onClick={() => { setCreating(false); setError('') }}>{t('common.cancel')}</button>
          </div>
        </div>
      )}
    </div>
  )
}
