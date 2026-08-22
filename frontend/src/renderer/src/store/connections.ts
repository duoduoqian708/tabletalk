import { create } from 'zustand'
import type { ConnectionConfig } from '@renderer/api/types'
import * as api from '@renderer/api/connections'

const DEFAULT_KEY = 'tabletalk-default-conn'

function getDefault(): string | null {
  try { return localStorage.getItem(DEFAULT_KEY) } catch { return null }
}

interface ConnectionsState {
  list: ConnectionConfig[]
  currentId: string | null
  defaultId: string | null
  loading: boolean
  error: string | null
  load: () => Promise<void>
  create: (input: api.ConnectionInput) => Promise<ConnectionConfig | null>
  update: (id: string, patch: Partial<api.ConnectionInput>) => Promise<void>
  remove: (id: string) => Promise<void>
  select: (id: string | null) => void
  setDefault: (id: string | null) => void
  clearError: () => void
}

export const useConnections = create<ConnectionsState>((set, get) => ({
  list: [],
  currentId: null,
  defaultId: getDefault(),
  loading: false,
  error: null,

  async load() {
    set({ loading: true, error: null })
    try {
      const list = await api.listConnections()
      const def = get().defaultId
      const cur = get().currentId
      // 优先级：默认数据源 > 当前选中 > 列表第一个
      const currentId = def && list.some((c) => c.id === def)
        ? def
        : (cur && list.some((c) => c.id === cur) ? cur : (list[0]?.id ?? null))
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
          currentId: s.currentId === id ? (list[0]?.id ?? null) : s.currentId,
          defaultId: s.defaultId === id ? null : s.defaultId
        }
      })
    } catch (e) {
      set({ error: (e as Error).message })
    }
  },

  select(id) {
    set({ currentId: id })
  },

  setDefault(id) {
    try {
      if (id) localStorage.setItem(DEFAULT_KEY, id)
      else localStorage.removeItem(DEFAULT_KEY)
    } catch { /* ignore */ }
    set((s) => ({
      defaultId: id,
      // 设为默认即点亮：默认数据源同时成为工作台当前数据源（无需等下次启动）
      currentId: id ?? s.currentId
    }))
  },

  clearError() {
    set({ error: null })
  }
}))