import { create } from 'zustand'
import * as api from '@renderer/api/knowledge'
import type { KnowledgeOverview } from '@renderer/api/types'

interface BuildPhase {
  key: string
  label: string
  percent: number
  detail: string | null
  stage?: string | null
}
interface BuildProgressState {
  stage: string
  percent: number
  done: boolean
  error: string | null
  detail: string | null
  phases?: BuildPhase[]
}

interface KnowledgeState {
  overview: KnowledgeOverview | null
  loading: boolean
  busy: boolean
  error: string | null
  /** SSE 实时构建进度（无进度时 null） */
  buildProgress: BuildProgressState | null
  load: (connId: string) => Promise<void>
  /** 任务化构建：启动 + SSE 实时进度直到 done，然后刷新 overview */
  buildTask: (connId: string, onProgress?: (percent: number, stage: string) => void, opts?: { trigger?: 'init' | 'rebuild'; includeSamples?: boolean }) => Promise<void>
  /** 构建中刷新页面后重挂 SSE：只吃剩余进度（后端对无任务推 idle+done 帧后关闭） */
  reattachBuild: (connId: string) => Promise<void>
  confirmComment: (connId: string, table: string, column?: string) => Promise<void>
  rejectComment: (connId: string, table: string, column?: string) => Promise<void>
  confirmTag: (connId: string, name: string) => Promise<void>
  rejectTag: (connId: string, name: string) => Promise<void>
  assignTags: (connId: string, table: string, tags: string[]) => Promise<void>
  saveNote: (connId: string, table: string, note: string) => Promise<void>
  confirmAll: (connId: string) => Promise<void>
  /** 图谱编辑（持久化到知识库） */
  addEdge: (connId: string, edge: { from_table: string; to_table: string; from_col?: string | null; to_col?: string | null }) => Promise<void>
  removeEdge: (connId: string, edge: { from_table: string; to_table: string; kind: string }) => Promise<void>
  setExcluded: (connId: string, table: string, excluded: boolean) => Promise<void>
  /** LLM 图谱 draft 边确认/拒绝 */
  confirmGraphDraft: (connId: string, fromTable?: string | null) => Promise<void>
  rejectGraphDraft: (connId: string, fromTable?: string | null) => Promise<void>
}

/** fetch 流式解析 SSE（EventSource 不支持自定义 header，token 走 header） */
async function readBuildEvents(connId: string, onProgress: (p: BuildProgressState) => void): Promise<void> {
  const { getRuntime } = await import('@renderer/api/client')
  const token = getRuntime()?.token || localStorage.getItem('tt_token') || ''
  const res = await fetch(`/api/v1/knowledge/${connId}/build/events`, {
    headers: { 'X-TableTalk-Token': token, Accept: 'text/event-stream' },
  })
  if (!res.ok || !res.body) throw new Error('SSE 连接失败')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const parts = buf.split('\n\n')
    buf = parts.pop() ?? ''
    for (const part of parts) {
      const line = part.split('\n').find((l) => l.startsWith('data: '))
      if (!line) continue
      try {
        const p = JSON.parse(line.slice(6)) as BuildProgressState
        onProgress(p)
        if (p.done) return
      } catch { /* 忽略坏帧 */ }
    }
  }
}

export const useKnowledge = create<KnowledgeState>((set, get) => ({
  overview: null,
  loading: false,
  busy: false,
  error: null,
  buildProgress: null,

  async load(connId) {
    set({ loading: true, error: null })
    try {
      const ov = await api.overview(connId)
      set({ overview: ov })
    } catch (e) {
      set({ error: (e as Error).message })
    } finally {
      set({ loading: false })
    }
  },

  async buildTask(connId, onProgress, opts) {
    set({ busy: true, error: null, buildProgress: { stage: '排队中', percent: 0, done: false, error: null, detail: null } })
    try {
      await api.build(connId, opts?.includeSamples ?? false, opts?.trigger ?? 'init')
      // SSE 实时进度：推送即写 store（KbBuildGate 订阅显示），done 后退出
      await readBuildEvents(connId, (p) => {
        set({ buildProgress: p })
        onProgress?.(p.percent, p.stage)
        if (p.error && p.error !== 'cancelled') set({ error: p.error })
      })
      await get().load(connId)
    } catch (e) {
      set({ error: (e as Error).message })
    } finally {
      set({ busy: false, buildProgress: null })
    }
  },

  async reattachBuild(connId) {
    if (get().busy) return
    set({ busy: true, error: null, buildProgress: { stage: '排队中', percent: 0, done: false, error: null, detail: null } })
    try {
      await readBuildEvents(connId, (p) => {
        set({ buildProgress: p })
        if (p.error && p.error !== 'cancelled') set({ error: p.error })
      })
      await get().load(connId)
    } catch (e) {
      set({ error: (e as Error).message })
    } finally {
      set({ busy: false, buildProgress: null })
    }
  },

  async confirmComment(connId, table, column) {
    await api.confirmComment(connId, table, column)
    await get().load(connId)
  },

  async rejectComment(connId, table, column) {
    await api.rejectComment(connId, table, column)
    await get().load(connId)
  },

  async confirmTag(connId, name) {
    await api.confirmTag(connId, name)
    await get().load(connId)
  },

  async rejectTag(connId, name) {
    await api.rejectTag(connId, name)
    await get().load(connId)
  },

  async assignTags(connId, table, tags) {
    await api.assignTags(connId, table, tags)
    await get().load(connId)
  },

  async saveNote(connId, table, note) {
    await api.saveDoc(connId, table, note)
    await get().load(connId)
  },

  async confirmAll(connId) {
    await api.confirmAllEnabled(connId)
    await get().load(connId)
  },

  async addEdge(connId, edge) {
    const r = await api.addEdge(connId, edge)
    set((s) => s.overview ? {
      overview: { ...s.overview, graph: { ...s.overview.graph, edges: r.graph.edges } }
    } : {})
  },

  async removeEdge(connId, edge) {
    const r = await api.removeEdge(connId, edge)
    set((s) => s.overview ? {
      overview: { ...s.overview, graph: { ...s.overview.graph, edges: r.graph.edges } }
    } : {})
  },

  async setExcluded(connId, table, excluded) {
    const r = await api.setExcluded(connId, table, excluded)
    set((s) => s.overview ? {
      overview: { ...s.overview, graph: { ...s.overview.graph, excluded: r.excluded } }
    } : {})
  },

  async confirmGraphDraft(connId, fromTable) {
    set({ busy: true })
    try {
      const r = await api.confirmGraphDrafts(connId, fromTable)
      set((s) => s.overview ? {
        overview: { ...s.overview, graph: { ...s.overview.graph, llm_draft_edges: r.llm_draft_edges } }
      } : {})
    } finally {
      set({ busy: false })
    }
  },

  async rejectGraphDraft(connId, fromTable) {
    set({ busy: true })
    try {
      const r = await api.rejectGraphDrafts(connId, fromTable)
      set((s) => s.overview ? {
        overview: { ...s.overview, graph: { ...s.overview.graph, llm_draft_edges: r.llm_draft_edges } }
      } : {})
    } finally {
      set({ busy: false })
    }
  }
}))
