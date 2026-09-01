import { useCallback, useEffect, useState } from 'react'
import { listApprovals, approveApproval, rejectApproval, type ApprovalItem } from '@renderer/api/approvals'
import { listAuditPage, ackAudit } from '@renderer/api/audit'
import { useConnections } from '@renderer/store/connections'
import { useAuditSignal } from '@renderer/store/auditSignal'
import { VerdictBadge } from '../VerdictBadge'
import { fmtDT } from '@renderer/lib/timefmt'
import { useI18n } from '@renderer/store/i18n'
import type { Sel } from './EntryList'

interface Entry { _id: number; ts: string; verdict: string; tier: string; origin: string; sql: string; ack?: string; reasons?: { rule_id: string; message: string; message_en?: string; objects?: string[] }[] }

function explain403(e: unknown, gateBlockMsg: string): string {
  const msg = (e as { message?: string }).message ?? String(e)
  return msg.includes('403') ? gateBlockMsg : msg
}

/** 右列：默认今日时间线；选中左列条目→完整详情+处置。 */
export default function DetailPane(props: { selected: Sel; onApprovalChanged?: () => void }): React.JSX.Element {
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '')
  const { t } = useI18n()
  const refreshSignal = useAuditSignal((s) => s.refresh)
  const onChanged = (): void => { void refreshSignal() }

  if (!props.selected) return <TodayTimeline connName={connName} />
  return props.selected.kind === 'approval'
    ? <ApprovalDetail id={props.selected.id} onChanged={onChanged} onApprovalChanged={props.onApprovalChanged} />
    : <EntryDetail id={props.selected.id} onChanged={onChanged} />

  function TodayTimeline({ connName }: { connName: string }): React.JSX.Element {
    const [rows, setRows] = useState<Entry[]>([])
    // 存储为 UTC，"今日"边界 = 本地今日 00:00 换算成 UTC（toISOString 基于 UTC 时钟）
    const now = new Date()
    const fromTs = new Date(now.getFullYear(), now.getMonth(), now.getDate()).toISOString().slice(0, 19)
    useEffect(() => {
      let alive = true
      void listAuditPage({ connection: connName || undefined, from_ts: fromTs, limit: 50, cursor: 0 })
        .then((r) => { if (alive) setRows(r.items as Entry[]) }).catch(() => undefined)
      return () => { alive = false }
    }, [connName])
    return (
      <div className="au-right-pane">
        <div className="tl-day"><b>{t('audit.today')}</b><span className="au-meta">{t('audit.nRecords', { n: rows.length })}</span></div>
        {rows.map((r) => (
          <div key={r._id} className="ev-row">
            <span className="ev-t">{fmtDT(r.ts, 'time')}</span>
            <VerdictBadge v={r.verdict} />
            <span className="mono" style={{ fontSize: 11 }}>{r.sql.slice(0, 56)}</span>
          </div>
        ))}
        {rows.length === 0 && <div className="mpage-empty">{t('audit.empty')}</div>}
      </div>
    )
  }

  function ApprovalDetail({ id, onChanged, onApprovalChanged }: { id: string; onChanged: () => void; onApprovalChanged?: () => void }): React.JSX.Element {
    const [item, setItem] = useState<ApprovalItem | null>(null)
    const [busy, setBusy] = useState(false)
    const [confirming, setConfirming] = useState(false)
    const [err, setErr] = useState<string | null>(null)
    const reload = useCallback(() => {
      setConfirming(false)
      void listApprovals().then((r) => setItem(r.items.find((x) => x.id === id) ?? null)).catch(() => undefined)
    }, [id])
    useEffect(reload, [reload])
    // Q6：批准即执行写库——影响行数较大时先红框确认，避免误点即写
    const APPROVE_CONFIRM_ROWS = 1000
    async function decide(kind: 'approve' | 'reject', force = false): Promise<void> {
      if (!item) return
      if (kind === 'approve' && !force && item.preview_rows != null && item.preview_rows > APPROVE_CONFIRM_ROWS) {
        setConfirming(true)
        return
      }
      setConfirming(false); setBusy(true); setErr(null)
      try {
        if (kind === 'approve') await approveApproval(item.id)
        else await rejectApproval(item.id, 'rejected from audit page')
        onChanged(); onApprovalChanged?.(); reload()
      } catch (e) { setErr(explain403(e, t('audit.gateBlockRetry'))) } finally { setBusy(false) }
    }
    if (!item) return <div className="au-right-pane"><div className="mpage-empty">{t('common.loading')}</div></div>
    return (
      <div className="au-right-pane"><div className="det-body">
        <div className="det-kv"><span className="k">{t('audit.colVerdict')}</span><VerdictBadge v={item.status === 'pending' ? 'review' : item.status === 'rejected' ? 'block' : 'executed'} /><span className="au-meta mono">{fmtDT(item.requested_at)}</span></div>
        <div className="det-sql">{item.sql}</div>
        {item.preview_rows != null && <div className="det-kv"><span className="k">{t('audit.previewRows')}</span><span className="mono">COUNT ≈ {item.preview_rows}</span></div>}
        {err && <div className="review-err">{err}</div>}
        {confirming && (
          <div className="review-err" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <span>{t('audit.approveConfirmRows', { n: item.preview_rows ?? '?' })}</span>
            <button className="btn pri" disabled={busy} onClick={() => void decide('approve', true)}>{t('audit.confirmApprove')}</button>
            <button className="btn gho" disabled={busy} onClick={() => setConfirming(false)}>{t('common.cancel')}</button>
          </div>
        )}
        {item.status === 'pending' ? (
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn pri" disabled={busy} onClick={() => void decide('approve')}>{t('audit.approveExec')}</button>
            <button className="btn gho" disabled={busy} onClick={() => void decide('reject')}>{t('audit.rejectBtn')}</button>
          </div>
        ) : (
          <div className="det-kv"><span className="k">{t('audit.metaStatus')}</span><span>{item.status}{item.executed_audit_id ? ` · ${t('audit.executedTag')}` : ''}</span></div>
        )}
      </div></div>
    )
  }

  function EntryDetail({ id, onChanged }: { id: number; onChanged: () => void }): React.JSX.Element {
    const connName2 = connName
    const [entry, setEntry] = useState<Entry | null>(null)
    useEffect(() => {
      let alive = true
      // 用 page(before_id=id+1, limit=1) 定位该行（cursor 为严格小于，倒序第一行即目标行本身）。
      void listAuditPage({ connection: connName2 || undefined, cursor: (id as number) + 1, limit: 1 })
        .then((r) => { if (alive) setEntry((r.items as Entry[])[0] ?? null) }).catch(() => undefined)
      return () => { alive = false }
    }, [id, connName2])
    if (!entry) return <div className="au-right-pane"><div className="mpage-empty">{t('common.loading')}</div></div>
    const canAck = entry.verdict !== 'allow' && entry.ack !== 'ack'
    return (
      <div className="au-right-pane"><div className="det-body">
        <div className="det-kv"><span className="k">{t('audit.colVerdict')}</span><VerdictBadge v={entry.verdict} /><span className="au-meta mono">{fmtDT(entry.ts)} · {entry.origin === 'ai' ? 'AI' : t('audit.originManual')}</span></div>
        <div className="det-sql">{entry.sql}</div>
        {(entry.reasons ?? []).map((r, i) => (
          <div key={i} className="det-kv"><span className="k">{t('audit.triggerRule')}</span><span className="mono">{r.rule_id}</span><span style={{ color: 'var(--ink-dim)' }}>{r.message}</span></div>
        ))}
        {canAck && (
          <button className="btn pri" onClick={async () => { await ackAudit(entry._id); onChanged() }}>{t('audit.markAcked')}</button>
        )}
      </div></div>
    )
  }
}
