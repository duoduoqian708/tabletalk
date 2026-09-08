import { create } from 'zustand'
import * as api from '@renderer/api/knowledge'
import type { KnowledgeOverview } from '@renderer/api/types'

interface BuildStepMeta {
  key: string
  label: string
}
interface BuildPhase {
  key: string
  label: string
  percent: number
  detail: string | null
  stage?: string | null
  /** 当前子步（per_table/partition/global）+ 进度 */
  step?: string | null
  step_label?: string | null
  step_index?: number | null
  step_total?: number | null
  steps?: BuildStepMeta[] | null
  /** LLM 调用期间进度不动但跑光动画 */
  busy?: boolean
  /** 流式生成尾巴（单行 LLM 思考/正文尾文，2026-09）：进度条下的"在输出"证据 */
  live?: string | null
}
interface BuildProgressState {
  stage: string
  percent: number
  done: boolean
  error: string | null
  detail: string | null
  /** 任务类型（2026-09：build | sync，前端区分展示） */
  kind?: string
  phases?: BuildPhase[]
  /** 失败/取消定位：出错的阶段与子步（兼容旧帧） */
  error_at?: { phase?: string | null; step?: string | null } | null
}

interface KnowledgeState {
  overview: KnowledgeOverview | null
  loading: boolean
  busy: boolean
  error: string | null
  /** SSE 实时构建进度（无进度时 null）；connId 标记属于哪个连接，切连接不串台 */
  buildProgress: (BuildProgressState & { connId: string }) | null
  load: (connId: string) => Promise<void>
  /** 任务化构建：启动 + SSE 实时进度直到 done，然后刷新 overview */
  buildTask: (connId: string, onProgress?: (percent: number, stage: string) => void, opts?: { trigger?: 'init' | 'rebuild'; includeSamples?: boolean; annotateMode?: 'diff' | 'full'; tagMode?: 'keep' | 'anchor' | 'fresh' }) => Promise<void>
  /** 构建中刷新页面后重挂 SSE：只吃剩余进度（后端对无任务推 idle+done 帧后关闭） */
  reattachBuild: (connId: string) => Promise<void>
  /** 任务化增量同步（已发起 job 后调用）：接同一 SSE 通道吃进度直到 done，然后刷新 overview */
  syncTask: (connId: string) => Promise<void>
  confirmComment: (connId: string, table: string, column?: string | null, tableOnly?: boolean) => Promise<void>
  rejectComment: (connId: string, table: string, column?: string) => Promise<void>
  confirmTag: (connId: string, name: string) => Promise<void>
  rejectTag: (connId: string, name: string) => Promise<void>
  assignTags: (connId: string, table: string, tags: string[]) => Promise<void>
  saveNote: (connId: string, table: string, note: string) => Promise<void>
  /** 放弃本轮全部草案（撤草案保历史）：后端撤下 draft 并按有无 confirmed 置 ready/none */
  discardAll: (connId: string) => Promise<void>
  /** 关闭错误条（手机锁屏恢复等通道级抖动的残留提示，用户可一键清掉） */
  dismissError: () => void
  /** 图谱编辑（持久化到知识库） */
  addEdge: (connId: string, edge: { from_table: string; to_table: string; from_col?: string | null; to_col?: string | null; cardinality?: 'n:1' | '1:1' | '1:N' | 'N:M'; guard?: string | null; extra_cols?: [string, string][] }) => Promise<void>
  removeEdge: (connId: string, edge: { from_table: string; to_table: string; source: string }) => Promise<void>
  setExcluded: (connId: string, table: string, excluded: boolean) => Promise<void>
  /** 2D 图布局持久化（拖拽松手全量快照 → payload.layout） */
  saveLayout: (connId: string, layout: Record<string, { x: number; y: number }>) => Promise<void>
  /** LLM 图谱 draft 边确认/拒绝 */
  confirmGraphDraft: (connId: string, fromTable?: string | null) => Promise<void>
  rejectGraphDraft: (connId: string, fromTable?: string | null) => Promise<void>
}

/** 构建进度观测日志（prod 也开）：低频生命周期点，断线排查的前端证据源 */
function kbLog(...args: unknown[]): void {
  console.log('[kb]', ...args)
}

/** 网络层错误（手机锁屏挂起/切网瞬间的 fetch 中断）：进度有轮询兜底、任务在后端活着，不算失败 */
function isNetworkError(e: unknown): boolean {
  const err = e as Error
  const msg = err?.message ?? ''
  return err?.name === 'TypeError' || /failed to fetch|networkerror|load failed/i.test(msg)
}

/** overview 拉取抗抖：手机锁屏恢复/弱网瞬间的失败静默重试（1s/3s/5s 退避，共 ~9s 窗口），
    全失败才上抛——2026-09 从 [0,300,900] 加长：手机网络恢复窗口 1.2s 远不够（生效后 stale 根因之一） */
