import { useCallback, useEffect, useState } from 'react'
import { listAudit } from '@renderer/api/audit'
import type { AuditEntry } from '@renderer/api/types'
import { useI18n } from '@renderer/store/i18n'
import { fmtDT } from '@renderer/lib/timefmt'
import { CloseBtn } from './ui/buttons'

/* ═══════════════════════════════════════════════
   知识库历史记录抽屉（右滑面板）
   数据源：审计日志 origin=kb_build —— 每次「构建 / 重建 / 放弃草案 / 确认启用」。
   在读已有留痕，不改审计，不复活审批流。
   ═══════════════════════════════════════════════ */

type KbActionKind = 'build' | 'rebuild' | 'discard' | 'confirm' | 'sync' | 'other'

interface KbHistoryRow {
  kind: KbActionKind
  ts: string
  detail: string
  sql: string
}

/** 审计 extra_json 平铺字段（build/sync 留痕时的结构化明细） */
interface KbAuditExtra {
  trigger?: 'init' | 'rebuild'
  include_samples?: boolean
  added_tables?: number
  removed_tables?: number
  changed_tables?: number
  cleared_tags?: string[]
  discarded?: { columns?: number; tables?: number; tags?: number; edges?: number }
}

/** 把审计条目解析成「动作 + 明细」：kind 由结构化字段（status/trigger）判定，
    SQL 字符串只用于提取数字明细——不再解析注释文案 */
function describe(e: AuditEntry, tr: (k: string, v?: Record<string, string | number>) => string): KbHistoryRow {
  const sql = e.sql || ''
  const ts = e.ts
  const ex = e as unknown as KbAuditExtra
  if (e.status === 'discarded') {
    const d = ex.discarded
    const parts: string[] = []
    if (d) {
      if (d.columns) parts.push(tr('kbh.detail.col', { n: d.columns }))
      if (d.tables) parts.push(tr('kbh.detail.table', { n: d.tables }))
      if (d.tags) parts.push(tr('kbh.detail.tag', { n: d.tags }))
      if (d.edges) parts.push(tr('kbh.detail.edge', { n: d.edges }))
    }
    return { kind: 'discard', ts, sql, detail: parts.length ? tr('kbh.detail.discard', { detail: parts.join(' · ') }) : tr('kbh.detail.discardAll') }
  }
  if (e.status === 'synced') {
    const part: string[] = []
    if (ex.added_tables) part.push(tr('kbh.detail.added', { n: ex.added_tables }))
    if (ex.removed_tables) part.push(tr('kbh.detail.removed', { n: ex.removed_tables }))
    if (ex.changed_tables) part.push(tr('kbh.detail.changed', { n: ex.changed_tables }))
    if (ex.cleared_tags?.length) part.push(tr('kbh.detail.cleared', { tags: ex.cleared_tags.join('、') }))
    return { kind: 'sync', ts, sql, detail: tr('kbh.detail.sync', { detail: part.join(' · ') }) }
  }
  if (e.status === 'confirmed') {
    const m = sql.match(/docs=(\d+)[^,]*tags=(\d+)/)
    return { kind: 'confirm', ts, sql, detail: m ? tr('kbh.detail.confirm', { docs: m[1], tags: m[2] }) : tr('kbh.detail.confirmPlain') }
  }
  if (ex.trigger === 'rebuild') {
    return { kind: 'rebuild', ts, sql, detail: ex.include_samples ? tr('kbh.detail.rebuild') : tr('kbh.detail.rebuildPlain') }
  }
  if (ex.trigger === 'init') {
    return { kind: 'build', ts, sql, detail: ex.include_samples ? tr('kbh.detail.build') : tr('kbh.detail.buildPlain') }
  }
  return { kind: 'other', ts, sql, detail: e.status || tr('kbh.detail.other') }
}

function fmtTime(ts: string): string {
  return fmtDT(ts)
}

export function KbHistoryDrawer({ open, onClose, connId, connName }: {
  open: boolean
  onClose: () => void
  connId: string | null
  connName: string
}): React.JSX.Element | null {
  const { t } = useI18n()
  const [rows, setRows] = useState<KbHistoryRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!connId) return
    setLoading(true)
    setError(null)
    try {
      const r = await listAudit(undefined, { origin: 'kb_build', limit: 200 })
      const mine = (r.entries ?? [])
        .filter((e) => e.connection === connId)
        .map((e) => describe(e, t))
      setRows(mine)
    } catch (e) {
      setError((e as Error).message)
      setRows([])
    } finally {
      setLoading(false)
    }
  }, [connId])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  if (!open) return null

  return (
    <div className="kbh-mask" onClick={onClose}>
      <aside className="kbh-drawer" onClick={(e) => e.stopPropagation()}>
        <div className="kbh-head">
          <div className="kbh-title">
            <span className="kbh-title-icon">🕘</span>
            {t('kbh.title')}
            <span className="kbh-conn mono">{connName}</span>
          </div>
          <span className="spacer" />
          <CloseBtn className="kbh-close" title={t('common.close')} onClick={onClose} />
        </div>
        <div className="kbh-sub mono">
          {t('kbh.sub')}
        </div>

        <div className="kbh-body">
        {error ? (
          <div className="kbh-empty mono">{t('common.failed')}: {error}</div>
        ) : loading ? (
          <div className="kbh-empty mono">{t('common.loading')}</div>
        ) : rows.length === 0 ? (
          <div className="kbh-empty mono">{t('kbh.empty')}</div>
        ) : (
          <div className="kbh-list">
            {rows.map((r, i) => (
              <div key={`${r.ts}-${i}`} className="kbh-row">
                <span className={`kbh-dot ${r.kind}`} />
                <div className="kbh-mid">
                  <div className="kbh-row-top">
                    <span className={`kbh-kind kbh-kind-${r.kind}`}>{t(`kbh.kind.${r.kind}`)}</span>
                    <span className="kbh-ts mono">{fmtTime(r.ts)}</span>
                  </div>
                  <div className="kbh-detail">{r.detail}</div>
                  {r.sql && r.sql.startsWith('--') && (
                    <div className="kbh-sql mono" title={r.sql}>{r.sql}</div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        </div>

        <div className="kbh-foot">
          <button className="kbh-btn" onClick={() => void load()} disabled={loading}>
            {loading ? t('kbh.refreshIng') : t('kbh.refresh')}
          </button>
          <button className="kbh-btn primary" onClick={onClose}>{t('kbh.close')}</button>
        </div>
      </aside>
    </div>
  )
}