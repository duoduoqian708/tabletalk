/* 报告卡：章节序列 + SVG 图表块（柱/折/饼）+ 叙述 + 数字回溯。
 * 北欧极简：白面板、细边框、蓝强调。报告数据为快照（生成时刻固化）。 */
import { useState } from 'react'
import type { ReportView, ReportSectionResult } from '@renderer/store/results'

/* ---------- SVG 图表块：柱 / 折线 / 饼，北欧细线 ---------- */
const PAL = ['#0a6dff', '#7ba6ff', '#9ec0ff', '#c5d8ff', '#5a8eff', '#2f7bff']

function num(v: unknown): number {
  const n = typeof v === 'number' ? v : parseFloat(String(v))
  return Number.isNaN(n) ? 0 : n
}

function ChartBlock({ section }: { section: ReportSectionResult }): React.JSX.Element {
  const kind = section.chart?.kind ?? 'bar'
  const data: unknown[][] = section.chart?.data ?? []
  const cols: string[] = section.chart?.columns ?? section.columns ?? []
  if (data.length === 0 || cols.length === 0) {
    return <div className="rpt-chart empty mono">无图表数据</div>
  }
  // 第一列标签 + 第二列数值（符合聚合查询 month/region → 值 的典型形态）
  const labels: string[] = data.map((r: unknown[]) => String(r[0] ?? ''))
  const values: number[] = data.map((r: unknown[]) => num(r[1]))

  if (kind === 'line') {
    const max = Math.max(...values, 1)
    const W = 480
    const H = 160
    const pad = 28
    const step = values.length > 1 ? (W - pad * 2) / (values.length - 1) : 0
    const pts = values.map((v: number, i: number) => `${pad + i * step},${H - pad - (v / max) * (H - pad * 2)}`)
    const path = pts.join(' ')
    return (
      <svg className="rpt-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={section.title}>
        <line x1={pad} y1={H - pad} x2={W - pad} y2={H - pad} className="axis" />
        <line x1={pad} y1={pad} x2={pad} y2={H - pad} className="axis" />
        <polyline points={path} fill="none" stroke={PAL[0]} strokeWidth="2" />
        {pts.map((p: string, i: number) => (
          <g key={i}>
            <circle cx={Number(p.split(',')[0])} cy={Number(p.split(',')[1])} r="3" fill={PAL[0]} />
            <text x={pad + i * step} y={H - pad + 14} className="ax-lab" textAnchor="middle">{labels[i]}</text>
          </g>
        ))}
      </svg>
    )
  }

  if (kind === 'pie') {
    const total = values.reduce((a: number, b: number) => a + b, 0) || 1
    const cx = 110
    const cy = 110
    const R = 90
    let acc = 0
    const slices = values.map((v: number, i: number) => {
      const start = (acc / total) * 2 * Math.PI - Math.PI / 2
      acc += v
      const end = (acc / total) * 2 * Math.PI - Math.PI / 2
      const x1 = cx + R * Math.cos(start)
      const y1 = cy + R * Math.sin(start)
      const x2 = cx + R * Math.cos(end)
      const y2 = cy + R * Math.sin(end)
      const large = end - start > Math.PI ? 1 : 0
      return { d: `M${cx},${cy} L${x1},${y1} A${R},${R} 0 ${large} 1 ${x2},${y2} Z`, fill: PAL[i % PAL.length], label: labels[i], val: v }
    })
    return (
      <div className="rpt-pie-wrap">
        <svg className="rpt-chart pie" viewBox="0 0 220 220" role="img" aria-label={section.title}>
          {slices.map((s: { d: string; fill: string }, i: number) => (
            <path key={i} d={s.d} fill={s.fill} stroke="#fff" strokeWidth="1.5" />
          ))}
        </svg>
        <ul className="rpt-pie-legend mono">
          {slices.map((s: { label: string; val: number }, i: number) => (
            <li key={i}><span className="sw" style={{ background: PAL[i % PAL.length] }} />{s.label} · {s.val}</li>
          ))}
        </ul>
      </div>
    )
  }

  // bar（默认）
  const max = Math.max(...values, 1)
  const W = 480
  const H = 160
  const pad = 28
  const bw = Math.max(8, (W - pad * 2) / values.length - 8)
  const slot = (W - pad * 2) / values.length
  return (
    <svg className="rpt-chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={section.title}>
      <line x1={pad} y1={H - pad} x2={W - pad} y2={H - pad} className="axis" />
      <line x1={pad} y1={pad} x2={pad} y2={H - pad} className="axis" />
      {values.map((v: number, i: number) => {
        const h = (v / max) * (H - pad * 2)
        const x = pad + i * slot + (slot - bw) / 2
        const y = H - pad - h
        return (
          <g key={i}>
            <rect x={x} y={y} width={bw} height={h} fill={PAL[i % PAL.length]} rx="2" />
            <text x={x + bw / 2} y={H - pad + 14} className="ax-lab" textAnchor="middle">{labels[i]}</text>
          </g>
        )
      })}
    </svg>
  )
}

