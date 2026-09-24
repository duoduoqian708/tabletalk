# 知识库建设 · 任务清单与索引

> 日期：2026-08-29 · 状态：已完成（T1–T10 全部落地）
> 修订：2026-08-30 —— 三路审查发现「写好了没接线」问题后，修复轮 R1–R13 完成（数据 bug/隐私漏洞/接线/存储建表/门面下沉/文档勾选）；各任务详细文档的「偏差记录」节记有全部实现偏差。
> 依据：`docs/knowledge-and-engine-design.md`（设计）
> **每个任务的详细执行文档在 `docs/kb-tasks/` 下，本文件只做索引与协作约定。**

---

## 任务总览

| # | 任务 | 详细文档 | 对应阶段 | 依赖 | 状态 |
|---|---|---|---|---|---|
| T1 | 工程骨架：模块结构 + 门面拆分 | [T1-skeleton.md](kb-tasks/T1-skeleton.md) | Phase 0.5 | — | - [x] |
| T2 | 统一图谱：合并两套图 | [T2-graph-unify.md](kb-tasks/T2-graph-unify.md) | Phase 0 | T1 | - [x] |
| T3 | 边模型升级：cols/guard/confidence | [T3-edge-model.md](kb-tasks/T3-edge-model.md) | Phase 1.1 | T2 | - [x] |
| T4 | 多来源建边管线 | [T4-edge-builder.md](kb-tasks/T4-edge-builder.md) | Phase 1.2 | T3 | - [x] |
| T5 | 采样分列策略 | [T5-sampling.md](kb-tasks/T5-sampling.md) | Phase 1.3 | T2 | - [x] |
| T6 | 路径序列化 + 图校验 | [T6-path-validation.md](kb-tasks/T6-path-validation.md) | Phase 1.4/1.5 | T3 | - [x] |
| T7 | 概念字典 + 静态业务常量 | [T7-concepts.md](kb-tasks/T7-concepts.md) | Phase 2 | T1 | - [x] |
| T8 | 表级过滤器 + 会话变量池 | [T8-filters-session.md](kb-tasks/T8-filters-session.md) | Phase 3 | T1 | - [x] |
| T9 | 消费流程整合 | [T9-consumption.md](kb-tasks/T9-consumption.md) | Phase 4 | T3+T6+T7+T8 | - [x] |
| T10 | L3 行为层 | [T10-behavior.md](kb-tasks/T10-behavior.md) | Phase 5 | T2+T9 | - [x] |

**执行顺序**：T1 → T2 → T3 → T4 → T5 → T6 → T7 → T8 → T9 → T10
**可并行**：T5 可与 T3/T4 并行；T7/T8 可与 T3–T6 并行（并行前确认各自前置完成）

---

## 协作约定（每次 review 前请确认）

1. **每完成一个任务**：在详细文档的「完成定义」勾选 + 运行对应新测试 + 全量测试，把结果贴给我
2. **我 review 的内容**：接口对齐（对照任务文档的「数据模型/接口」）、验收标准逐条、测试是否真实断言（抽查空断言）
3. **发现偏差**：我在任务文档下记问题，你修完我复查，不往下推
4. **接口变更**：任何接口改动先同步到任务文档，不要静默偏离——T9 消费流程依赖 T3/T6/T7/T8 的接口

## 文档目录

- 设计：`docs/knowledge-and-engine-design.md`
- 任务索引：`docs/knowledge-build-tasks.md`（本文件）
- 任务详细文档：`docs/kb-tasks/T*.md`