async function loadOverviewWithRetry(connId: string): Promise<KnowledgeOverview> {
  let lastErr: unknown
  for (const delay of [0, 1000, 3000, 5000]) {
    if (delay) await new Promise((r) => setTimeout(r, delay))
    try {
      return await api.overview(connId)
    } catch (e) {
      lastErr = e
      kbLog(`overview 拉取失败（第 ${delay ? '重试' : '首次'}）：${(e as Error).message}`)
    }
  }
  throw lastErr
}

/** fetch 流式解析 SSE（EventSource 不支持自定义 header，token 走 header） */
async function readBuildEvents(connId: string, onProgress: (p: BuildProgressState) => void): Promise<void> {
  try {
    await readBuildEventsSse(connId, onProgress)
  } catch (e) {
    // SSE 断线 ≠ 构建失败：后端任务照跑（jobs 常驻内存），此处静默降级为轮询直到终态。
    // 只有后端推来的 error 帧才算真失败（onProgress 已把它写进 store）。
    const err = e as Error
    kbLog(`SSE 断开（${err.name}: ${err.message}）→ 降级轮询 /build/progress`)
    await pollBuildProgress(connId, onProgress)
  }
}

/** SSE 断线后的兜底：轮询 build/progress（与 SSE 帧同一份 job.progress，含 phases） */
async function pollBuildProgress(connId: string, onProgress: (p: BuildProgressState) => void): Promise<void> {
  const started = Date.now()
  for (;;) {
    let p: BuildProgressState
    try {
      p = await api.buildProgress(connId)
    } catch (e) {
      // 轮询也断（sidecar 重启等）：job 已死（内存态），视为终态结束，交由上层刷新状态
      kbLog(`轮询失败（${(e as Error).message}）→ 视为通道终止（进程可能重启，构建已失）`)
      return
    }
    onProgress(p)
    if (p.done) {
      kbLog(`轮询终态：${p.error ? `error=${p.error}` : 'done'}（降级耗时 ${((Date.now() - started) / 1000).toFixed(0)}s）`)
      return
    }
    await new Promise((r) => setTimeout(r, 1500))
  }
}

/** SSE 读流主体：正常收到 done → 返回；任何异常上抛（由 readBuildEvents 降级轮询接管） */
async function readBuildEventsSse(connId: string, onProgress: (p: BuildProgressState) => void): Promise<void> {
  const { getRuntime } = await import('@renderer/api/client')
  const token = getRuntime()?.token || localStorage.getItem('tt_token') || ''
  const res = await fetch(`/api/v1/knowledge/${connId}/build/events`, {
    headers: { 'X-TableTalk-Token': token, Accept: 'text/event-stream' },
  })
  if (!res.ok || !res.body) throw new Error(`SSE 连接失败 HTTP ${res.status}`)
  kbLog('SSE 已连接（构建进度流）')
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let gotDone = false
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const parts = buf.split('\n\n')
    buf = parts.pop() ?? ''
    for (const part of parts) {
      const line = part.split('\n').find((l) => l.startsWith('data: '))
      if (!line) continue
      try {
        const p = JSON.parse(line.slice(6)) as BuildProgressState
        onProgress(p)
        if (p.done) {
          gotDone = true
          if (p.error) kbLog(`SSE 终态：构建失败 error=${p.error}`)
          else kbLog('SSE 终态：构建完成')
          return
        }
      } catch { /* 忽略坏帧 */ }
    }
  }
  // 流正常关但没收到 done（后端流被掐/代理截断）→ 也视为断线，交降级轮询
  if (!gotDone) throw new Error('SSE 流提前关闭（未收到 done）')
}

