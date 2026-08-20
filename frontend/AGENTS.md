# frontend/AGENTS.md

React 18 + TypeScript + Vite 5 SPA. **Vite root is `src/renderer/`** (not repo root);
build output `../../dist` → `frontend/dist/`, served same-origin by the backend.
Aliases: `@renderer` → `src/renderer/src`, `@shared` → `src/shared`.
State = **zustand 4.5 only**. No router library, no chart/graph lib (all hand-rolled).

## Critical non-obvious behaviors

- **No router.** "Pages" are a zustand enum `useUi.view`
  (`'workspace'|'gate'|'knowledge'|'graph'|'audit'`). No URLs/deep-links; refresh
  returns to `workspace`. First run with no connections → onboarding → "使用演示库".
- **Token lives in a module singleton** `rt` in `src/renderer/src/api/client.ts`
  (NOT a store). `useBootstrap` polls `GET /api/v1/bootstrap` **untokened** every
  700ms until the sidecar answers, then `setRuntime({baseUrl:'', token, dataDir})`.
  `request()` attaches `X-TableTalk-Token`; `baseUrl` is `''` (same-origin).
- **`chatStream` bypasses `request()`** — it reads `getRuntime()` directly and
  duplicates the token-attach logic. Keep both in sync if auth changes.
- **AI "reasoning steps" panel is a scripted simulation**, not server-driven
  (`THINK_LINES` + `setTimeout` in `AiRail.tsx`). Real backend `think` events are
  merged in, but structure/timing is fake. Changing backend won't change the animation.
- **Chat history is in `localStorage`** (`tabletalk-chats-v1`, capped 30), NOT the backend.
  Backend keeps only audit JSONL + a session title. Switching connections restores the
  last conversation for that connection.
- **Read-tier AI SQL auto-executes with no click** (`maybeAutoRun` in `AiRail`).
  DML (`review`) needs the "✓ 确认执行" button; `block`/`ddl` never auto-run.
- **Settings drawer safety/general inputs are non-functional placeholders.**
  `persist()` only saves `ai_models`/`default_ai_model`/`embedding_models`/
  `default_embedding_model`. Theme section is a locked placeholder.
- Result set is single-instance (`useResults.result`); each new result/report replaces
  the prior (no tabs). `report` and `result` are mutually exclusive in the center pane.

## Scripts

`npm run dev` (Vite :5173, proxies `/api`→`127.0.0.1:8777`; backend must be running) ·
`npm run typecheck` (tsc --noEmit) · `npm run build` (→ `frontend/dist`) ·
`npm run screenshot` (Playwright: spawns backend on isolated
`TABLETALK_DATA_DIR=/tmp/tabletalk-shot` + port **8766**, connects demo DB, runs
"AI 生成标签", screenshots `docs/screenshots/knowledge-review.png`).

## Verdict surfacing

Backend returns `allow`/`review`/`block`/`executed` (`api/types.ts`). `AiRail.SqlCard`
badge + footer text map tier→verdict; `ModulePages.tsx Badge` reuses them for
`GatePage` / `AuditPage`. DDL always shows "草稿 · 手动执行" (AI has no DDL exec tool).
