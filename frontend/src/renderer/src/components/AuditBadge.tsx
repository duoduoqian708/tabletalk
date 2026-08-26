import { useAuditSignal } from '@renderer/store/auditSignal'
import { useUi } from '@renderer/store/ui'
import { useI18n } from '@renderer/store/i18n'

/** 状态栏安全徽章：无事显示平安，有事显示可点击计数（跳审计页）。 */
export function AuditBadge(): React.JSX.Element {
  const { t } = useI18n()
  const unread = useAuditSignal((s) => s.unread)
  const pending = useAuditSignal((s) => s.pending)
  const setView = useUi((s) => s.setView)
  if (!unread && !pending) {
    return <span className="sig-pill ok">✓ {t('audit.allClear')}</span>
  }
  return (
    <span className="sig-group">
      {unread > 0 && (
        <button className="sig-pill danger" onClick={() => setView('audit')}>
          ● {t('audit.unreadN', { n: unread })}
        </button>
      )}
      {pending > 0 && (
        <button className="sig-pill warn" onClick={() => setView('audit')}>
          ◔ {t('audit.pendingN', { n: pending })}
        </button>
      )}
    </span>
  )
}
