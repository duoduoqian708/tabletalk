import { create } from 'zustand'

export type View = 'workspace' | 'knowledge' | 'graph' | 'audit'
export type MainView = 'graph' | 'table'

interface UiState {
  view: View
  setView: (v: View) => void
  /** 主区双视图：图谱 / 表格 */
  mainView: MainView
  setMainView: (v: MainView) => void
  /** AI 输入草稿（图谱"问 AI 这张表"→ 预填输入框） */
  askDraft: string | null
  setAskDraft: (t: string | null) => void
}

export const useUi = create<UiState>((set) => ({
  view: 'workspace',
  setView: (v) => set({ view: v }),
  mainView: 'graph',
  setMainView: (v) => set({ mainView: v }),
  askDraft: null,
  setAskDraft: (t) => set({ askDraft: t })
}))
