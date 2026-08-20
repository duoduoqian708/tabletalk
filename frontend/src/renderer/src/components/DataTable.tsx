import { useEffect, useMemo, useRef, useState } from 'react'
import { pageSize, selectRows, useResults } from '@renderer/store/results'
import { toastMsg } from '@renderer/utils/toast'
import { useI18n } from '@renderer/store/i18n'

/* 三个列头操作 icon（北欧极简细线） */
function IconUp(): React.JSX.Element {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
      <path d="M5 1 L8.2 4.4 H6.4 V9 H3.6 V4.4 H1.8 Z" fill="currentColor" />
    </svg>
  )
}

function IconDown(): React.JSX.Element {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
      <path d="M5 9 L1.8 5.6 H3.6 V1 H6.4 V5.6 H8.2 Z" fill="currentColor" />
    </svg>
  )
}

function IconFilter(): React.JSX.Element {
  return (
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.1" strokeLinejoin="round" aria-hidden="true">
      <path d="M1 1.6 H9 L6 5.4 V8.2 L4 7.2 V5.4 Z" />
    </svg>
  )
}

/** JSON 语法高亮（key/字符串/数字/布尔/null）。非法 JSON 返回转义原文。 */
function highlightJson(text: string): string {
  const esc = (s: string): string =>
    s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const re =
    /("(?:[^"\\]|\\.)*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g
  return esc(text).replace(re, (m, str, colon, kw, num) => {
    if (str) {
      return colon ? `<span class="k-kw">${m.replace(colon, '')}</span>${esc(colon)}` : `<span class="k-str">${m}</span>`
    }
    if (kw) return `<span class="k-kw">${m}</span>`
    if (num) return `<span class="k-num">${m}</span>`
    return m
  })
}

/** 判定字符串是否为合法 JSON（对象/数组），合法返回格式化文本。
 *  防精度丢失：字符串外的 ≥16 位整数加引号转字符串再 parse（如 9007199254740993）。 */
function tryFormatJson(text: string): { ok: true; pretty: string } | { ok: false } {
  const t = text.trim()
  if (!t.startsWith('{') && !t.startsWith('[')) return { ok: false }
  let out = ''
  let inStr = false
  let i = 0
  while (i < t.length) {
    const ch = t[i]
    if (inStr) {
      out += ch
      if (ch === '\\' && i + 1 < t.length) {
        out += t[i + 1]
        i += 2
        continue
      }
      if (ch === '"') inStr = false
      i += 1
      continue
    }
    if (ch === '"') {
      inStr = true
      out += ch
      i += 1
      continue
    }
    if (ch === '-' || (ch >= '0' && ch <= '9')) {
      let j = i + 1
      while (j < t.length && t[j] >= '0' && t[j] <= '9') j += 1
      const num = t.slice(i, j)
      out += num.replace('-', '').length >= 16 ? `"${num}"` : num
      i = j
      continue
    }
    out += ch
    i += 1
  }
  try {
    return { ok: true, pretty: JSON.stringify(JSON.parse(out), null, 2) }
  } catch {
    return { ok: false }
  }
}

interface PopState {
  value: string
  /** 打开时刻锚定的单元格位置（之后格式化/重渲染不移动弹窗） */
  x: number
  anchorTop: number
  anchorBottom: number
  flipUp: boolean
  /** 翻转向上时的下界（表格可视区底部） */
  areaBottom: number
}

