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

/** 结果标签：数据浏览 / AI 查询 / 报告并存，可关闭可切换 */
export interface ResultTab {
  id: string
  kind: 'data' | 'report'
  title: string
  name: string
  result: ResultSet | null
  report: ReportView | null
}

interface ResultsState {
  tabs: ResultTab[]
  activeId: string | null
  filter: { col: number; text: string } | null
  sort: { index: number; dir: 1 | -1 } | null
  page: number
  /** 新开一个数据标签（AI 查询 / 结构预览）并激活 */
  push: (ds: Omit<ResultSet, 'id'>) => void
  /** 清空全部标签 */
  clear: () => void
  /** 新开一个报告标签并激活 */
  setReport: (r: Omit<ReportView, 'id'>) => void
  clearReport: () => void
  /** 追加/更新一个章节到活动报告标签（section 事件流式到达时调用） */
  upsertSection: (s: ReportSectionResult) => void
  /** 写入叙述 + 来源（活动报告标签） */
  setNarration: (text: string, refs: ReportView['refs']) => void
  closeTab: (id: string) => void
  activate: (id: string) => void
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

function resetViewport() {
  return { filter: null as { col: number; text: string } | null, sort: null as { index: number; dir: 1 | -1 } | null, page: 0 }
}

export const useResults = create<ResultsState>((set) => ({
  tabs: [],
  activeId: null,
  filter: null,
  sort: null,
  page: 0,

  push(ds) {
    const id = `res${++seq}`
    set((s) => ({
      tabs: [...s.tabs, { id, kind: 'data' as const, title: ds.title, name: ds.name, result: { ...ds, id }, report: null }],
      activeId: id,
      ...resetViewport()
    }))
  },

  clear() {
    set({ tabs: [], activeId: null, ...resetViewport() })
  },

  setReport(r) {
    const id = `rep${++rseq}`
    set((s) => ({
      tabs: [...s.tabs, { id, kind: 'report' as const, title: r.title, name: r.title, result: null, report: { ...r, id } }],
      activeId: id,
      ...resetViewport()
    }))
  },

  clearReport() {
    set((s) => ({
      tabs: s.tabs.filter((t) => t.kind !== 'report'),
      activeId: s.activeId && s.tabs.find((t) => t.id === s.activeId)?.kind === 'report' ? null : s.activeId
    }))
  },

  upsertSection(s) {
    set((st) => {
      const tab = st.tabs.find((t) => t.id === st.activeId)
      if (!tab || tab.kind !== 'report' || !tab.report) return {}
      const exists = tab.report.sections.findIndex((x) => x.id === s.id)
      const sections = exists >= 0
        ? tab.report.sections.map((x) => (x.id === s.id ? s : x))
        : [...tab.report.sections, s]
      return { tabs: st.tabs.map((t) => (t.id === tab.id ? { ...t, report: { ...tab.report!, sections } } : t)) }
    })
  },

  setNarration(text, refs) {
    set((st) => {
      const tab = st.tabs.find((t) => t.id === st.activeId)
      if (!tab || tab.kind !== 'report' || !tab.report) return {}
      return { tabs: st.tabs.map((t) => (t.id === tab.id ? { ...t, report: { ...tab.report!, narration: text, refs } } : t)) }
    })
  },

  closeTab(id) {
    set((s) => {
      const idx = s.tabs.findIndex((t) => t.id === id)
      if (idx < 0) return {}
      const tabs = s.tabs.filter((t) => t.id !== id)
      const activeId = s.activeId === id ? (tabs[idx]?.id ?? tabs[idx - 1]?.id ?? null) : s.activeId
      return { tabs, activeId, ...(s.activeId === id ? resetViewport() : {}) }
    })
  },

  activate(id) {
    set({ activeId: id, ...resetViewport() })
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
      const va = a[index]
      const vb = b[index]
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir
      return String(va).localeCompare(String(vb), undefined, { numeric: true }) * dir
    })
  }
  return rows
}
