import { useEffect, useState } from 'react'
import { useI18n } from '@renderer/store/i18n'
import { getSafetyRules, putGateRules, type SafetyRule } from '@renderer/api/safety'

const LADDER: Record<string, number> = { allow: 0, review: 1, block: 2 }
const VERDICTS = ['allow', 'review', 'block'] as const

/** 闸门说明 + 规则目录（动态，来自 GET /safety/rules）。非硬规则可调覆盖——只提供"更严"档。 */
export default function RulesDrawer(props: { onClose: () => void }): React.JSX.Element {
  const { t } = useI18n()
  const [rules, setRules] = useState<SafetyRule[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void getSafetyRules().then((r) => setRules(r.rules)).catch(() => undefined)
  }, [])

  async function setOverride(id: string, val: string): Promise<void> {
    const updated = rules.map((r) => (r.id === id ? { ...r, override: val === r.default_verdict ? null : val } : r))
    setRules(updated)
    // 只提交实际收严项（默认值省略）
    const map: Record<string, string> = {}
    for (const r of updated) if (r.override && r.override !== r.default_verdict) map[r.id] = r.override
    setBusy(true)
    try {
      const res = await putGateRules(map)
      if (res.gate_rules) {
        setRules((prev) => prev.map((r) => ({ ...r, override: res.gate_rules?.[r.id] ?? null })))
      }
    } catch {
      // 失败保持乐观值；后端 normalize 契约会在下次打开抽屉时校正
    } finally { setBusy(false) }
  }

  function optionsFor(r: SafetyRule): string[] {
    const base = LADDER[r.default_verdict] ?? 0
    return [r.default_verdict, ...VERDICTS.filter((v) => (LADDER[v] ?? 0) > base)]
  }

  return (
    <div className="drawer-mask" onClick={props.onClose}>
      <div className="drawer-panel" onClick={(e) => e.stopPropagation()}>
        <div className="sec-h"><span>{t('gate.ruleTitle')}</span><button className="au-ltab" onClick={props.onClose}>✕</button></div>
        <div className="tiers">
          <div className="tier read">
            <div className="t-h"><div className="t-ic">⌕</div><div className="t-t">{t('gate.tierRead')}</div><div className="t-st">{t('verdict.allow')}</div></div>
            <div className="t-why">{t('gate.tierReadWhy')}</div>
            <div className="t-kw"><span>SELECT</span><span>SHOW</span><span>EXPLAIN</span><span>PRAGMA</span></div>
            <div className="t-d">{t('gate.tierReadDesc')}</div>
            <div className="t-rule">{t('gate.ruleAutoLimit')}</div>
          </div>
          <div className="tier write">
            <div className="t-h"><div className="t-ic">✎</div><div className="t-t">{t('gate.tierWrite')}</div><div className="t-st">{t('verdict.review')}</div></div>
            <div className="t-why">WRITE · GATE REQUIRED</div>
            <div className="t-kw"><span>INSERT</span><span>UPDATE</span><span>DELETE</span></div>
            <div className="t-d">{t('gate.tierWriteDesc')}</div>
            <div className="t-rule">{t('gate.ruleNoWhere')}</div>
          </div>
          <div className="tier ddl">
            <div className="t-h"><div className="t-ic">▧</div><div className="t-t">{t('gate.tierDdl')}</div><div className="t-st">{t('gate.tierDdlManual')}</div></div>
            <div className="t-why">MANUAL ONLY · AI BLOCKED</div>
            <div className="t-kw"><span>CREATE</span><span>ALTER</span><span>DROP</span><span>TRUNCATE</span></div>
            <div className="t-d">{t('gate.tierDdlDesc')}</div>
            <div className="t-rule">{t('gate.ruleDdlDraft')}</div>
          </div>
        </div>
        <div className="panel">
          <div className="p-h">{t('gate.ruleTitle')}<span className="p-s">{t('audit.rulePanelSub')}</span></div>
          <div className="p-h hint mono" style={{ fontSize: 11, fontWeight: 400 }}>{t('gate.configHint')}</div>
          <table className="rule-table">
            <thead><tr>
              <th style={{ width: 210 }}>{t('audit.colRule')}</th>
              <th>{t('audit.colDesc')}</th>
              <th style={{ width: 70 }}>{t('audit.colVerdict')}</th>
            </tr></thead>
            <tbody>
              {rules.map((r) => {
                const lockedHint = r.floor ? ` 🔒 ${t('gate.floorLock')}` : ''
                return (
                  <tr key={r.id}>
                    <td><span className="mono">{r.id}</span><span className="mono" style={{ color: 'var(--ink-faint)', fontSize: 11 }}>{lockedHint}</span></td>
                    <td>{t(`gate.rule.${r.id}`)}</td>
                    <td>
                      <select
                        className="ru-sel mono"
                        value={r.override ?? r.default_verdict}
                        disabled={busy}
                        onChange={(e) => void setOverride(r.id, e.target.value)}
                        title={t(`gate.rule.${r.id}`)}
                      >
                        {optionsFor(r).map((v) => (
                          <option key={v} value={v}>
                            {v === r.default_verdict ? `${t('gate.verdictDefault')} · ${t(`verdict.${v}`)}` : t(`verdict.${v}`)}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}