/** 单元格预览浮层：点击单元格的「⋯」触发，位置锚定打开时刻。 */
function CellPop({ pop, onClose }: { pop: PopState; onClose: () => void }): React.JSX.Element {
  const { t } = useI18n()
  const [fmt, setFmt] = useState<{ ok: true; pretty: string } | null>(null)
  const [err, setErr] = useState(false)
  const isJson = /^\s*[{[]/.test(pop.value)

  const style: React.CSSProperties = {
    left: Math.min(pop.x, window.innerWidth - 440),
    // 锚定：下方空间够→top=单元格底部；不够→bottom=单元格顶部（翻转向上）
    ...(pop.flipUp
      ? { top: undefined, bottom: pop.areaBottom - pop.anchorTop, maxHeight: Math.max(120, pop.anchorTop - 16) }
      : { top: pop.anchorBottom, bottom: undefined, maxHeight: Math.max(120, pop.areaBottom - pop.anchorBottom - 8) })
  }

  return (
    <div className="cell-pop" style={style}>
      <div className="cp-head">
        <span className="cp-tag">{isJson ? 'JSON' : 'TEXT'}</span>
        <span className="cp-acts">
          {isJson && (
              <button
                className={`cp-btn${fmt ? ' on' : ''}`}
                onClick={() => {
                  const r = tryFormatJson(pop.value)
                  if (r.ok) { setFmt(r); setErr(false) } else { setFmt(null); setErr(true) }
                }}
              >
                {t('ws.format')}
              </button>
          )}
          <button className="cp-btn" onClick={onClose}>✕</button>
        </span>
      </div>
      <div className="cp-body" dangerouslySetInnerHTML={{ __html: fmt ? highlightJson(fmt.pretty) : highlightJson(pop.value) }} />
      {err && <div className="cp-err">{t('table.jsonErr')}</div>}
    </div>
  )
}

export function DataTable(): React.JSX.Element {
  const { t } = useI18n()
  const { filter, sort, page, setFilter, sortBy, cycleSort, setPage } = useResults()
  const active = useResults((s) => s.tabs.find((t) => t.id === s.activeId) ?? null)
  const result = active?.kind === 'data' ? active.result : null
  const [filterCol, setFilterCol] = useState<number | null>(null)
  const [hlRow, setHlRow] = useState<number | null>(null)
  const [pop, setPop] = useState<PopState | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const { rows, total } = useMemo(() => {
    if (!result) return { rows: [], total: 0 }
    const all = selectRows(result, filter, sort)
    return { rows: all, total: all.length }
  }, [result, filter, sort])

  // 点击外部/Escape/滚动关闭弹窗（打开时刻锚定，不随鼠标移动）
  useEffect(() => {
    if (!pop) return
    const onDown = (e: MouseEvent): void => {
      const t = e.target as HTMLElement
      if (!t.closest('.cell-pop') && !t.closest('.c-more')) setPop(null)
    }
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') setPop(null)
    }
    const close = (): void => setPop(null)
    const sc = scrollRef.current
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    sc?.addEventListener('scroll', close)
    window.addEventListener('resize', close)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      sc?.removeEventListener('scroll', close)
      window.removeEventListener('resize', close)
    }
  }, [pop])

  if (!result) {
    return (
      <div className="tableview">
        <div className="empty">
          <div>
            <div style={{ fontSize: 13, color: 'var(--ink-dim)', marginBottom: 6 }}>{t('table.noResult')}</div>
            {t('table.hint1', { brand: 'tabletalk' })}<br />
            {t('table.hint2')}
          </div>
        </div>
      </div>
    )
  }

  const PAGE = pageSize()
  const pageCount = Math.max(1, Math.ceil(total / PAGE))
  const pageRows = rows.slice(page * PAGE, (page + 1) * PAGE)
  const showPager = total > PAGE
  const activeFilter = filter && filter.col === filterCol ? filter.text : ''

  function toggleColFilter(i: number): void {
    setFilterCol((prev) => {
      if (prev === i) {
        setFilter(null)
        return null
      }
      setFilter(null)
      return i
    })
  }

  /** 点击「⋯」打开预览：锚定当前单元格位置，之后不随鼠标/格式化移动。 */
  function openPop(v: string, td: HTMLTableCellElement): void {
    const r = td.getBoundingClientRect()
    // 以表格可视区底部为界（而非整个视口），格式化后内容变高也不越界
    const areaBottom = scrollRef.current?.getBoundingClientRect().bottom ?? window.innerHeight
    const flipUp = r.bottom + 320 > areaBottom
    setPop({
      value: v,
      x: r.left,
      anchorTop: r.top,
      anchorBottom: r.bottom + 6,
      flipUp,
      areaBottom
    })
  }

  async function copyText(text: string, what: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text)
      toastMsg(what)
    } catch {
      toastMsg(t('ws.copyFail'))
    }
  }

  function copyCell(v: unknown): void {
    void copyText(String(v), t('toast.copied'))
  }

  function copyRow(ri: number): void {
    if (!result) return
    const obj: Record<string, unknown> = {}
    result.headers.forEach((h, i) => { obj[h] = pageRows[ri][i] })
    void copyText(JSON.stringify(obj), t('ws.copyRow'))
  }

  return (
    <div className="tableview">
      {filterCol !== null && (
        <div className="colfilter">
          <span className="cf-ic">⌕</span>
          <span className="cf-col mono">{result.headers[filterCol] ?? ''}</span>
          <input
            autoFocus
            value={activeFilter}
            placeholder={t('table.filterCol')}
            onChange={(e) => setFilter({ col: filterCol, text: e.target.value })}
            onKeyDown={(e) => { if (e.key === 'Escape') toggleColFilter(filterCol) }}
          />
          <button className="cf-x" onClick={() => toggleColFilter(filterCol)}>✕</button>
        </div>
      )}
      <div className="scroll" ref={scrollRef}>
        <table>
          <thead>
            <tr>
              <th className="rowno"><span className="th-main">#</span></th>
              {result.headers.map((h, i) => (
                <th key={h + i} onClick={() => cycleSort(i)}>
                  <span className="th-main">{h}</span>
                  <span className="th-tyline">
                    <span className="ty">{result.types[i] ?? ''}</span>
                    <span className="th-ops">
                      <span
                        className={`th-op${sort?.index === i && sort.dir === 1 ? ' on' : ''}`}
                        title={sort?.index === i && sort.dir === 1 ? t('table.ascClear') : t('table.asc')}
                        onClick={(e) => { e.stopPropagation(); sortBy(i, 1) }}
                      ><IconUp /></span>
                      <span
                        className={`th-op${sort?.index === i && sort.dir === -1 ? ' on' : ''}`}
                        title={sort?.index === i && sort.dir === -1 ? t('table.descClear') : t('table.desc')}
                        onClick={(e) => { e.stopPropagation(); sortBy(i, -1) }}
                      ><IconDown /></span>
                      <span
                        className={`th-op${filterCol === i ? ' on' : ''}`}
                        title={filterCol === i ? t('table.filterClear') : t('table.filterOn')}
                        onClick={(e) => { e.stopPropagation(); toggleColFilter(i) }}
                      ><IconFilter /></span>
                    </span>
                  </span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody key={result.id}>
            {pageRows.map((r, ri) => (
              <tr key={ri} className={hlRow === ri ? 'hl' : ''}>
                <td
                  className="rowno num"
                  title={t('ws.copyRowTip')}
                  onMouseEnter={() => setHlRow(ri)}
                  onMouseLeave={() => setHlRow(null)}
                  onClick={() => copyRow(ri)}
                >
                  {page * PAGE + ri + 1}
                </td>
                {r.map((c, ci) => {
                  const s = String(c)
                  const cls = s.endsWith('%') ? 'num pos' : typeof c === 'number' ? 'num' : ''
                  return (
                    <td
                      key={ci}
                      className={cls}
                      title={t('ws.copyTip')}
                      onClick={() => copyCell(c)}
                    >
                      <span className="c-text">{s}</span>
                      <CellMore onOpen={(td) => openPop(s, td)} />
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="tv-foot">
        <span className="ro mono" style={{ color: 'var(--teal)' }}>{t('table.rowCount', { n: total })}</span>
        <span className="spacer" />
        {showPager && (
          <span className="pager">
            <button disabled={page <= 0} onClick={() => setPage(page - 1)}>‹</button>
            <span style={{ margin: '0 6px' }}>{t('table.pageOf', { cur: page + 1, total: pageCount })}</span>
            <button disabled={page >= pageCount - 1} onClick={() => setPage(page + 1)}>›</button>
          </span>
        )}
        <span>{result.meta}</span>
      </div>

      {pop && <CellPop pop={pop} onClose={() => setPop(null)} />}
    </div>
  )
}

/** 「⋯」按钮：内容被截断时显示；点击打开预览（不触发行复制）。 */
function CellMore({ onOpen }: { onOpen: (td: HTMLTableCellElement) => void }): React.JSX.Element {
  const { t } = useI18n()
  const [truncated, setTruncated] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const td = btnRef.current?.closest('td')
    const span = td?.querySelector<HTMLElement>('.c-text')
    if (!td || !span) return
    setTruncated(span.scrollWidth > span.clientWidth + 1)
  }, [])

  return (
    <button
      ref={btnRef}
      className={`c-more${truncated ? '' : ' hidden'}`}
      title={t('table.viewFull')}
      onClick={(e) => {
        e.stopPropagation()
        const td = e.currentTarget.closest('td')
        if (td) onOpen(td as HTMLTableCellElement)
      }}
    >⋯</button>
  )
}
