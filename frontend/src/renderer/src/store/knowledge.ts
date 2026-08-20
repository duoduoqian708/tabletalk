import { create } from 'zustand'
import * as api from '@renderer/api/knowledge'
import type { KnowledgeOverview } from '@renderer/api/types'

interface KnowledgeState {
  overview: KnowledgeOverview | null
  loading: boolean
  busy: boolean
  error: string | null
  load: (connId: string) => Promise<void>
  /** 任务化构建：启动 + 轮询进度直到 done，然后刷新 overview */
  buildTask: (connId: string, onProgress?: (percent: number, stage: string) => void) => Promise<void>
  annotateTags: (connId: string) => Promise<void>
  annotateEnums: (connId: string) => Promise<void>
  confirmComment: (connId: string, table: string, column?: string) => Promise<void>
  rejectComment: (connId: string, table: string, column?: string) => Promise<void>
  confirmTag: (connId: string, name: string) => Promise<void>
  rejectTag: (connId: string, name: string) => Promise<void>
  confirmEnum: (connId: string, table: string, column: string) => Promise<void>
  rejectEnum: (connId: string, table: string, column: string) => Promise<void>
  saveEnum: (connId: string, table: string, column: string, value: string, meaning: string) => Promise<void>
  assignTags: (connId: string, table: string, tags: string[]) => Promise<void>
  saveNote: (connId: string, table: string, note: string) => Promise<void>
  confirmAll: (connId: string) => Promise<void>
  /** 图谱编辑（持久化到知识库） */
  addEdge: (connId: string, edge: { from_table: string; to_table: string; from_col?: string | null; to_col?: string | null }) => Promise<void>
  removeEdge: (connId: string, edge: { from_table: string; to_table: string; kind: string }) => Promise<void>
  setExcluded: (connId: string, table: string, excluded: boolean) => Promise<void>
}

export const useKnowledge = create<KnowledgeState>((set, get) => ({
  overview: null,
  loading: false,
  busy: false,
  error: null,

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

  async buildTask(connId, onProgress) {
    set({ busy: true, error: null })
    try {
      await api.build(connId)
      // 轮询进度（任务化构建：后台跑，页面轮询）
      for (let i = 0; i < 600; i++) {
        await new Promise((r) => setTimeout(r, 500))
        const p = await api.buildProgress(connId)
        onProgress?.(p.percent, p.stage)
        if (p.done) {
          if (p.error && p.error !== 'cancelled') set({ error: p.error })
          break
        }
      }
      await get().load(connId)
    } catch (e) {
      set({ error: (e as Error).message })
    } finally {
      set({ busy: false })
    }
  },

  async annotateTags(connId) {
    set({ busy: true })
    try {
      await api.annotateTags(connId)
      await get().load(connId)
    } finally {
      set({ busy: false })
    }
  },

  async annotateEnums(connId) {
    set({ busy: true })
    try {
      await api.annotateEnums(connId)
      await get().load(connId)
    } finally {
      set({ busy: false })
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

  async confirmEnum(connId, table, column) {
    await api.confirmEnum(connId, table, column)
    await get().load(connId)
  },

  async rejectEnum(connId, table, column) {
    await api.rejectEnum(connId, table, column)
    await get().load(connId)
  },

  async saveEnum(connId, table, column, value, meaning) {
    await api.saveEnum(connId, table, column, value, meaning)
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
  }
}))
