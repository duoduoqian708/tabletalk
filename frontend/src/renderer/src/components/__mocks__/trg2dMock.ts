import type { GraphEdge } from '@renderer/api/types'
import type { Trg2dTable, Trg2dLayout } from '@renderer/components/TableRelationGraph2D'

/* TableRelationGraph2D 最小 mock 场景（Task 5 接线宿主时复用）：
   3 个领域簇 + fk/user 确认边 + LLM draft 边 + 1 张排除表 */

export const TRG2D_MOCK_TABLES: Trg2dTable[] = [
  { name: 'users', tags: ['用户'] },
  { name: 'user_profiles', tags: ['用户'] },
  { name: 'orders', tags: ['交易'] },
  { name: 'order_items', tags: ['交易'] },
  { name: 'payments', tags: ['交易'] },
  { name: 'products', tags: ['商品'] },
  { name: 'categories', tags: ['商品'] },
  { name: 'shipments', tags: ['物流'] },
  { name: 'audit_log', tags: [], excluded: true },
]

export const TRG2D_MOCK_EDGES: GraphEdge[] = [
  { from: 'orders', from_col: 'user_id', to: 'users', to_col: 'id', kind: 'fk', cardinality: 'n:1', reason: '外键 orders.user_id → users.id' },
  { from: 'order_items', from_col: 'order_id', to: 'orders', to_col: 'id', kind: 'fk', cardinality: 'n:1', reason: '外键 order_items.order_id → orders.id' },
  { from: 'payments', from_col: 'order_id', to: 'orders', to_col: 'id', kind: 'fk', cardinality: 'n:1', reason: '外键 payments.order_id → orders.id' },
  { from: 'shipments', from_col: 'order_id', to: 'orders', to_col: 'id', kind: 'fk', cardinality: '1:1', reason: '一单一发货（FK 兼唯一索引）' },
  { from: 'order_items', from_col: 'product_id', to: 'products', to_col: 'id', kind: 'fk', cardinality: 'n:1', reason: '外键 order_items.product_id → products.id' },
  { from: 'products', from_col: 'category_id', to: 'categories', to_col: 'id', kind: 'fk', cardinality: 'n:1', reason: '外键 products.category_id → categories.id' },
  { from: 'user_profiles', from_col: 'user_id', to: 'users', to_col: 'id', kind: 'user', cardinality: '1:1', reason: '人工连线' },
  // LLM 未确认 draft 边：虚线琥珀样式
  { from: 'orders', from_col: 'product_id', to: 'products', to_col: 'id', kind: 'llm', cardinality: 'n:1', status: 'draft', reason: '列名与值分布推断：订单直连商品（待确认）' },
]

export const TRG2D_MOCK_COLUMNS: Record<string, string[]> = {
  users: ['id', 'name', 'email', 'created_at'],
  user_profiles: ['id', 'user_id', 'bio', 'avatar_url'],
  orders: ['id', 'user_id', 'product_id', 'amount', 'status', 'created_at'],
  order_items: ['id', 'order_id', 'product_id', 'quantity', 'price'],
  payments: ['id', 'order_id', 'method', 'paid_at'],
  products: ['id', 'category_id', 'title', 'price', 'stock'],
  categories: ['id', 'name', 'parent_id'],
  shipments: ['id', 'order_id', 'carrier', 'tracking_no'],
  audit_log: ['id', 'actor', 'action', 'at'],
}

/** 预置坐标（模拟 payload.layout 回读；缺省则组件走领域聚类初始布局） */
export const TRG2D_MOCK_LAYOUT: Trg2dLayout | undefined = undefined
