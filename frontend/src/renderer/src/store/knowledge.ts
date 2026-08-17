import { create } from 'zustand'
import * as api from '@renderer/api/knowledge'
import type { KnowledgeOverview } from '@renderer/api/types'

interface KnowledgeState {
  overview: KnowledgeOverview | null
  loading: boolean
  busy: boolean
  error: string | null
  load: (connId: string) => Promise<void>
  build: (connId: string) => Promise<void>
  annotateTags: (connId: string) => Promise<void>
  confirmComment: (connId: string, table: string, column?: string) => Promise<void>
  rejectComment: (connId: string, table: string, column?: string) => Promise<void>
  confirmTag: (connId: string, name: string) => Promise<void>
  rejectTag: (connId: string, name: string) => Promise<void>
  assignTags: (connId: string, table: string, tags: string[]) => Promise<void>
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

  async build(connId) {
    set({ busy: true, error: null })
    try {
      await api.build(connId)
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
  }
}))
