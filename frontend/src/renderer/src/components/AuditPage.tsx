import { useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useI18n } from '@renderer/store/i18n'
import ListPane from './audit/ListPane'
import DetailPane from './audit/DetailPane'
import ReportDrawer from './audit/ReportDrawer'
import RulesDrawer from './audit/RulesDrawer'
import type { Sel } from './audit/EntryList'
import { useAuditSignal } from '@renderer/store/auditSignal'

/** 安全与审计 v2：一行结论 + 左(统计/列表)右(今日/详情) 主从。 */
export function AuditPage(): React.JSX.Element {
  const currentId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '—')
  const { t } = useI18n()
  const unread = useAuditSignal((s) => s.unread)
  const pending = useAuditSignal((s) => s.pending)
  const todayBlocked = useAuditSignal((s) => s.todayBlocked)
  const [drawer, setDrawer] = useState<'none' | 'report' | 'rules'>('none')
  const [selected, setSelected] = useState<Sel>(null)

  if (!currentId) return <div className="mpage"><div className="mpage-empty">{t('audit.noConnection')}</div></div>

  const calm = !unread && !pending && !todayBlocked
  return (
    <div className="review kb-page audit-page au2">
      <div className="concl-bar">
        {calm
          ? <span className="cb-verdict ok">✓ {t('audit.allClearToday')}</span>
          : <span className="cb-verdict warn">
              ⚠ {t('audit.todayLine', { blocked: todayBlocked, pending })}
              {unread > 0 && <span className="cb-unread">{t('audit.unreadN', { n: unread })}</span>}
            </span>}
        <span className="cb-sub mono">{connName}</span>
        <span className="spacer" />
        <button className="rs-btn" onClick={() => setDrawer('report')}>{t('audit.reportLink')}</button>
        <button className="rs-btn" onClick={() => setDrawer('rules')}>{t('audit.rulesLink')}</button>
      </div>
      <div className="au-cols2">
        <ListPane selected={selected} onSelect={setSelected} />
        <DetailPane selected={selected} />
      </div>
      {drawer === 'report' && <ReportDrawer onClose={() => setDrawer('none')} />}
      {drawer === 'rules' && <RulesDrawer onClose={() => setDrawer('none')} />}
    </div>
  )
}
