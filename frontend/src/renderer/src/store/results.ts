import { create } from 'zustand'
import type { ReportSectionResult } from '@renderer/api/ai'

export type { ReportSectionResult }

export interface ResultSet {
  id: string
  title: string
  name: string
  headers: string[]
  types: string[]
  rows: unknown[][]
  meta: string
}

/* 报告形态：与表格 ResultSet 并存于结果区（数字回溯 + 快照口径） */
export interface ReportView {
  id: string
  reportId: string
  title: string
  snapshotTs: string
  sections: ReportSectionResult[]
  narration: string
  refs: { result_id: string; title: string; sql_head: string; row_count: number }[]
}

interface ResultsState {
  result: ResultSet | null
  report: ReportView | null
  filter: { col: number; text: string } | null
  sort: { index: number; dir: 1 | -1 } | null
  page: number
  /** 替换当前结果（AI-first：单一结果视图，无多 tab session） */
  push: (ds: Omit<ResultSet, 'id'>) => void
  clear: () => void
  /** 报告形态：用报告视图替换结果区（清掉表格） */
  setReport: (r: Omit<ReportView, 'id'>) => void
  clearReport: () => void
  /** 追加/更新一个章节到当前报告（section 事件流式到达时调用） */
  upsertSection: (s: ReportSectionResult) => void
  /** 写入叙述 + 来源 */
  setNarration: (text: string, refs: ReportView['refs']) => void
  setFilter: (f: { col: number; text: string } | null) => void
  /** 显式设置某列排序方向；同列同向再点 = 取消 */
  sortBy: (index: number, dir: 1 | -1) => void
  /** 点击列头循环：无 → 升 → 降 → 无 */
  cycleSort: (index: number) => void
  setPage: (p: number) => void
}

let seq = 0
let rseq = 0
const PAGE_SIZE = 50

export function pageSize(): number {
  return PAGE_SIZE
}

export const useResults = create<ResultsState>((set) => ({
  result: null,
  report: null,
  filter: null,
  sort: null,
  page: 0,

  push(ds) {
    const id = `res${++seq}`
    set({ result: { ...ds, id }, report: null, filter: null, sort: null, page: 0 })
  },

  clear() {
    set({ result: null, report: null, filter: null, sort: null, page: 0 })
  },

  setReport(r) {
    const id = `rep${++rseq}`
    set({ report: { ...r, id }, result: null, filter: null, sort: null, page: 0 })
  },

  clearReport() {
    set({ report: null })
  },

  upsertSection(s) {
    set((st) => {
      if (!st.report) return {}
      const exists = st.report.sections.findIndex((x) => x.id === s.id)
      const sections = exists >= 0
        ? st.report.sections.map((x) => (x.id === s.id ? s : x))
        : [...st.report.sections, s]
      return { report: { ...st.report, sections } }
    })
  },

  setNarration(text, refs) {
    set((st) => (st.report ? { report: { ...st.report, narration: text, refs } } : {}))
  },

  setFilter(f) {
    set({ filter: f, page: 0 })
  },

  sortBy(index, dir) {
    set((s) => {
      const sort = s.sort && s.sort.index === index && s.sort.dir === dir ? null : { index, dir }
      return { sort, page: 0 }
    })
  },

  cycleSort(index) {
    set((s) => {
      if (!s.sort || s.sort.index !== index) return { sort: { index, dir: 1 as const }, page: 0 }
      return { sort: s.sort.dir === 1 ? { index, dir: -1 as const } : null, page: 0 }
    })
  },

  setPage(p) {
    set({ page: p })
  }
}))

/** 当前结果集的派生视图：按列过滤 → 排序 → 分页。 */
export function selectRows(
  result: ResultSet,
  filter: { col: number; text: string } | null,
  sort: { index: number; dir: 1 | -1 } | null
): unknown[][] {
  let rows = result.rows
  if (filter && filter.text) {
    const f = filter.text.toLowerCase()
    rows = rows.filter((r) => String(r[filter.col] ?? '').toLowerCase().includes(f))
  }
  if (sort) {
    const { index, dir } = sort
    rows = [...rows].sort((a, b) => {
      const x = a[index]
      const y = b[index]
      const nx = typeof x === 'number' ? x : parseFloat(String(x))
      const ny = typeof y === 'number' ? y : parseFloat(String(y))
      if (!Number.isNaN(nx) && !Number.isNaN(ny)) return (nx - ny) * dir
      return String(x).localeCompare(String(y)) * dir
    })
  }
  return rows
}
