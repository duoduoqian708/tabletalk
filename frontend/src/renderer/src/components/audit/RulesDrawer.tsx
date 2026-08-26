import { useI18n } from '@renderer/store/i18n'
import { VerdictBadge } from '../VerdictBadge'

export default function RulesDrawer(props: { onClose: () => void }): React.JSX.Element {
  const { t } = useI18n()
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
          <table className="rule-table">
            <thead><tr><th style={{ width: 210 }}>{t('audit.colRule')}</th><th style={{ width: 90 }}>{t('audit.colScope')}</th><th style={{ width: 80 }}>{t('audit.colVerdict')}</th><th>{t('audit.colDesc')}</th></tr></thead>
            <tbody>
              <tr><td>{t('gate.ruleReadRow')}</td><td>SELECT</td><td><VerdictBadge v="allow" /></td><td>{t('gate.ruleReadDesc')}</td></tr>
              <tr><td>{t('gate.ruleNoWhereRow')}</td><td>UPDATE / DELETE</td><td><VerdictBadge v="block" /></td><td>{t('gate.ruleNoWhereDesc')}</td></tr>
              <tr><td>{t('gate.ruleReviewRow')}</td><td>INSERT / UPDATE / DELETE</td><td><VerdictBadge v="review" /></td><td>{t('gate.ruleReviewDesc')}</td></tr>
              <tr><td>{t('gate.ruleDdlRow')}</td><td>CREATE / ALTER / DROP / TRUNCATE</td><td><span className="badge manual">MANUAL</span></td><td>{t('gate.ruleDdlDesc')}</td></tr>
              <tr><td>{t('gate.ruleParseFailRow')}</td><td>{t('audit.anySql')}</td><td><VerdictBadge v="review" /></td><td>{t('gate.ruleParseFailDesc')}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
