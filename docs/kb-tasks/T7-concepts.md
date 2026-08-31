# T7 · 概念字典 + 静态业务常量（详细执行文档）

> 所属：知识库建设任务清单 · 状态：已完成 · 依赖：T1（独立于 T3–T6，可并行）
>
> ⚠️ **以下作为参考**：本文档基于当前设计编写，如果执行中发现遗漏或不合理之处，**可以调整**（调整后同步更新本文档与索引）。

---

## 1. 目标

实现 `semantic/concepts.py`：概念条目（规范枚举 + 成员列值映射）+ 静态业务常量；把现有 `ColumnInfo.values` 平铺字符串（`"P=待付款;S=已发货"`）**迁移为概念条目候选**；`kb_read` 扩展为按列查概念。对齐设计 §5 + §8.2。

## 2. 现状（读码后核实）

- `ColumnInfo.values`（store.py:43）：平铺字符串 `"P=待付款；S=已发货；R=已退货"`——非结构化，无法支撑"华东区→EAST"这类值落地
- 无概念概念实体：`orders.region`/`users.region`/`stores.region` 各自独立，没有"地区"这个共享维度
- `kb_read` 工具（tools/kb_read.py）：按表名/关键词查表卡 + 标签，无按列查概念能力
- 现有 tag 的 confirm/reject 工作流可复用（store.py 标签组方法）

## 3. 数据模型（实现必须对齐）

```python
# backend/app/knowledge/semantic/concepts.py

@dataclass
class Concept:
    name: str
    canonical_enum: list[dict]       # [{"code":"EAST","label":"华东"}, ...]
    members: list[dict]              # [{"table":"orders","column":"region","mapping": "identity" | {值: code}}]
    status: str = "draft"            # draft | confirmed
    kind: str = "dimension"          # dimension(维度) | constant(静态业务常量)
    updated_at: str = ""
    source: str = ""                 # human | sampling

class ConceptStore:
    def __init__(self, data_dir: Path): ...

    def upsert(self, conn_id: str, c: Concept) -> bool: ...
    def list(self, conn_id: str) -> list[Concept]: ...
    def get(self, conn_id: str, name: str) -> Concept | None: ...
    def get_for_column(self, conn_id: str, table: str, column: str) -> Concept | None:
        """按 (table, column) 命中成员列 → 返回所属概念（供值落地）"""
    def confirm(self, conn_id: str, name: str) -> bool: ...
    def reject(self, conn_id: str, name: str) -> bool: ...
    def parse_values_to_candidates(self, conn_id: str, table: str, column: str,
                                   values_text: str) -> list[dict]:
        """解析 "P=待付款;S=已发货" → 概念条目候选 [{code:"P",label:"待付款"}]"""
```

**concepts 表（storage.py 增加）**：

```sql
CREATE TABLE IF NOT EXISTS concepts (
  name TEXT PRIMARY KEY,
  canonical_enum TEXT NOT NULL,      -- JSON: [{"code":"EAST","label":"华东"}]
  members TEXT NOT NULL,             -- JSON: [{"table","column","mapping"}]
  status TEXT DEFAULT 'draft',
  kind TEXT DEFAULT 'dimension',     -- dimension | constant
  updated_at TEXT DEFAULT '',
  source TEXT DEFAULT ''
);
```

## 4. 静态业务常量

- `kind="constant"` 的 Concept：税率、阈值、魔数（如"大客户 = 年消费 > 100 万"）
- 与运行时变量（T8 会话变量）**严格区分**：常量进知识库（本任务），变量用占位符不进库（T8）
- 常量条目检索：`kb_read` 扩展后按关键词可查

## 5. 边界情况处理规则

| # | 情况 | 规则 |
|---|---|---|
| 1 | 一个成员列属于多个概念 | 禁止（一列一概念），upsert 时校验冲突 |
| 2 | 同名不同义列（两表 status 含义不同） | 不自动合并为同概念——归属人工确认（§5 铁律 2） |
| 3 | values 解析失败（格式不标准） | 返回空候选，不自动建条目（宁可弃不自动写） |
| 4 | 概念已存在（同名 upsert） | 覆盖（INSERT OR REPLACE），但 confirmed 状态不降级（保留 status） |
| 5 | 成员列引用的 (table,column) 不存在 | upsert 时校验 schema，不存在则拒绝 |
| 6 | 静态常量与维度同名 | 允许（name 唯一但 kind 区分）——建议命名加前缀（如 `const.tax_rate`） |
| 7 | 防漂移采样 | 定期对成员列 `SELECT DISTINCT` 与 canonical_enum 比对，新值→提示人工确认（§5 铁律 3） |

