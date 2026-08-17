import { create } from 'zustand'

export type View = 'workspace' | 'gate' | 'knowledge' | 'graph' | 'audit'

interface UiState {
  view: View
  setView: (v: View) => void
}

export const useUi = create<UiState>((set) => ({
  view: 'workspace',
  setView: (v) => set({ view: v })
}))
