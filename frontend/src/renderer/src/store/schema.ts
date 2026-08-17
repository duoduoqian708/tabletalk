import { create } from 'zustand'
import type { SchemaResponse } from '@renderer/api/types'
import * as api from '@renderer/api/schema'

interface SchemaState {
  data: SchemaResponse | null
  selectedTable: string | null
  expandedTables: string[]
  loading: boolean
  error: string | null
  load: (connId: string) => Promise<void>
  selectTable: (t: string | null) => void
  toggleExpand: (t: string) => void
  expandAll: () => void
  collapseAll: () => void
}

export const useSchema = create<SchemaState>((set) => ({
  data: null,
  selectedTable: null,
  expandedTables: [],
  loading: false,
  error: null,

  async load(connId) {
    set({ loading: true, error: null })
    try {
      const data = await api.getSchema(connId)
      set({ data, selectedTable: null, expandedTables: [] })
    } catch (e) {
      set({ error: (e as Error).message })
    } finally {
      set({ loading: false })
    }
  },

  selectTable(t) {
    set({ selectedTable: t })
  },

  toggleExpand(t) {
    set((s) => ({
      expandedTables: s.expandedTables.includes(t)
        ? s.expandedTables.filter((x) => x !== t)
        : [...s.expandedTables, t]
    }))
  },

  expandAll() {
    set((s) => ({
      expandedTables: s.data?.tables.map((t) => t.name) ?? s.expandedTables
    }))
  },

  collapseAll() {
    set({ expandedTables: [] })
  }
}))