export const useKnowledge = create<KnowledgeState>((set, get) => ({
  overview: null,
  loading: false,
  busy: false,
  error: null,
  buildProgress: null,

  async load(connId) {
    set({ loading: true, error: null })
    try {
      const ov = await loadOverviewWithRetry(connId)
      set({ overview: ov })
    } catch (e) {
      set({ error: (e as Error).message })
    } finally {
      set({ loading: false })
    }
  },

  async buildTask(connId, onProgress, opts) {
    set({ busy: true, error: null, buildProgress: { connId, stage: '排队中', percent: 0, done: false, error: null, detail: null } })
    let started = false
    try {
      await api.build(
        connId,
        opts?.includeSamples ?? false,
        opts?.trigger ?? 'init',
        opts?.annotateMode ?? 'diff',
        opts?.tagMode ?? 'keep'
      )
      started = true
      // SSE 实时进度：推送即写 store（KbBuildGate 订阅显示），done 后退出
      await readBuildEvents(connId, (p) => {
        set({ buildProgress: { ...p, connId } })
        onProgress?.(p.percent, p.stage)
        if (p.error && p.error !== 'cancelled') set({ error: p.error })
      })
      await get().load(connId)
    } catch (e) {
      kbLog(`buildTask 异常：${(e as Error).name}: ${(e as Error).message}`)
      // 启动失败（构建没起来）= 真失败照常展示；启动后的通道级网络错误只记日志
      if (!started || !isNetworkError(e)) set({ error: (e as Error).message })
    } finally {
      set({ busy: false, buildProgress: null })
    }
  },

  async reattachBuild(connId) {
    if (get().busy) return
    set({ busy: true, error: null, buildProgress: { connId, stage: '排队中', percent: 0, done: false, error: null, detail: null } })
    try {
      await readBuildEvents(connId, (p) => {
        set({ buildProgress: { ...p, connId } })
        if (p.error && p.error !== 'cancelled') set({ error: p.error })
      })
      await get().load(connId)
    } catch (e) {
      kbLog(`reattachBuild 异常：${(e as Error).name}: ${(e as Error).message}`)
      if (!isNetworkError(e)) set({ error: (e as Error).message })
    } finally {
      set({ busy: false, buildProgress: null })
    }
  },

  async syncTask(connId) {
    if (get().busy) return
    set({ busy: true, error: null, buildProgress: { connId, stage: '排队中', percent: 0, done: false, error: null, detail: null } })
    try {
      await readBuildEvents(connId, (p) => {
        set({ buildProgress: { ...p, connId } })
        if (p.error && p.error !== 'cancelled') set({ error: p.error })
      })
      await get().load(connId)
    } catch (e) {
      kbLog(`syncTask 异常：${(e as Error).name}: ${(e as Error).message}`)
      if (!isNetworkError(e)) set({ error: (e as Error).message })
    } finally {
      set({ busy: false, buildProgress: null })
    }
  },

  async confirmComment(connId, table, column, tableOnly) {
    await api.confirmComment(connId, table, column ?? undefined, tableOnly)
    await get().load(connId)
  },

  async rejectComment(connId, table, column) {
    await api.rejectComment(connId, table, column)
    await get().load(connId)
  },

  async confirmTag(connId, name) {
    await api.confirmTag(connId, name)
    await get().load(connId)
  },

  async rejectTag(connId, name) {
    await api.rejectTag(connId, name)
    await get().load(connId)
  },

  async assignTags(connId, table, tags) {
    await api.assignTags(connId, table, tags)
    await get().load(connId)
  },

  async saveNote(connId, table, note) {
    await api.saveDoc(connId, table, note)
    await get().load(connId)
  },

  async discardAll(connId) {
    await api.discardKb(connId)
    await get().load(connId)
  },

  dismissError() {
    set({ error: null })
  },

  async addEdge(connId, edge) {
    // 合并列对：from_col/to_col(第一对) + extra_cols → cols(完整列表)传后端
    const { extra_cols, from_col, to_col, ...rest } = edge
    const cols: [string, string][] | null =
      extra_cols && extra_cols.length ? [[from_col || '', to_col || ''], ...extra_cols] : null
    const r = await api.addEdge(connId, { ...rest, from_col, to_col, cols })
    set((s) => s.overview ? {
      overview: { ...s.overview, graph: { ...s.overview.graph, edges: r.graph.edges } }
    } : {})
  },

  async removeEdge(connId, edge) {
    const r = await api.removeEdge(connId, edge)
    set((s) => s.overview ? {
      overview: { ...s.overview, graph: { ...s.overview.graph, edges: r.graph.edges } }
    } : {})
  },

  async saveLayout(connId, layout) {
    const r = await api.putGraphLayout(connId, layout)
    set((s) => s.overview ? {
      overview: { ...s.overview, graph: { ...s.overview.graph, layout: r.layout } }
    } : {})
  },

  async setExcluded(connId, table, excluded) {
    const r = await api.setExcluded(connId, table, excluded)
    set((s) => s.overview ? {
      overview: { ...s.overview, graph: { ...s.overview.graph, excluded: r.excluded } }
    } : {})
  },

  async confirmGraphDraft(connId, fromTable) {
    set({ busy: true })
    try {
      const r = await api.confirmGraphDrafts(connId, fromTable)
      set((s) => s.overview ? {
        overview: {
          ...s.overview,
          graph: { ...s.overview.graph, llm_draft_edges: r.llm_draft_edges, edges: r.edges },
        }
      } : {})
    } finally {
      set({ busy: false })
    }
  },

  async rejectGraphDraft(connId, fromTable) {
    set({ busy: true })
    try {
      const r = await api.rejectGraphDrafts(connId, fromTable)
      set((s) => s.overview ? {
        overview: {
          ...s.overview,
          graph: { ...s.overview.graph, llm_draft_edges: r.llm_draft_edges, edges: r.edges },
        }
      } : {})
    } finally {
      set({ busy: false })
    }
  }
}))
