import { create } from 'zustand'
import type { AiCard } from '@renderer/api/ai'
import { useI18n } from '@renderer/store/i18n'

/* 对话消息轮次（可序列化，存 localStorage） */
export interface Subtask {
  id: string
  tool: string
  label: string
  status: string
  details: string[]
  blocks: import('@renderer/api/ai').Block[]
}
export interface Turn {
  role: 'user' | 'ai'
  text?: string
  question?: string
  thinks?: string[]
  cards?: AiCard[]
  steps?: { id: string; label: string; status: string; detail: string[] }[]
  subtasks?: Subtask[]
  scene?: string
  /** 2026-09 §19.6 实时任务流：任务级树（task_/subtask_ 事件驱动） */
  tasks?: import('@renderer/api/ai').AiTaskFlow[]
  /** 意图澄清候选问题（点击=续问 new_question） */
  clarifyOptions?: string[]
  pending?: boolean
  running?: boolean
  /** 报告模式 turn 标记：表明这是报告流（澄清/计划/章节走 results store.report） */
  isReport?: boolean
  /** 报告澄清中：渲染澄清输入，答完作为历史重传 */
  clarify?: string[]
  /** B1 出网清单 */
  manifest?: import('@renderer/api/ai').Manifest
  /** WS4 会话 id：用于 DML 确认 token 校验 */
  sessionId?: string | null
}

export interface Conversation {
  id: string
  connId: string
  title: string | null
  updatedAt: number
  turns: Turn[]
}

export type TrustLevel = 'all_confirm' | 'read_auto' | 'max_trust'

const LS_KEY = 'tabletalk-chats-v1'
const LS_KEY_LEGACY = 'cleared-chats-v1'

function load(): Conversation[] {
  try {
    let raw = localStorage.getItem(LS_KEY)
    if (!raw) {
      // 旧键兼容：读入旧会话并迁移到新键（一次性）
      raw = localStorage.getItem(LS_KEY_LEGACY)
      if (raw) {
        localStorage.setItem(LS_KEY, raw)
        localStorage.removeItem(LS_KEY_LEGACY)
      }
    }
    return raw ? (JSON.parse(raw) as Conversation[]) : []
  } catch {
    return []
  }
}

interface ChatState {
  conversations: Conversation[]
  activeId: string | null
  /** 会话信任级别（HITL 介入粒度）：all_confirm 全部介入 / read_auto 读自动·写确认 / max_trust 读写自动（V0.5 仅前两档可用） */
  trustLevel: TrustLevel
  setTrustLevel: (level: TrustLevel) => void
  newConversation: (connId: string) => string
  rename: (id: string, title: string) => void
  /** 持久化轮次内容，**不**刷新 updatedAt（查看/恢复历史不更新时间） */
  saveTurns: (id: string, turns: Turn[]) => void
  /** 提问时调用：刷新会话更新时间 */
  touch: (id: string) => void
  select: (id: string) => void
}

const TRUST_KEY = 'tabletalk-trust-level'

export const useChat = create<ChatState>((set) => ({
  conversations: load(),
  activeId: null,
  trustLevel: (localStorage.getItem(TRUST_KEY) as TrustLevel) || 'read_auto',

  setTrustLevel(level) {
    localStorage.setItem(TRUST_KEY, level)
    set({ trustLevel: level })
  },

  newConversation(connId) {
    const id = `c${Date.now().toString(36)}${Math.floor(Math.random() * 1e4).toString(36)}`
    const conv: Conversation = { id, connId, title: null, updatedAt: Date.now(), turns: [] }
    set((s) => ({ conversations: [conv, ...s.conversations].slice(0, 30), activeId: id }))
    return id
  },

  rename(id, title) {
    set((s) => ({
      conversations: s.conversations.map((c) => (c.id === id ? { ...c, title } : c))
    }))
  },

  saveTurns(id, turns) {
    set((s) => ({
      conversations: s.conversations.map((c) => (c.id === id ? { ...c, turns } : c))
    }))
  },

  touch(id) {
    set((s) => ({
      conversations: s.conversations.map((c) =>
        c.id === id ? { ...c, updatedAt: Date.now() } : c
      )
    }))
  },

  select(id) {
    set({ activeId: id })
  }
}))

/* 持久化（订阅保存，简单可靠） */
useChat.subscribe((s) => {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(s.conversations))
  } catch {
    /* 存储满等场景静默 */
  }
})

/** 从首问生成对话标题（通用机制：去掉语气词/句尾标点，截取前 N 个有效字符，零业务定制）。 */
export function generateTitle(q: string): string {
  const clean = q
    .replace(/[？?。.!！，,、\s]+$/g, '')          // 去句尾标点/词气
    .replace(/^(请|帮我|麻烦|给我|我想(要|看|知道|查)?|请问|帮我查|帮我分析)\s*/g, '') // 去常见祈使/语气前缀
  if (!clean) return useI18n.getState().t('chat.newConversation')
  return clean.length > 16 ? `${clean.slice(0, 16)}…` : clean
}

export function relTime(ts: number): string {
  const t = useI18n.getState().t
  const d = Date.now() - ts
  if (d < 60_000) return t('chat.justNow')
  if (d < 3_600_000) return t('chat.minutesAgo', { n: Math.floor(d / 60_000) })
  if (d < 86_400_000) return t('chat.hoursAgo', { n: Math.floor(d / 3_600_000) })
  return t('chat.daysAgo', { n: Math.floor(d / 86_400_000) })
}
