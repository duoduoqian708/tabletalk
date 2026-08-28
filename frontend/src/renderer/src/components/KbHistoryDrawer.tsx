import { useCallback, useEffect, useState } from 'react'
import { listAudit } from '@renderer/api/audit'
import type { AuditEntry } from '@renderer/api/types'
import { useI18n } from '@renderer/store/i18n'
import { fmtDT } from '@renderer/lib/timefmt'

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

/** 把审计条目解析成人类可读的「动作 + 明细」 */
function describe(e: AuditEntry): KbHistoryRow {
  const sql = e.sql || ''
  const ts = e.status && !e.status.includes('=') ? e.ts : e.ts
  if (e.status === 'discarded') {
    const d = (e as unknown as { discarded?: { columns?: number; tables?: number; tags?: number; edges?: number } }).discarded
    const parts: string[] = []
    if (d) {
      if (d.columns) parts.push(`列×${d.columns}`)
      if (d.tables) parts.push(`表×${d.tables}`)
      if (d.tags) parts.push(`标签×${d.tags}`)
      if (d.edges) parts.push(`边×${d.edges}`)
    }
    return { kind: 'discard', ts, sql, detail: parts.length ? `撤下草案 ${parts.join(' · ')}` : '撤下本轮全部草案' }
  }
  if (sql.includes('-- kb confirm_all')) {
    const m = sql.match(/docs=(\d+)[^,]*tags=(\d+)/)
    return { kind: 'confirm', ts, sql, detail: m ? `确认启用：注释×${m[1]} 标签×${m[2]}` : '确认启用知识库' }
  }
  if (sql.includes('trigger=rebuild')) {
    const auth = (e as unknown as { include_samples?: boolean }).include_samples
    return { kind: 'rebuild', ts, sql, detail: auth ? '全量重建 · 样本已授权' : '全量重建 · 样本未授权' }
  }
  if (sql.includes('-- kb sync')) {
    const c = (e as unknown as { cleared_tags?: string[] }).cleared_tags ?? []
    const extra = e as unknown as { added_tables?: number; removed_tables?: number; changed_tables?: number }
    const part = []
    if (extra.added_tables) part.push(`+${extra.added_tables}表`)
    if (extra.removed_tables) part.push(`-${extra.removed_tables}表`)
    if (extra.changed_tables) part.push(`变更${extra.changed_tables}表`)
    if (c.length) part.push(`清理标签 ${c.join('、')}`)
    return { kind: 'sync', ts, sql, detail: `增量同步 ${part.join(' · ')}` }
  }
  if (sql.includes('-- kb build')) {
    const auth = (e as unknown as { include_samples?: boolean }).include_samples
    return { kind: 'build', ts, sql, detail: auth ? '首次构建 · 样本已授权' : '首次构建 · 样本未授权' }
  }
  return { kind: 'other', ts, sql, detail: e.status || '知识库操作' }
}

const KIND_LABEL: Record<KbActionKind, string> = {
  build: '构建',
  rebuild: '重建',
  discard: '放弃草案',
  confirm: '确认启用',
  sync: '增量同步',
  other: '操作',
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
        .map(describe)
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
            知识库历史
            <span className="kbh-conn mono">{connName}</span>
          </div>
          <span className="spacer" />
          <button className="kbh-close" onClick={onClose} title={t('common.close')}>✕</button>
        </div>
        <div className="kbh-sub mono">
          来自审计日志 origin=kb_build · 构建 / 重建 / 放弃 / 确认留痕
        </div>

        <div className="kbh-body">
        {error ? (
          <div className="kbh-empty mono">{t('common.failed')}: {error}</div>
        ) : loading ? (
          <div className="kbh-empty mono">{t('common.loading')}</div>
        ) : rows.length === 0 ? (
          <div className="kbh-empty mono">暂无知识库操作记录</div>
        ) : (
          <div className="kbh-list">
            {rows.map((r, i) => (
              <div key={`${r.ts}-${i}`} className="kbh-row">
                <span className={`kbh-dot ${r.kind}`} />
                <div className="kbh-mid">
                  <div className="kbh-row-top">
                    <span className={`kbh-kind kbh-kind-${r.kind}`}>{KIND_LABEL[r.kind]}</span>
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
            {loading ? '刷新中…' : '刷新'}
          </button>
          <button className="kbh-btn primary" onClick={onClose}>关闭</button>
        </div>
      </aside>
    </div>
  )
}