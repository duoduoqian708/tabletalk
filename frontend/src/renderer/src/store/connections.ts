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
  /** 启动时以后端持久化的默认数据源覆盖本地（后端为事实源，跨浏览器/origin 一致） */
  applyBackendDefault: (id: string | null) => void
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
      // 清理默认残留（state + localStorage + 后端），避免脏 id 下次启动走回退
      const wasDefault = get().defaultId === id
      if (wasDefault) {
        try { localStorage.removeItem(DEFAULT_KEY) } catch { /* ignore */ }
        void import('@renderer/api/settings').then(({ updateSettings }) =>
          updateSettings({ default_connection: '' })
        ).catch(() => undefined)
      }
      set((s) => ({
        list: s.list.filter((c) => c.id !== id),
        currentId: s.currentId === id ? (s.list.find((c) => c.id !== id)?.id ?? null) : s.currentId,
        defaultId: wasDefault ? null : s.defaultId
      }))
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
    // 后端持久化（事实源，跨浏览器/origin 一致）；空串=清除默认。失败不打断本地体验。
    void import('@renderer/api/settings').then(({ updateSettings }) =>
      updateSettings({ default_connection: id || '' })
    ).catch(() => undefined)
  },

  applyBackendDefault(id) {
    if (!id) return  // 后端未设置 → 保留 localStorage 降级值（兼容本功能上线前的旧设置）
    try { localStorage.setItem(DEFAULT_KEY, id) } catch { /* ignore */ }
    set((s) => (s.defaultId === id ? {} : { defaultId: id, currentId: id }))
  },

  clearError() {
    set({ error: null })
  }
}))