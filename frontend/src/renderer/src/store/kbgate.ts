import { create } from 'zustand'

interface KbGateState {
  /** 被强制打开构建门禁的连接（AI/查询收到 kb_not_built 时置位） */
  forceConnId: string | null
  forceOpen: (connId: string) => void
  clearForce: () => void
}

export const useKbGate = create<KbGateState>((set) => ({
  forceConnId: null,
  forceOpen: (connId) => set({ forceConnId: connId }),
  clearForce: () => set({ forceConnId: null })
}))
