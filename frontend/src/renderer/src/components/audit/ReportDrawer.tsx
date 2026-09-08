import { useEffect, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { auditEgress, auditWeekly } from '@renderer/api/audit'
import { fmtDT } from '@renderer/lib/timefmt'
import { useI18n } from '@renderer/store/i18n'
import { CloseBtn } from '../ui/buttons'

export default function ReportDrawer(props: { onClose: () => void }): React.JSX.Element {
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '')
  const { t } = useI18n()
  const [egress, setEgress] = useState<Awaited<ReturnType<typeof auditEgress>> | null>(null)
  const [weekly, setWeekly] = useState<Awaited<ReturnType<typeof auditWeekly>> | null>(null)
  useEffect(() => {
    void auditEgress(connName).then(setEgress).catch(() => undefined)
    void auditWeekly(connName).then(setWeekly).catch(() => undefined)
  }, [connName])
  return (
    <div className="drawer-mask" onClick={props.onClose}>
      <div className="drawer-panel" onClick={(e) => e.stopPropagation()}>
        <div className="sec-h"><span>{t('audit.tabEgress')}</span><CloseBtn className="au-ltab" title={t('common.close')} onClick={props.onClose} /></div>
        <div className="mono" style={{ fontSize: 12, padding: '8px 0' }}>
          {t('audit.egressTotal', { n: egress?.total ?? 0, byModel: Object.entries(egress?.by_model ?? {}).map(([k, v]) => `${k}:${v}`).join(' '), byMode: Object.entries(egress?.by_mode ?? {}).map(([k, v]) => `${k}:${v}`).join(' ') })}
        </div>
        <div className="sec-h"><span>{t('audit.tabWeekly')}</span></div>
        <div className="mono" style={{ fontSize: 12 }}>
          {Object.entries(weekly?.weekly ?? {}).map(([w, c]) => <span key={w} style={{ marginRight: 8 }}>{w}:{c}</span>)}
        </div>
        <div className="sec-h"><span>{t('audit.anomalyNightBatch')}</span></div>
        {(weekly?.anomalies ?? []).map((a, i) => <div key={i} className="mono" style={{ fontSize: 11, padding: '2px 0' }}>{fmtDT(a.ts)} · {a.sql}</div>)}
      </div>
    </div>
  )
}