/* ---------- 单章折叠数据块（复用表格观感，简化版） ---------- */
function SectionData({ section }: { section: ReportSectionResult }): React.JSX.Element {
  const [open, setOpen] = useState(false)
  const cols: string[] = section.columns ?? []
  const rows: unknown[][] = section.rows ?? []
  if (!section.ok) {
    return <div className="rpt-sec-fail mono">查询未通过闸门：{section.reason ?? '拦截'}</div>
  }
  return (
    <div className="rpt-sec-data">
      <button className="rpt-fold" onClick={() => setOpen((o) => !o)}>
        {open ? '▾ 折叠明细' : '▸ 展开明细'}（{section.row_count} 行 · {section.elapsed_ms ?? 0}ms）
      </button>
      {open && (
        <div className="rpt-table-wrap">
          <table className="rpt-table mono">
            <thead>
              <tr>{cols.map((c: string, i: number) => <th key={i}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {rows.slice(0, 50).map((r: unknown[], i: number) => (
                <tr key={i}>{r.map((c: unknown, j: number) => <td key={j}>{String(c ?? '')}</td>)}</tr>
              ))}
              {rows.length > 50 && (
                <tr><td className="muted" colSpan={cols.length}>共 {rows.length} 行，已截断至前 50</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

/* ---------- 叙述：支持 [rN] 上标 → 跳转该章来源 ---------- */
function Narration({ text }: { text: string }): React.JSX.Element {
  if (!text) {
    return <div className="rpt-narr mono muted">（叙述生成中…）</div>
  }
  // 把 [r1] 标注转为上标，点击时滚到对应章
  const parts = text.split(/(\[r\d+\])/g)
  const scrollTo = (id: string): void => {
    document.getElementById(`rpt-sec-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
  return (
    <div className="rpt-narr">
      {parts.map((p: string, i: number) => {
        const m = /^\[r(\d+)\]$/.exec(p)
        if (m) {
          return (
            <button key={i} className="rpt-ref" onClick={() => scrollTo(`r${m[1]}`)} title={`跳到来源查询 r${m[1]}`}>
              r{m[1]}
            </button>
          )
        }
        return <span key={i}>{p}</span>
      })}
    </div>
  )
}

/* ---------- 报告卡主体 ---------- */
export function ReportCard({ report }: { report: ReportView }): React.JSX.Element {
  return (
    <div className="rpt-card">
      <header className="rpt-h">
        <div className="rpt-title">{report.title}</div>
        <div className="rpt-meta mono">
          快照 {report.snapshotTs} · {report.sections.length} 章 · {report.refs.length} 个可回溯来源 · 模型可见聚合结果集
        </div>
      </header>

      {report.sections.map((s: ReportSectionResult) => (
        <section className="rpt-sec" key={s.id} id={`rpt-sec-${s.id}`}>
          <div className="rpt-sec-h">
            <span className="rpt-sec-id mono">{s.id}</span>
            <span className="rpt-sec-t">{s.title}</span>
            <span className="rpt-sec-intent mono">{s.intent ?? ''}</span>
          </div>
          {s.ok && <ChartBlock section={s} />}
          <SectionData section={s} />
        </section>
      ))}

      {report.narration && (
        <section className="rpt-sec narr-sec">
          <div className="rpt-sec-h"><span className="rpt-sec-t">总结</span></div>
          <Narration text={report.narration} />
        </section>
      )}

      {report.refs.length > 0 && (
        <footer className="rpt-refs mono">
          <div className="rpt-refs-t">数字回溯</div>
          {report.refs.map((r: ReportView['refs'][number]) => (
            <button key={r.result_id} className="rpt-ref-line"
              onClick={() => document.getElementById(`rpt-sec-${r.result_id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })}>
              <span className="rid">{r.result_id}</span> {r.title}
              <span className="muted"> · {r.row_count} 行</span>
              <span className="muted sq"> {r.sql_head}</span>
            </button>
          ))}
        </footer>
      )}
    </div>
  )
}