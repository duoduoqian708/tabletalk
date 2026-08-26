import { useEffect, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { auditStats, type StatBucket } from '@renderer/api/audit'
import EntryList, { type Sel } from './EntryList'
import { useI18n } from '@renderer/store/i18n'

type Scope = 'today' | '7d' | '30d'

/** 左列：三档统计 SVG 柱图 + 关键数 + 审计流水列表。 */
export default function ListPane(props: {
  selected: Sel
  onSelect: (s: Sel) => void
}): React.JSX.Element {
  const connId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '')
  const { t } = useI18n()
  const [scope, setScope] = useState<Scope>('today')
  const [buckets, setBuckets] = useState<StatBucket[]>([])

  useEffect(() => {
    if (!connId) return
    let alive = true
    void auditStats(scope, connName).then((r) => { if (alive) setBuckets(r.buckets) }).catch(() => undefined)
    return () => { alive = false }
  }, [connId, connName, scope])

  const totals = buckets.reduce((a, b) => ({ allow: a.allow + b.allow, review: a.review + b.review, block: a.block + b.block }), { allow: 0, review: 0, block: 0 })
  const max = Math.max(1, ...buckets.map((b) => b.total))

  return (
    <div className="au-left-pane">
      <div className="sec-h"><span>{t('audit.statsTitle')}</span>
        <span className="tier-switch">
          {(['today', '7d', '30d'] as Scope[]).map((s) => (
            <button key={s} className={`tier-b${scope === s ? ' on' : ''}`} onClick={() => setScope(s)}>
              {t(`audit.scope.${s}`)}
            </button>
          ))}
        </span>
      </div>
      <div className="stat-block">
        <div className="stat-bars">
          {buckets.map((b) => (
            <div key={b.bucket} className="stat-col" title={`${b.bucket}: ${b.total}`}>
              <div className="stat-bar main" style={{ height: `${Math.round((b.total / max) * 100)}%` }} />
              {b.block > 0 && <div className="stat-bar b" style={{ height: `${Math.max(8, Math.round((b.block / max) * 100))}%` }} />}
            </div>
          ))}
        </div>
        <div className="knums">
          <span className="knum"><b>{totals.allow}</b>{t('audit.kAllow')}</span>
          <span className="knum"><b className="c-red">{totals.block}</b>{t('audit.kBlock')}</span>
          <span className="knum"><b className="c-amber">{totals.review}</b>{t('audit.kReview')}</span>
        </div>
      </div>
      <EntryList selected={props.selected} onSelect={props.onSelect} />
    </div>
  )
}
