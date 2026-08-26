import { useCallback, useMemo } from 'react'
import type { GraphEdge, GraphLayout, KnowledgeOverview } from '@renderer/api/types'
import type { Trg2dAddEdge, Trg2dDeleteEdge, Trg2dTable } from '@renderer/components/TableRelationGraph2D'
import { useKnowledge } from '@renderer/store/knowledge'
import { useI18n } from '@renderer/store/i18n'
import { toastMsg } from '@renderer/utils/toast'

/* ═══════════════════════════════════════════════
   TableRelationGraph2D × 知识库双宿主共享接线
   （审阅弹窗右栏 + 知识库编辑页 2D 形态）
   同一数据源（overview）+ 同一组 action，宿主只管摆组件
   ═══════════════════════════════════════════════ */

/** overview.tables → 组件节点（首标签聚类 + 排除灰显 + 规模驱动半径） */
export function trgTables(ov: KnowledgeOverview): Trg2dTable[] {
  return ov.tables.map((tb) => ({
    name: tb.name,
    tags: tb.tags.map((tg) => tg.name),
    excluded: tb.excluded,
    size: tb.column_count,
  }))
}

/** graph.edges + llm_draft_edges → 组件边（draft 边 kind=llm + status=draft 虚线琥珀） */
export function trgEdges(ov: KnowledgeOverview): GraphEdge[] {
  const drafts: GraphEdge[] = (ov.graph.llm_draft_edges ?? []).map((d) => ({
    from: d.from_table, from_col: d.from_col,
    to: d.to_table, to_col: d.to_col,
    kind: 'llm', status: 'draft', reason: d.reason,
  }))
  return [...(ov.graph.edges ?? []), ...drafts]
}

/** overview.tables.columns → 连线面板两端字段下拉数据源 */
export function trgColumns(ov: KnowledgeOverview): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const tb of ov.tables) out[tb.name] = tb.columns.map((c) => c.name)
  return out
}

interface Trg2dActions {
  onAddEdge: (e: Trg2dAddEdge) => Promise<void>
  onDeleteEdge: (e: Trg2dDeleteEdge) => Promise<void>
  /** draft(llm) 边确认：与原右栏列表逐条 ✓ 同链路（confirmGraphDraft） */
  onConfirmEdge: (e: Trg2dDeleteEdge) => Promise<void>
  onLayoutChange: (layout: GraphLayout) => void
}

/** 双宿主共用的图上动作：增删边 / draft 边 ✓✕ / 布局持久化 */
export function useTrg2dActions(connId: string | null): Trg2dActions {
  const { t } = useI18n()
  const addEdge = useKnowledge((s) => s.addEdge)
  const removeEdge = useKnowledge((s) => s.removeEdge)
  const saveLayout = useKnowledge((s) => s.saveLayout)
  const confirmGraphDraft = useKnowledge((s) => s.confirmGraphDraft)
  const rejectGraphDraft = useKnowledge((s) => s.rejectGraphDraft)

  const onAddEdge = useCallback(async (e: Trg2dAddEdge) => {
    if (!connId) return
    await addEdge(connId, e)
    toastMsg(t('kb.linkedToast', { from: e.from_table, to: e.to_table }))
  }, [connId, addEdge, t])

  const onDeleteEdge = useCallback(async (e: Trg2dDeleteEdge) => {
    if (!connId) return
    if (e.kind === 'llm' && e.status === 'draft') {
      // draft 边不在正式图谱里：✕ = 拒绝草案（同原列表 ✕）
      await rejectGraphDraft(connId, e.from_table)
      return
    }
    await removeEdge(connId, { from_table: e.from_table, to_table: e.to_table, kind: e.kind })
    toastMsg(t('kb.relDeletedToast', { from: e.from_table, to: e.to_table }))
  }, [connId, removeEdge, rejectGraphDraft, t])

  const onConfirmEdge = useCallback(async (e: Trg2dDeleteEdge) => {
    if (!connId) return
    await confirmGraphDraft(connId, e.from_table)
  }, [connId, confirmGraphDraft])

  const onLayoutChange = useCallback((layout: GraphLayout) => {
    if (connId) void saveLayout(connId, layout).catch(() => { /* 布局保存失败静默（下次拖拽重试） */ })
  }, [connId, saveLayout])

  return useMemo(() => ({ onAddEdge, onDeleteEdge, onConfirmEdge, onLayoutChange }),
    [onAddEdge, onDeleteEdge, onConfirmEdge, onLayoutChange])
}
