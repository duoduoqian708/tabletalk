import { create } from 'zustand'

interface KbGateState {
  /** 被强制打开构建门禁的连接（AI/查询收到 kb_not_built 时置位） */
  forceConnId: string | null
  /** 受控构建确认弹窗；null=关闭。trigger 决定文案（开始构建/开始全部重构） */
  buildTrigger: 'init' | 'rebuild' | null
  /** 请求打开审核镜头的连接（构建完成 CTA「去审核」置位，知识库页消费后清除） */
  reviewWantedConnId: string | null
  forceOpen: (connId: string) => void
  clearForce: () => void
  openBuildDialog: (trigger: 'init' | 'rebuild') => void
  closeBuildDialog: () => void
  requestReview: (connId: string) => void
  consumeReview: (connId: string) => void
}

export const useKbGate = create<KbGateState>((set) => ({
  forceConnId: null,
  buildTrigger: null,
  reviewWantedConnId: null,
  forceOpen: (connId) => set({ forceConnId: connId }),
  clearForce: () => set({ forceConnId: null }),
  openBuildDialog: (trigger) => set({ buildTrigger: trigger }),
  closeBuildDialog: () => set({ buildTrigger: null }),
  requestReview: (connId) => set({ reviewWantedConnId: connId }),
  consumeReview: (connId) => set((s) => (s.reviewWantedConnId === connId ? { reviewWantedConnId: null } : {})),
}))
