import { create } from 'zustand'
import type { AiCard } from '@renderer/api/ai'

/* 对话消息轮次（可序列化，存 localStorage） */
export interface Turn {
  role: 'user' | 'ai'
  text?: string
  question?: string
  thinks?: string[]
  cards?: AiCard[]
  steps?: { id: string; label: string; status: string; detail: string[] }[]
  pending?: boolean
  running?: boolean
  /** 报告模式 turn 标记：表明这是报告流（澄清/计划/章节走 results store.report） */
  isReport?: boolean
  /** 报告澄清中：渲染澄清输入，答完作为历史重传 */
  clarify?: string[]
}

export interface Conversation {
  id: string
  connId: string
  title: string | null
  updatedAt: number
  turns: Turn[]
}

const LS_KEY = 'cleared-chats-v1'

function load(): Conversation[] {
  try {
    const raw = localStorage.getItem(LS_KEY)
    return raw ? (JSON.parse(raw) as Conversation[]) : []
  } catch {
    return []
  }
}

interface ChatState {
  conversations: Conversation[]
  activeId: string | null
  newConversation: (connId: string) => string
  rename: (id: string, title: string) => void
  /** 持久化轮次内容，**不**刷新 updatedAt（查看/恢复历史不更新时间） */
  saveTurns: (id: string, turns: Turn[]) => void
  /** 提问时调用：刷新会话更新时间 */
  touch: (id: string) => void
  select: (id: string) => void
}

export const useChat = create<ChatState>((set) => ({
  conversations: load(),
  activeId: null,

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

/** 从首问语义生成对话标题（规则映射 + 截断兜底）。 */
export function generateTitle(q: string): string {
  const s = q.toLowerCase()
  const rules: [RegExp, string][] = [
    [/退货|退款|return/, '商品退货率分析'],
    [/提价|涨价|调价|价格/, '商品调价'],
    [/删除|delete|删/, '数据删除操作'],
    [/索引|建表|删表|alter|ddl/, '表结构变更'],
    [/客户|clv|生命周期|价值/, '客户价值分析'],
    [/库存|积压|周转|inventory/, '库存周转分析'],
    [/销售|sales|revenue/, '销售数据分析'],
    [/字段|列|结构|describe/, '表结构查询'],
    [/报告|报表|概览|趋势|分析报告/, '数据分析报告']
  ]
  for (const [re, t] of rules) {
    if (re.test(s)) return t
  }
  return q.replace(/[？?。.!！\s]+$/, '').slice(0, 16)
}

export function relTime(ts: number): string {
  const d = Date.now() - ts
  if (d < 60_000) return '刚刚'
  if (d < 3_600_000) return `${Math.floor(d / 60_000)} 分钟前`
  if (d < 86_400_000) return `${Math.floor(d / 3_600_000)} 小时前`
  return `${Math.floor(d / 86_400_000)} 天前`
}
