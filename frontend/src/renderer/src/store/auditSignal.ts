import { create } from 'zustand'
import { auditSignal } from '@renderer/api/audit'
import { useConnections } from '@renderer/store/connections'

/** 轮询间隔：用户要求易于调整（可能改 10s），改这一个常量即可 */
const SIGNAL_POLL_INTERVAL = 30_000

interface SignalState {
  unread: number
  pending: number
  todayBlocked: number
  todayReview: number
  refresh: () => Promise<void>
  start: () => void
  stop: () => void
}

let timer: number | undefined

export const useAuditSignal = create<SignalState>((set) => ({
  unread: 0,
  pending: 0,
  todayBlocked: 0,
  todayReview: 0,
  refresh: async () => {
    try {
      const { currentId, list } = useConnections.getState()
      const connName = list.find((c) => c.id === currentId)?.name
      const s = await auditSignal(connName)
      set({ unread: s.unread_exceptions, pending: s.pending_approvals, todayBlocked: s.today.blocked, todayReview: s.today.review })
    } catch { /* 静默降级：徽章保持上次值 */ }
  },
  start: () => {
    if (timer !== undefined) return
    void useAuditSignal.getState().refresh()
    timer = window.setInterval(() => void useAuditSignal.getState().refresh(), SIGNAL_POLL_INTERVAL)
  },
  stop: () => {
    if (timer !== undefined) { window.clearInterval(timer); timer = undefined }
  },
}))
