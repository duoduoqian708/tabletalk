import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { listAuditPage, type PageQuery } from '@renderer/api/audit'
import { VerdictBadge } from '../VerdictBadge'
import { fmtDT } from '@renderer/lib/timefmt'
import { useI18n } from '@renderer/store/i18n'

export type Sel = { kind: 'approval'; id: string } | { kind: 'entry'; id: number } | null
type Tab = 'todo' | 'all'

const PAGE = 50

interface Item { _id: number; ts: string; verdict: string; tier: string; origin: string; sql: string; ack?: string }

/** 左列下部：子页签[异常待办|全部流水] + 服务端搜索 + keyset 懒加载列表。 */
export default function EntryList(props: {
  selected: Sel
  onSelect: (s: Sel) => void
}): React.JSX.Element {
  const connId = useConnections((s) => s.currentId)
  const connName = useConnections((s) => s.list.find((c) => c.id === s.currentId)?.name ?? '')
  const { t } = useI18n()
  const [tab, setTab] = useState<Tab>('todo')
  const [unreadOnly, setUnreadOnly] = useState(false)
  const [qRaw, setQRaw] = useState('')
  const [q, setQ] = useState('')
  const [items, setItems] = useState<Item[]>([])
  const [cursor, setCursor] = useState<number | null>(0)
  const [loading, setLoading] = useState(false)
  const sentinel = useRef<HTMLDivElement>(null)
  const seqRef = useRef(0)

  // 300ms 防抖搜索
  useEffect(() => {
    const h = window.setTimeout(() => setQ(qRaw.trim()), 300)
    return () => window.clearTimeout(h)
  }, [qRaw])

  const baseQuery = useMemo<PageQuery>(() => ({
    connection: connName || undefined,
    exception: tab === 'todo' || undefined,
    unread_only: tab === 'todo' && unreadOnly || undefined,
    q: tab === 'all' && q || undefined,
    limit: PAGE,
  }), [connName, tab, unreadOnly, q])

  const loadFirst = useCallback(() => {
    if (!connId) return
    const seq = ++seqRef.current
    setLoading(true)
    void listAuditPage({ ...baseQuery, cursor: 0 })
      .then((r) => { if (seq === seqRef.current) { setItems(r.items as Item[]); setCursor(r.next_cursor) } })
      .catch(() => { if (seq === seqRef.current) { setItems([]); setCursor(null) } })
      .finally(() => { if (seq === seqRef.current) setLoading(false) })
  }, [baseQuery, connId])

  useEffect(loadFirst, [loadFirst])

  const loadMore = useCallback(() => {
    if (!connId || cursor == null || loading) return
    const seq = ++seqRef.current
    setLoading(true)
    void listAuditPage({ ...baseQuery, cursor }).then((r) => {
      if (seq !== seqRef.current) return
      setItems((prev) => [...prev, ...(r.items as Item[])]); setCursor(r.next_cursor)
    }).catch(() => { if (seq === seqRef.current) setCursor(null) }).finally(() => { if (seq === seqRef.current) setLoading(false) })
  }, [baseQuery, cursor, loading, connId])

  // 滚动到底自动加载
  useEffect(() => {
    const el = sentinel.current
    if (!el) return
    const ob = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) loadMore() })
    ob.observe(el)
    return () => ob.disconnect()
  }, [loadMore])

  return (
    <>
      <div className="au-search-row">
        {tab === 'all'
          ? <input className="au-search mono" placeholder={t('audit.searchServer')} value={qRaw} onChange={(e) => setQRaw(e.target.value)} />
          : <button className={`au-ltab${unreadOnly ? ' on' : ''}`} onClick={() => setUnreadOnly((v) => !v)}>{t('audit.unreadOnly')}</button>}
        <span className="spacer" />
        <button className={`au-ltab${tab === 'todo' ? ' on' : ''}`} onClick={() => setTab('todo')}>{t('audit.tabTodo')}</button>
        <button className={`au-ltab${tab === 'all' ? ' on' : ''}`} onClick={() => setTab('all')}>{t('audit.tabAll')}</button>
      </div>
      <div style={{ overflowY: 'auto', flex: 1 }}>
        {items.map((it) => (
          <div key={it._id}
            className={`au-row${props.selected?.kind === 'entry' && props.selected.id === it._id ? ' sel' : ''}`}
            onClick={() => props.onSelect({ kind: 'entry', id: it._id })}>
            <VerdictBadge v={it.verdict} />
            <span className="mono" style={{ fontSize: 11 }}>{it.sql.slice(0, 48)}</span>
            <span className="au-meta">
              {it.verdict === 'review' ? t('verdict.review') : ''}
              {it.ack === 'unread' && it.verdict !== 'allow' ? ` · ${t('audit.unreadTag')}` : ''}
              {' '}{fmtDT(it.ts)}
            </span>
          </div>
        ))}
        {!loading && items.length === 0 && <div className="mpage-empty">{t('audit.empty')} 🎉</div>}
        <div ref={sentinel} style={{ height: 28 }} />
        {cursor == null && items.length > 0 && <div className="mpage-empty" style={{ fontSize: 11 }}>{t('audit.noMore')}</div>}
      </div>
    </>
  )
}
