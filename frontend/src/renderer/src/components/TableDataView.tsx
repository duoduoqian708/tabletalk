import { useEffect, useState } from 'react'
import { previewTable } from '@renderer/api/schema'
import type { TablePreview } from '@renderer/api/types'
import { useI18n } from '@renderer/store/i18n'

interface Props {
  connId: string
  table: string
  onClose: () => void
}

export function TableDataView({ connId, table, onClose }: Props): React.JSX.Element {
  const { t } = useI18n()
  const [data, setData] = useState<TablePreview | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    setData(null); setErr(null)
    previewTable(connId, table, 200)
      .then((d) => alive && setData(d))
      .catch((e) => alive && setErr((e as Error).message))
    return () => { alive = false }
  }, [connId, table])

  return (
    <div className="table-data-view">
      <div className="tdv-head">
        <span className="tdv-title">{t('node.tableLabel')} <b>{table}</b></span>
        <span className="tdv-sub mono">{data ? `${data.total} ${t('table.rows')}` : t('common.loading')}</span>
        <span className="spacer" />
        <button className="tdv-back" onClick={onClose}>← {t('table.backToGraph')}</button>
      </div>
      <div className="tdv-body">
        {err && <div className="tdv-err">{t('table.loadFail')}: {err}</div>}
        {!err && !data && <div className="tdv-loading">{t('table.readingData')}</div>}
        {data && (
          <table className="tdv-table">
            <thead>
              <tr>
                {data.columns.map((c, i) => (
                  <th key={c}>
                    <span className="tdv-col">{c}</span>
                    <span className="tdv-type mono">{data.types[i]}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((row, ri) => (
                <tr key={ri}>
                  {row.map((cell, ci) => (
                    <td key={ci} className="mono">{cell === null ? <span className="tdv-null">NULL</span> : String(cell)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
