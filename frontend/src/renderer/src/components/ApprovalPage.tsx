import { useEffect, useState } from 'react'
import { getRuntime } from '@renderer/api/client'
import { fmtDT } from '@renderer/lib/timefmt'
import { useI18n } from '@renderer/store/i18n'

interface Approval {
  id: string
  connection_id: string
  sql: string
  requested_by: string
  requested_at: string
  status: string
  reviewed_by?: string
  reviewed_at?: string
  note?: string
}

export function ApprovalPage(): React.JSX.Element {
  const { t } = useI18n()
  const [items, setItems] = useState<Approval[]>([])
  const [filter, setFilter] = useState<string>('')
  const load = async (): Promise<void> => {
    const rt = getRuntime()
    if (!rt?.token) return
    const qs = filter ? `?status=${filter}` : ''
    const r = await fetch(`/api/v1/approvals${qs}`, { headers: { 'X-TableTalk-Token': rt.token } })
    if (r.ok) {
      const j = await r.json()
      setItems(j.items || [])
    }
  }
  useEffect(() => { void load() }, [filter])
  const act = async (id: string, action: 'approve' | 'reject'): Promise<void> => {
    const rt = getRuntime()
    if (!rt?.token) return
    const note = action === 'reject' ? (prompt(t('approval.rejectPrompt')) || '') : (prompt(t('approval.approveNotePrompt')) || '')
    if (action === 'reject' && !note.trim()) {
      alert(t('approval.rejectNeedsReason'))
      return
    }
    const r = await fetch(`/api/v1/approvals/${id}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-TableTalk-Token': rt.token },
      body: JSON.stringify({ note }),
    })
    if (r.ok) void load()
    else {
      const j = await r.json().catch(() => ({}))
      alert(j.detail || t('toast.failed'))
    }
  }
  const pendingCount = items.filter((a) => a.status === 'pending').length
  return (
    <div className="review kb-page">
      <div className="audit-head">
        <h1>{t('approval.title')} <span className="db-chip mono">DML Review</span>{pendingCount > 0 && <span className="db-chip mono" style={{ background: 'var(--amber-dim)', color: 'var(--amber)', marginLeft: 8 }}>{t('approval.pendingBadge', { n: pendingCount })}</span>}</h1>
        <p>{t('approval.subtitle')}</p>
      </div>
      <div className="audit-bar">
        <div className="seg">
          <button className={`seg-b${filter===''?' on':''}`} onClick={()=> setFilter('')}>{t('common.all')}</button>
          <button className={`seg-b${filter==='pending'?' on':''}`} onClick={()=> setFilter('pending')}>{t('approval.filterPending')}</button>
          <button className={`seg-b${filter==='approved'?' on':''}`} onClick={()=> setFilter('approved')}>{t('approval.filterApproved')}</button>
          <button className={`seg-b${filter==='rejected'?' on':''}`} onClick={()=> setFilter('rejected')}>{t('approval.filterRejected')}</button>
        </div>
        <span className="spacer" />
        <button className="rs-btn" onClick={()=> void load()}>{t('common.refresh')}</button>
      </div>
      <div className="panel">
        {items.length===0 && <div className="mpage-empty">{t('approval.empty')}</div>}
        {items.map((a)=> (
          <div key={a.id} className="approval-row" style={{padding:'10px', borderTop:'1px solid var(--line)', display:'flex', flexDirection:'column', gap:6}}>
            <div style={{display:'flex', gap:12, alignItems:'center', width:'100%'}}>
              <span className="mono" style={{minWidth:90, color: a.status==='pending' ? 'var(--amber)' : a.status==='approved' ? 'var(--green)' : 'var(--red)'}}>{a.status}</span>
              <span className="mono" style={{flex:1, whiteSpace:'nowrap', overflow:'hidden', textOverflow:'ellipsis'}} title={a.sql}>{a.sql.slice(0,80)}</span>
              <span className="mono" style={{fontSize:11, color:'var(--ink-faint)'}}>{a.requested_by} · {fmtDT(a.requested_at)}</span>
              {a.status==='pending' && (
                <>
                  <button className="mini-btn set" onClick={()=> void act(a.id,'approve')}>{t('approval.approve')}</button>
                  <button className="mini-btn dang" onClick={()=> void act(a.id,'reject')}>{t('approval.reject')}</button>
                </>
              )}
            </div>
            {(a.reviewed_by || a.note) && (
              <div className="mono" style={{fontSize:11, color:'var(--ink-dim)', paddingLeft:4}}>
                → {a.reviewed_by || '—'} · {a.reviewed_at ? fmtDT(a.reviewed_at) : '—'} {a.note ? `· ${a.note}` : ''} · {t('approval.auditChain', { id: a.id, by: a.reviewed_by || '—' })}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
