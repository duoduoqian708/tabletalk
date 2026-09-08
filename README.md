# tabletalk

![gate](https://img.shields.io/badge/gate-100%25%20blocked-brightgreen) ![e2e](https://img.shields.io/badge/e2e-68%20checks-brightgreen) ![license](https://img.shields.io/badge/license-Apache--2.0-blue)

AI-first 数据库客户端（自托管 Web）。用自然语言对话查询你的数据库，背后是一套**模型无关的本地安全闸门**：AI 可以写，但永远不能危险地写。

> 曾用名：TABLETALK → tabletalk。现正式命名为 **tabletalk**。

## 核心能力

- **自然语言 → SQL**：AI 副驾生成 SQL，读取自动执行，写操作强制预览 + 人工确认，DDL 仅手动执行
- **安全闸门**：纯规则、模型无关（sqlglot 解析 → ALLOW / REVIEW / BLOCK），离线也能拦
- **知识库 + 图谱**：接入数据源后自动构建（结构注释、领域标签、FK/值重叠图、向量索引），带进度条与一键确认闸；未构建的数据源不可用
- **向量检索**：内置 numpy 批量索引（几万条毫秒级）+ SQLite 持久化，统一 VectorStore 接口（collection + metadata 过滤），企业版可换 LanceDB/pgvector/Qdrant 实现
- **审计留痕**：所有真实执行写入 JSONL 审计日志
- **定时任务**：任务 = Python 脚本，通过AI对话创作，支持状态机管理的ReAct调试循环，脚本化调度与执行

## 快速开始

```bash
python3 backend/tabletalk.py        # 一键：建 venv → 装依赖 → 构建前端 → 起服务 → 开浏览器
```

- 首次启动自动播种演示库（`~/.tabletalk/demo.db`），连接页「使用演示库」即可体验
- 大模型与嵌入模型**必须用户自配**（系统设置里配置任意 OpenAI 兼容端点）；未配置嵌入模型时降级为词面+图谱检索
- 数据目录默认 `~/.tabletalk`（连接配置 / 审计 / 会话 / 知识库），可用 `TABLETALK_DATA_DIR` 覆盖

## 技术栈

- **后端**：FastAPI sidecar（Python）——独占数据库连接、安全闸门与 AI 编排
- **前端**：React 18 + TypeScript + Vite + Zustand（Web SPA，由后端同源托管）
- **数据库**：SQLite（内置）/ PostgreSQL / MySQL，方言适配器注册制
- **SQL 解析**：sqlglot（安全闸门语法树）

## 文档

- 架构与决策：`CLAUDE.md`、`backend/AGENTS.md`、`frontend/AGENTS.md`
- 产品与开发蓝图：`docs/product-handbook/`（定位与原则 / 功能需求详规 / 交互与视觉规范 / 技术架构 / 路线图与验收；方向性论述见 `00-product-direction.md`）

## 开源与许可

- **License**: Apache-2.0 (`LICENSE`) — 宽松企业友好；品牌名与 logo 保留商标权（见 `LICENSE` §6）。
- **Open-core**: `ee/` 为商业闭源占位（Phase 3 团队网关），`backend/`+`frontend/`+`docs/` 保持 Apache-2.0。
- **安全闸门**: `backend/app/safety/` 已独立为 `tabletalk-gate`（`gate/` 纯函数、零依赖，`pip install -e gate` 可单独使用），攻击测试集 `backend/tests/attack/cases.yaml` 100 条全拦截（`pytest tests/test_gate_attack.py`）。

## 贡献

见 `CONTRIBUTING.md`、`SECURITY.md`。提交前请跑：`cd backend && .venv/bin/python -m pytest -q` 与 `cd frontend && npm run typecheck && npm run build && npm run e2e:graph3d`（星图像素断言回归）。
