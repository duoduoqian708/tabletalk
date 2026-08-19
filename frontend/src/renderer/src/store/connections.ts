import { create } from 'zustand'
import type { ConnectionConfig } from '@renderer/api/types'
import * as api from '@renderer/api/connections'

interface ConnectionsState {
  list: ConnectionConfig[]
  currentId: string | null
  loading: boolean
  error: string | null
  load: () => Promise<void>
  create: (input: api.ConnectionInput) => Promise<ConnectionConfig | null>
  update: (id: string, patch: Partial<api.ConnectionInput>) => Promise<void>
  remove: (id: string) => Promise<void>
  select: (id: string | null) => void
  clearError: () => void
}

export const useConnections = create<ConnectionsState>((set, get) => ({
  list: [],
  currentId: null,
  loading: false,
  error: null,

  async load() {
    set({ loading: true, error: null })
    try {
      const list = await api.listConnections()
      const cur = get().currentId
      const currentId = cur && list.some((c) => c.id === cur) ? cur : (list[0]?.id ?? null)
      set({ list, currentId })
    } catch (e) {
      set({ error: (e as Error).message })
    } finally {
      set({ loading: false })
    }
  },

  async create(input) {
    try {
      const cfg = await api.createConnection(input)
      set((s) => ({ list: [...s.list, cfg], currentId: cfg.id, error: null }))
      return cfg
    } catch (e) {
      set({ error: (e as Error).message })
      return null
    }
  },

  async update(id, patch) {
    try {
      const cfg = await api.updateConnection(id, patch)
      set((s) => ({ list: s.list.map((c) => (c.id === id ? cfg : c)), error: null }))
    } catch (e) {
      set({ error: (e as Error).message })
    }
  },

  async remove(id) {
    try {
      await api.deleteConnection(id)
      set((s) => {
        const list = s.list.filter((c) => c.id !== id)
        return {
          list,
          currentId: s.currentId === id ? (list[0]?.id ?? null) : s.currentId
        }
      })
    } catch (e) {
      set({ error: (e as Error).message })
    }
  },

  select(id) {
    set({ currentId: id })
  },

  clearError() {
    set({ error: null })
  }
}))