## 6. 日志要求（必须加）

```python
logger = logging.getLogger("kb.concepts")

logger.info("[concepts] conn=%(conn)s upsert concept=%(name)s kind=%(kind)s status=%(status)s", ...)
logger.info("[concepts] conn=%(conn)s confirm/reject concept=%(name)s", ...)
logger.warning("[concepts] conn=%(conn)s 概念 %(name)s 成员列冲突：%(table)s.%(column)s 已属于 %(other)s", ...)
logger.info("[concepts] conn=%(conn)s 防漂移：concept=%(name)s 新值 %(new_values)s（待确认）", ...)
```

## 7. 消费接口（kb_read 扩展）

`tools/kb_read.py` 增加查询类型或参数：

```python
# query_type 增加 "concept"：按列查概念
# 入参：table + column → 返回 {concept, canonical_enum, member_mapping}
# 入参：keyword → 返回概念名匹配列表
```

## 8. 测试设计（tests/knowledge/test_concepts.py）

| 用例 | 构造 | 断言 |
|---|---|---|
| `test_upsert_and_get` | upsert 地区概念 | get 读回一致 |
| `test_get_for_column` | 概念成员含 (orders, region) | get_for_column 命中 |
| `test_confirm_reject` | draft → confirm / reject | status 流转正确 |
| `test_member_conflict_rejected` | 同列加入两个概念 | 第二个 upsert 返回 False |
| `test_values_parse` | "P=待付款;S=已发货" | 解析出 [{code:"P",label:"待付款"},...] |
| `test_constant_kind` | 税率常量（kind=constant） | 存取正确，与 dimension 并存 |
| `test_drift_detection` | 成员列采样出现新值 | 标记待确认（不自动写） |

## 9. 执行步骤

- [x] 1. `storage.py` 加 concepts 表 + 读写
- [x] 2. `semantic/concepts.py`：`Concept`/`ConceptStore` 实现（含解析、冲突校验、防漂移）
- [x] 3. `kb_read` 扩展 concept 查询
- [x] 4. 现有 `ColumnInfo.values` 读入时解析为候选（构建/审查流程接入，人工确认后生效）
- [x] 5. 日志就位
- [x] 6. 测试 + 全量回归

```
cd backend && .venv/bin/python -m pytest -q
```

## 10. 验收标准（我 review 时逐条检查）

1. `Concept`/`ConceptStore` 字段与方法与 §3 一致
2. concepts 表落库（SQLite 可读）
3. `kb_read` 按列查概念可用
4. 一列一概念冲突校验生效
5. 防漂移：新值不自动写，提示待确认
6. 日志覆盖 CRUD/冲突/漂移
7. 全量 pytest 通过

## 11. 完成定义

- [x] concepts 表 + ConceptStore 实现
- [x] values 解析为候选接入
- [x] kb_read 概念查询扩展
- [x] 防漂移采样
- [x] 7 测试全绿 + 全量回归绿

---

## 参考

- 设计文档 §5（概念字典）/ §8.2（静态业务常量）
- 现状代码：`store.py::ColumnInfo`（values 字段）、`tools/kb_read.py`、tag confirm/reject 工作流（复用模式）
- 下游消费者：T9 消费流程（值落地检索限定在候选表内）


## 偏差记录（2026-08-30 修复轮 R11/R12）

1. **concepts 表已按 §3 建**（storage.py），存量 meta JSON 启动迁移（VACUUM 备份）。
2. **成员列 schema 校验已实现**（§5#5）：`ConceptStore.upsert(conn_id, c, schema=None)`，schema 提供时 (table,column) 不存在即拒绝。
3. **values→候选已接入**（§9#4）：构建落盘前 `BuildService.ingest_values_candidates` 解析 ColumnInfo.values 平铺串 → draft 概念（name={table}.{column}，source=sampling，人工确认后生效）。
4. **API 已补**：`GET /{conn_id}/concepts`、`POST /{conn_id}/concepts/confirm`、`POST /{conn_id}/concepts/reject`（kb_read 的 concept 查询此前已到位）。
