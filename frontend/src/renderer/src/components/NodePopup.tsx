import { useI18n } from '@renderer/store/i18n'

interface Props {
  table: string
  kind?: 'table' | 'view'
  rowCount: number
  columnCount: number
  fkCount: number
  x: number
  y: number
  onClose: () => void
  onOpenData: () => void
}

export function NodePopup({ table, kind, rowCount, columnCount, fkCount, x, y, onClose, onOpenData }: Props): React.JSX.Element {
  const { t } = useI18n()
  return (
    <div className="node-pop" style={{ left: x, top: y }}>
      <div className="np-head">
        <span className="np-title">{t('node.tableLabel')} <b>{table}</b></span>
        {kind === 'view' && <span className="np-view-badge">view</span>}
        <button className="np-x" onClick={onClose} title={t('common.close')}>✕</button>
      </div>
      <div className="np-stats">
        <div className="np-stat"><span className="np-num">{rowCount.toLocaleString()}</span><span className="np-lab">{t('node.rowLabel')}</span></div>
        <div className="np-stat"><span className="np-num">{columnCount}</span><span className="np-lab">{t('node.colLabel')}</span></div>
        <div className="np-stat"><span className="np-num">{fkCount}</span><span className="np-lab">{t('node.fkLabel')}</span></div>
      </div>
      <div className="np-actions">
        <button className="np-open" onClick={onOpenData}>{t('node.viewData')} →</button>
      </div>
    </div>
  )
}
