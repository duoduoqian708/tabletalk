import { useEffect, useMemo, useState } from 'react'
import { useKnowledge } from '@renderer/store/knowledge'
import { useConnections } from '@renderer/store/connections'
import { useI18n } from '@renderer/store/i18n'
import { batchReview, applyRoundTags, reviewFinalize, roundTableBaseline, annotateTable, type EdgeSelector } from '@renderer/api/knowledge'
import type { GraphDraftEdge, GraphEdge, KbTableView, RoundBaseline, RoundTag } from '@renderer/api/types'
import { toastMsg } from '@renderer/utils/toast'
import { IconAlert } from './ui/icons'

/* ═══════════════════════════════════════════════
   定稿台账 A2（2026-09 重设计）：三列 2:5:3
   左列 标签：新标签默认采用（可弃用）/ 旧标签默认淘汰（可保留），改默认制
   中列 表卡片：描述旧→新 + 表级用新/用旧 + 「字段 N」展开
        （字段级用新/用旧；新增/删除字段=纯标记）；新增/删除表=纯标记
   右列 边列表：新增（接受/拒绝，即时生效型）+ 删除/未变（标记展示）；
        卡片可展开详情（类型/端点/基数/条件/来源）
   印章栏：汇总 + 绿色「确认生效」（唯一收尾）+「放弃本轮」
   裁决语义不变：全暂存，未裁决项默认采用新版；
   唯一收尾路径：apply-round-tags → rejects → batch-review → review/finalize
   ═══════════════════════════════════════════════ */

type Decision = 'confirm' | 'reject'
type FieldPick = 'new' | 'old'

function Bdg({ kind, text }: { kind?: 'new' | 'rem' | 'mod' | 'vio'; text: string }): React.JSX.Element {
  return <span className={`rl-bdg ${kind ?? ''}`}>{text}</span>
}

function partRow(k: string, oldV: string | undefined, newV: string | undefined, mono = false): React.JSX.Element | null {
  if (!oldV && !newV) return null
  return (
    <div className="rl-part">
      <div className={`rl-old${mono ? ' mono' : ''}`}><span className="rl-k">旧·{k}</span>{oldV || <span style={{ opacity: .55 }}>无</span>}</div>
      {newV !== undefined && <div className={`rl-new${mono ? ' mono' : ''}`}><span className="rl-k">新·{k}</span>{newV}</div>}
    </div>
  )
}

/* ── 中列：表卡片（变更表：表级 + 字段级两级裁决） ── */
function TableCard(props: {
  tb: KbTableView
  baseline: RoundBaseline | null
  added: boolean
  renamedFrom: string | null
  diffCols: { added: string[]; removed: string[]; changed: string[] }
  dec: Decision | undefined
  fieldDec: Record<string, FieldPick>
  onDecide: (name: string, pick: Decision) => void
  onDecideField: (key: string, pick: FieldPick) => void
  expanded: boolean
  onToggle: () => void
}): React.JSX.Element {
  const { tb, baseline, added, renamedFrom, diffCols, dec, fieldDec, onDecide, onDecideField, expanded, onToggle } = props
  const { t } = useI18n()
  const proposalCols = tb.columns.filter((c) => c.proposed_comment || c.proposed_values || c.proposed_example)
  const total = 1 + proposalCols.length
  const done = (dec !== undefined ? 1 : 0) + proposalCols.filter((c) => fieldDec[`${tb.name}.${c.name}`] !== undefined).length

  const fieldCards = tb.columns.map((c) => {
    const addedCol = diffCols.added.includes(c.name)
    const changed = diffCols.changed.includes(c.name)
    const hasProposal = !!(c.proposed_comment || c.proposed_values || c.proposed_example)
    if (!hasProposal && !addedCol && !changed) return null
    const key = `${tb.name}.${c.name}`
    const pick = fieldDec[key] ?? 'new'
    const base = baseline?.columns?.[c.name]
    return (
      <div key={c.name} className="rl-fcard" data-d={pick}>
        <div className="rl-fhead">
          <span className="rl-fname">{tb.name}.{c.name}</span>
          <span className="rl-bdg">{c.type}</span>
          {addedCol && <Bdg kind="new" text={t('kb.review.rlAdded')} />}
          {changed && <Bdg kind="mod" text={t('kb.review.structChanged')} />}
          {hasProposal && (
            <span className="rl-facts">
              <button className={`rl-btn new${pick === 'new' ? ' on' : ''}`} onClick={() => onDecideField(key, 'new')}>{t('kb.review.rlUseNew')}</button>
              <button className={`rl-btn old${pick === 'old' ? ' on' : ''}`} onClick={() => onDecideField(key, 'old')}>{t('kb.review.rlUseOld')}</button>
            </span>
          )}
        </div>
        <div className="rl-fbody">
          {partRow(t('kb.review.partComment'), hasProposal ? base?.comment : (c.comment || base?.comment), hasProposal ? (c.proposed_comment || c.comment) : c.comment)}
          {partRow(t('kb.review.partVals'), hasProposal ? base?.values : c.values, hasProposal ? c.proposed_values : c.values, true)}
          {partRow(t('kb.review.partExample'), hasProposal ? base?.example : c.example, hasProposal ? c.proposed_example : c.example, true)}
        </div>
      </div>
    )
  }).filter(Boolean)

  return (
    <div className="rl-tcard" id={`rlcard-${tb.name}`} data-d={dec ?? ''}>
      <div className="rl-thead">
        <span className="rl-tname">{tb.name}</span>
        {added && <Bdg kind="new" text={t('kb.review.rlAdded')} />}
        {renamedFrom && <span className="rl-trel mono">{t('kb.review.rlFromRename', { name: renamedFrom })}</span>}
        <Bdg kind="vio" text={t('kb.review.rlTable')} />
        {diffCols.added.length > 0 && <Bdg kind="new" text={t('kb.review.rlAddCols', { n: diffCols.added.length })} />}
        {diffCols.removed.length > 0 && <Bdg kind="rem" text={t('kb.review.rlDelCols', { n: diffCols.removed.length })} />}
        <span className="rl-count"><b>{done}</b>/{total}</span>
        <button className={`rl-btn new${dec === 'confirm' ? ' on' : ''}`} onClick={() => onDecide(tb.name, 'confirm')}>{t('kb.review.rlUseNew')}</button>
        <button className={`rl-btn old${dec === 'reject' ? ' on' : ''}`} onClick={() => onDecide(tb.name, 'reject')}>{t('kb.review.rlUseOld')}</button>
      </div>
      <div className="rl-desc">
        <div className="rl-old"><span className="rl-k">旧·{t('kb.review.partComment')}</span>{baseline?.comment || <span style={{ opacity: .55 }}>无</span>}</div>
        <div className="rl-new"><span className="rl-k">新·{t('kb.review.partComment')}</span>{tb.proposed_comment || '—'}</div>
      </div>
      {(fieldCards.length > 0 || diffCols.removed.length > 0) && (
        <>
          <button className={`rl-expand${expanded ? ' open' : ''}`} onClick={onToggle}>
            {t('kb.review.rlFields', { n: fieldCards.length + diffCols.removed.length })} <span className="chev">▸</span>
          </button>
          <div className={`rl-fields${expanded ? ' show' : ''}`}>
            {fieldCards}
            {diffCols.removed.map((cn) => (
              <div key={cn} className="rl-fcard k-removed">
                <div className="rl-fhead"><span className="rl-fname">{tb.name}.{cn}</span><Bdg kind="rem" text={t('kb.review.rlRemoved')} /></div>
                <div className="rl-note">{t('kb.review.rlColRemovedNote')}</div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

/* ── 右列：边卡片（新增=即时裁决；删除/未变=标记） ── */
function EdgeCard(props: {
  edge: GraphDraftEdge | GraphEdge
  kind: 'new' | 'removed' | 'same'
  onConfirm: (sel: EdgeSelector) => void
  onReject: (sel: EdgeSelector) => void
  /** 红边「保留」（pin）：仅 kind=removed 用 */
  onPin?: () => void
  busy: boolean
}): React.JSX.Element {
  const { edge, kind, onConfirm, onReject, onPin, busy } = props
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const dft = edge as GraphDraftEdge
  const ge = edge as GraphEdge
  const name = kind === 'new'
    ? `${dft.from_table}${dft.from_col ? `.${dft.from_col}` : ''} → ${dft.to_table}${dft.to_col ? `.${dft.to_col}` : ''}`
    : `${ge.from}${ge.from_col ? `.${ge.from_col}` : ''} → ${ge.to}${ge.to_col ? `.${ge.to_col}` : ''}`
  const kv: [string, string][] = kind === 'new'
    ? ([
        [t('kb.review.kvSource'), dft.source],
        [t('kb.review.kvNote'), dft.reason || '—'],
        dft.status === 'previously_rejected' ? [t('kb.review.kvStatus'), t('kb.review.rlPrevRejected')] : null,
      ].filter(Boolean) as [string, string][])
    : ([
        [t('kb.review.kvType'), (ge.kinds ?? []).join('+') || ge.kind || '—'],
        [t('kb.review.kvCard'), ge.cardinality ?? '—'],
        [t('kb.review.kvCols'), (ge.cols ?? []).map(([a, b]) => `${a}=${b}`).join('；') || `${ge.from_col ?? ''}=${ge.to_col ?? ''}`],
        [t('kb.review.kvGuard'), ge.guard || '—'],
        [t('kb.review.kvConf'), ge.confidence != null ? String(ge.confidence) : '—'],
        [t('kb.review.kvSource'), ge.source],
      ] as [string, string][])
  return (
    <div className={`rl-ecard k-${kind}`}>
      <div className={`rl-ehead${open ? ' open' : ''}`} onClick={() => setOpen((v) => !v)}>
        {kind === 'new' && <Bdg kind="new" text={dft.diff === 'modified' ? 'modified' : t('kb.review.rlAdded')} />}
        {kind === 'removed' && <Bdg kind="rem" text={t('kb.review.rlRemoved')} />}
        {kind === 'same' && <Bdg text={t('kb.review.rlUnchanged')} />}
        <span className="rl-ename">{name}</span>
        {kind !== 'new' && <span className="rl-emeta">{ge.cardinality ?? ''}</span>}
        <span className="rl-chev">▸</span>
      </div>
      <div className={`rl-edetail${open ? ' show' : ''}`}>
        {kv.map(([k, v]) => <div key={k} className="rl-ekv"><span>{k}</span><b>{v}</b></div>)}
        {kind === 'removed' && <div className="rl-note">{t('kb.review.rlEdgeRemovedNote')}</div>}
        {kind === 'same' && <div className="rl-note">{t('kb.review.rlEdgeSameNote')}</div>}
        {kind === 'new' && (
          <div className="rl-eacts">
            <button className="rl-btn new" disabled={busy} onClick={() => onConfirm({ from_table: dft.from_table, to_table: dft.to_table, from_col: dft.from_col ?? null, to_col: dft.to_col ?? null })}>{t('kb.review.accept')}</button>
            <button className="rl-btn old" disabled={busy} onClick={() => onReject({ from_table: dft.from_table, to_table: dft.to_table, from_col: dft.from_col ?? null, to_col: dft.to_col ?? null })}>{t('kb.review.rejectEdge')}</button>
          </div>
        )}
        {kind === 'removed' && (
          <div className="rl-eacts">
            {ge.pinned
              ? <span className="rl-note">{t('kb.review.rlEdgePinned')}</span>
              : <button className="rl-btn" disabled={busy} onClick={() => onPin?.()}>{t('kb.review.rlKeepEdge')}</button>}
          </div>
        )}
      </div>
    </div>
  )
}

export function ReviewLens(props: { onClose: () => void }): React.JSX.Element | null {
  const { onClose } = props
  const currentId = useConnections((s) => s.currentId)
  const { overview, busy, load,
    confirmTag, rejectTag,
    confirmGraphDraft, rejectGraphDraft, pinGraphEdge } = useKnowledge()
  const { t } = useI18n()
  /** 表级裁决（暂存）：未出现 = 默认采用新版 */
  const [decisions, setDecisions] = useState<Record<string, Decision>>({})
  /** 字段级裁决（暂存）：key=`${table}.${col}`，未出现 = 默认新版 */
  const [fieldDecisions, setFieldDecisions] = useState<Record<string, FieldPick>>({})
  /** 标签改默认制：new 默认全采、old 默认全淘汰；点按改判 */
  const [tagSel, setTagSel] = useState<{ old: Set<string>; new: Set<string> } | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [baselines, setBaselines] = useState<Record<string, RoundBaseline>>({})
  /** 放弃本轮二次确认 */
  const [confirmDiscard, setConfirmDiscard] = useState(false)
  /** 生效二次确认（印章栏「确认生效」→ 先确认再执行） */
  const [confirmApply, setConfirmApply] = useState(false)
  /** 版本写入等待遮罩（复用旧版确认启用弹窗：归档→写向量→清理→生效） */
  const [versionMask, setVersionMask] = useState(false)
  const [applying, setApplying] = useState(false)

  const round = overview?.round
  const diff = round?.diff
  const incr = round?.mode === 'incr'
  const newTags = round?.tags_new ?? []
  const failedTables = round?.failed_tables ?? []
  const oldTags = useMemo(() => (overview?.tags.library ?? []).filter((tg) => tg.status === 'confirmed'), [overview])
  const draftTags = useMemo(() => (overview?.tags.library ?? []).filter((tg) => tg.status === 'draft'), [overview])
  const delTables = diff?.tables.removed ?? []
  const addedSet = useMemo(() => new Set(diff?.tables.added ?? []), [diff])
  const renamedMap = useMemo(() => new Map<string, string>((diff?.tables.renamed ?? []).map(([f, to]) => [to, f])), [diff])
  const pendingTables = useMemo(() => (overview?.tables ?? []).filter((tb) =>
    tb.proposed_comment || tb.columns.some((c) => c.proposed_comment || c.proposed_values || c.proposed_example)), [overview])

  useEffect(() => {
    if (!tagSel && (newTags.length > 0 || draftTags.length > 0)) {
      setTagSel({ old: new Set(), new: new Set(newTags.map((x) => x.name)) })
    }
  }, [newTags, draftTags, tagSel])

  /* 台账卡片需要全量旧版基线（描述/字段旧值），挂载即批量拉取（逐表接口，本地缓存） */
  useEffect(() => {
    if (!currentId) return
    let alive = true
    for (const tb of pendingTables) {
      if (baselines[tb.name]) continue
      roundTableBaseline(currentId, tb.name)
        .then((r) => { if (alive && r.has_baseline) setBaselines((s) => ({ ...s, [tb.name]: r.baseline })) })
        .catch(() => undefined)
    }
    return () => { alive = false }
  }, [currentId, pendingTables])  // eslint-disable-line react-hooks/exhaustive-deps

  const diffColsFor = (name: string) => ({
    added: (diff?.columns.added ?? []).filter((x) => x.startsWith(`${name}.`)).map((x) => x.split('.').slice(1).join('.')),
    removed: (diff?.columns.removed ?? []).filter((x) => x.startsWith(`${name}.`)).map((x) => x.split('.').slice(1).join('.')),
    changed: (diff?.columns.changed ?? []).filter((x) => x.startsWith(`${name}.`)).map((x) => x.split('.').slice(1).join('.')),
  })

  /* 裁决统计（印章栏） */
  const totalDecisions = pendingTables.reduce(
    (n, tb) => n + 1 + tb.columns.filter((c) => c.proposed_comment || c.proposed_values || c.proposed_example).length, 0)
  const decidedCount = Object.keys(decisions).length + Object.keys(fieldDecisions).length

  function decideWhole(name: string, pick: Decision): void {
    setDecisions((s) => {
      const next = { ...s }
      if (next[name] === pick) delete next[name]  // 再点一次 = 撤销裁决
      else next[name] = pick
      return next
    })
  }
  function decideField(key: string, pick: FieldPick): void {
    setFieldDecisions((s) => {
      const next = { ...s }
      if (pick === 'new') delete next[key]  // 用新 = 回默认（撤销）
      else next[key] = pick
      return next
    })
  }
  function toggleTag(side: 'new' | 'old', name: string): void {
    setTagSel((s) => {
      const base = s ?? { old: new Set<string>(), new: new Set(newTags.map((x) => x.name)) }
      const next = new Set(base[side])
      if (next.has(name)) next.delete(name); else next.add(name)
      return { ...base, [side]: next }
    })
  }

  /** 生效执行（二次确认后）：遮罩全程显示——batchReview 含受影响表向量重嵌，finalize 收尾 */
  async function runApply(): Promise<void> {
    setConfirmApply(false)
    setVersionMask(true)
    // 120s 兜底（2026-09 修复）：batchReview 重嵌已改后台任务、HTTP 秒回，正常远不到此限；
    // 万一某请求挂起（半开连接），超时自动摘遮罩 + 刷新状态，不再永久卡死"按钮不可用"
    const timeout = setTimeout(() => {
      setVersionMask(false)
      toastMsg(t('kb.review.applyTimeout'))
      if (currentId) void load(currentId)
    }, 120_000)
    try {
      await applyAll()
    } finally {
      clearTimeout(timeout)
      setVersionMask(false)
    }
  }

  async function applyAll(): Promise<void> {
    if (!currentId || !overview) return
    setApplying(true)
    try {
      // ① 标签先行：按改判结果应用（409=本轮无标签全集，增量轮 → 静默跳过）
      if (newTags.length > 0 && tagSel) {
        try {
          await applyRoundTags(currentId, [...tagSel.old], [...tagSel.new])
        } catch (e) {
          if ((e as { status?: number }).status !== 409) throw e
        }
      }
      // ② 暂存"用旧"的表/字段先即时撤下（拒绝提案，保持当前值）
      for (const [name, pick] of Object.entries(decisions)) {
        if (pick === 'reject') await useKnowledge.getState().rejectComment(currentId, name)
      }
      for (const [key, pick] of Object.entries(fieldDecisions)) {
        if (pick !== 'old') continue
        const [tbl, col] = key.split('.')
        await useKnowledge.getState().rejectComment(currentId, tbl, col)
      }
      // ③ 表批量裁决（未拒绝的表 = 采用新版）；后端全清判定 → 启用收尾（ready）
      const confirmTables = pendingTables.filter((tb) => decisions[tb.name] !== 'reject').map((tb) => tb.name)
      if (confirmTables.length > 0) {
        await batchReview(currentId, confirmTables, 'confirm')
      }
      // ④ 收尾：待审全清 → 版本启用（ready）；未清 → 保持 pending 继续审
      const fin = await reviewFinalize(currentId)
      await load(currentId)
      setDecisions({})
      setFieldDecisions({})
      setTagSel(null)
      if (fin.kb_status === 'ready') {
        toastMsg(t('kb.review.appliedEnabled', { tables: confirmTables.length, v: fin.version }))
        onClose()  // 审核完成 → 回知识库浏览态
      } else {
        toastMsg(t('kb.review.appliedPartial', { tables: confirmTables.length }))
      }
    } catch (e) {
      toastMsg(t('kb.review.applyFail', { msg: (e as Error).message }))
      // 出错也刷新 overview（2026-09）：服务端可能已部分成功（网络断在响应回程），
      // 不刷新会让手机一直显示 stale 的 pending 状态
      if (currentId) void load(currentId)
    } finally {
      setApplying(false)
    }
  }

  async function doDiscard(): Promise<void> {
    if (!currentId) return
    try {
      await useKnowledge.getState().discardAll(currentId)
      toastMsg(t('kb.review.discarded'))
      setConfirmDiscard(false)
      onClose()
    } catch (e) {
      toastMsg(t('kb.review.applyFail', { msg: (e as Error).message }))
    }
  }

  /** 失败表重试：单表重新注释（缓存未命中自然重新调 LLM） */
  const [retrying, setRetrying] = useState<string | null>(null)
  async function retryTable(name: string): Promise<void> {
    if (!currentId) return
    setRetrying(name)
    try {
      const r = await annotateTable(currentId, name)
      await load(currentId)
      toastMsg(t('kb.review.retryDone', { table: name, n: r.added }))
    } catch (e) {
      toastMsg(t('kb.review.applyFail', { msg: (e as Error).message }))
    } finally {
      setRetrying(null)
    }
  }

  if (!overview || !currentId) return null

  const draftEdges = overview.graph.llm_draft_edges ?? []
  const graphEdges = overview.graph.edges ?? []
  const delEdges: GraphEdge[] = graphEdges.filter((e) => e.diff === 'removed')
  const sameEdges: GraphEdge[] = graphEdges.filter((e) => e.diff !== 'removed')
  const adoptN = tagSel?.new.size ?? 0
  const keepN = tagSel?.old.size ?? 0

  return (
    <div className="rl-lens">
      <div className="rl-head">
        <span className="rl-title"><IconAlert size={12} /> {t('kb.review.title')}</span>
        {incr && diff && (
          <span className="rl-sub">{t('kb.review.incrSummary', {
            a: diff.tables.added.length,
            r: diff.tables.removed.length,
            rn: diff.tables.renamed.length,
            c: diff.columns.added.length + diff.columns.removed.length + diff.columns.changed.length,
            tags: newTags.length > 0 ? t('kb.review.tagN', { n: newTags.length }) : t('kb.review.tagNoChange'),
            e: draftEdges.length,
          })}</span>
        )}
        <button className="rl-x" onClick={onClose}>✕</button>
      </div>

      <div className="rl-cols">
        {/* ── 左列（2）：标签 · 新旧分区对照，改默认制 ── */}
        <section className="rl-col rl-col-tags">
          <div className="rl-cap">{t('kb.review.tagCompare')}
            <span className="rl-cap-cnt">{t('kb.review.rlStampTags', { a: adoptN, na: newTags.length, k: keepN, ko: oldTags.length })}</span>
          </div>
          {(newTags.length > 0 || draftTags.length > 0) ? (
            <>
              <div className="rl-tagh">{t('kb.review.rlNewTagsH')}<span className="rl-tk">{t('kb.review.rlNewTagsHint')}</span></div>
              {newTags.length > 0 ? newTags.map((tg: RoundTag) => {
                const on = tagSel?.new.has(tg.name) ?? true
                return (
                  <div key={tg.name} className={`rl-tagcard${on ? '' : ' off'}`} title={tg.description}>
                    <div className="rl-tn">
                      {tg.name}
                      {tg.renamed_from && <em className="rl-trel mono">{t('kb.review.renamedFrom', { name: tg.renamed_from })}</em>}
                      {!!tg.merged_from?.length && <em className="rl-trel mono">{t('kb.review.mergedFrom', { names: tg.merged_from.join('、') })}</em>}
                    </div>
                    <div className="rl-td">{tg.description}</div>
                    {!!tg.tables?.length && <div className="rl-chips">{tg.tables.map((m) => <span key={m} className="rl-chip">{m}</span>)}</div>}
                    <div style={{ marginTop: 8 }}>
                      <button className={`rl-tbtn${on ? '' : ' on'}`} onClick={() => toggleTag('new', tg.name)}>
                        {on ? t('kb.review.rlTagDrop') : t('kb.review.rlTagDropped')}
                      </button>
                    </div>
                  </div>
                )
              }) : draftTags.map((tg) => (
                /* 增量轮无 tags_new → 草稿标签即时裁决（沿用原语义） */
                <div key={tg.name} className="rl-tagcard">
                  <div className="rl-tn">{tg.name}</div>
                  <div className="rl-td">{tg.description}</div>
                  <div className="rl-eacts">
                    <button className="rl-btn new" onClick={() => void confirmTag(currentId, tg.name)}>{t('common.confirm')}</button>
                    <button className="rl-btn old" onClick={() => void rejectTag(currentId, tg.name)}>{t('kb.reject')}</button>
                  </div>
                </div>
              ))}
              {newTags.length > 0 && (
                <>
                  <div className="rl-tagh">{t('kb.review.rlOldTagsH')}<span className="rl-tk">{t('kb.review.rlOldTagsHint')}</span></div>
                  {oldTags.map((tg) => {
                    const kept = tagSel?.old.has(tg.name) ?? false
                    const goNew = newTags.find((x) => x.renamed_from === tg.name || x.merged_from?.includes(tg.name))
                    return (
                      <div key={tg.name} className={`rl-tagcard${kept ? ' kept' : ' off'}`} title={tg.description}>
                        <div className="rl-tn" style={kept ? undefined : { color: 'var(--ink-faint)' }}>
                          {tg.name}
                          {goNew && <em className="rl-trel mono">{t('kb.review.rlGoesTo', { name: goNew.name })}</em>}
                        </div>
                        <div className="rl-td">{tg.description}</div>
                        <div style={{ marginTop: 8 }}>
                          <button className={`rl-tbtn${kept ? ' on keep' : ''}`} onClick={() => toggleTag('old', tg.name)}>
                            {kept ? t('kb.review.rlTagKept') : t('kb.review.rlTagKeep')}
                          </button>
                        </div>
                      </div>
                    )
                  })}
                </>
              )}
            </>
          ) : (
            <div className="rl-none">{t('kb.review.noTagDiff')}</div>
          )}
        </section>

        {/* ── 中列（5）：表卡片流（裁决主战场）── */}
        <section className="rl-col rl-col-tables">
          <div className="rl-cap">{t('kb.review.tableFlow')}
            <span className="rl-cap-cnt">{t('kb.review.rlTableCap', {
              c: pendingTables.length, a: addedSet.size, r: delTables.length })}</span>
          </div>
          {failedTables.length > 0 && (
            <div className="rl-failed">
              <div className="rl-failed-h mono"><IconAlert size={11} /> {t('kb.review.failedTables', { n: failedTables.length })}</div>
              {failedTables.map((name) => (
                <div key={name} className="rl-failed-row">
                  <span className="rl-fname mono">{name}</span>
                  <Bdg kind="mod" text={t('kb.review.annotateFailed')} />
                  <span style={{ flex: 1 }} />
                  <button className="rl-btn" disabled={retrying === name}
                    onClick={() => void retryTable(name)}>
                    {retrying === name ? t('common.loading') : t('kb.review.retryAnnotate')}
                  </button>
                </div>
              ))}
            </div>
          )}
          {pendingTables.length === 0 && delTables.length === 0 && (
            <div className="rl-none">{t('kb.review.noTableDiff')}</div>
          )}
          {pendingTables.map((tb) => (
            <TableCard key={tb.name}
              tb={tb}
              baseline={baselines[tb.name] ?? null}
              added={addedSet.has(tb.name)}
              renamedFrom={renamedMap.get(tb.name) ?? null}
              diffCols={diffColsFor(tb.name)}
              dec={decisions[tb.name]}
              fieldDec={fieldDecisions}
              onDecide={decideWhole}
              onDecideField={decideField}
              expanded={expanded.has(tb.name)}
              onToggle={() => setExpanded((s) => {
                const next = new Set(s)
                if (next.has(tb.name)) next.delete(tb.name); else next.add(tb.name)
                return next
              })}
            />
          ))}
          {delTables.map((name) => (
            <div key={name} className="rl-tcard k-removed">
              <div className="rl-thead"><span className="rl-tname">{name}</span><Bdg kind="rem" text={t('kb.review.rlRemoved')} /></div>
              <div className="rl-note">{t('kb.review.rlTableRemovedNote')}</div>
            </div>
          ))}
        </section>

        {/* ── 右列（3）：边列表（新增=即时裁决；删除/未变=标记）── */}
        <section className="rl-col rl-col-edges">
          <div className="rl-cap">{t('kb.review.edgeFlow')}
            <span className="rl-cap-cnt">{t('kb.review.rlEdgeCap', { n: draftEdges.length, r: delEdges.length, s: sameEdges.length })}</span>
          </div>
          {draftEdges.length === 0 && delEdges.length === 0 && sameEdges.length === 0 && (
            <div className="rl-none">{t('kb.review.noEdgeDiff')}</div>
          )}
          {draftEdges.length > 1 && (
            <div style={{ marginBottom: 8 }}>
              <button className="rl-btn" disabled={busy} onClick={() => void confirmGraphDraft(currentId, {})}>
                {t('kb.review.acceptAllEdges')}
              </button>
            </div>
          )}
          {draftEdges.map((e) => (
            <EdgeCard key={`${e.from_table}.${e.from_col}->${e.to_table}.${e.to_col}`} edge={e} kind="new"
              onConfirm={(sel) => void confirmGraphDraft(currentId, sel)}
              onReject={(sel) => void rejectGraphDraft(currentId, sel)}
              busy={busy} />
          ))}
          {delEdges.map((e) => (
            <EdgeCard key={`${e.from}.${e.from_col}->${e.to}.${e.to_col}`} edge={e} kind="removed"
              onConfirm={() => undefined} onReject={() => undefined}
              onPin={() => void pinGraphEdge(currentId, { from_table: e.from, to_table: e.to, from_col: e.from_col ?? null, to_col: e.to_col ?? null })}
              busy={busy} />
          ))}
          {sameEdges.length > 0 && (
            <div className="rl-same-box">
              {sameEdges.map((e) => (
                <div key={`${e.from}-${e.to}-${e.from_col ?? ''}`} className="rl-same-row">
                  <span className="nm">{e.from}{e.from_col ? `.${e.from_col}` : ''} → {e.to}{e.to_col ? `.${e.to_col}` : ''}</span>
                  <span className="card">{e.cardinality ?? ''}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>

      {/* ── 印章栏（唯一收尾点）：汇总 + 绿色确认生效 + 放弃本轮 ── */}
      <div className="rl-stamp">
        <span className="rl-sb-info">
          {t('kb.review.rlStampInfo', { x: decidedCount, y: totalDecisions })}
          <span className="rl-sep">·</span>
          {t('kb.review.rlStampTags', { a: adoptN, na: newTags.length, k: keepN, ko: oldTags.length })}
          <span className="rl-sep">·</span>
          {t('kb.review.rlStampEdges', { n: draftEdges.length })}
        </span>
        <span className="rl-hint">{t('kb.review.defaultAdopt')}</span>
        <span style={{ flex: 1 }} />
        <button className="rl-stamp-ok" disabled={busy || applying} onClick={() => setConfirmApply(true)}>
          {applying ? t('common.loading') : t('kb.review.apply')}
        </button>
        <button className="btn danger-ghost" disabled={busy || applying} onClick={() => setConfirmDiscard(true)}>
          {t('kb.review.discardRound')}
        </button>
      </div>

      {/* 生效：二次确认（未裁决项默认采用新版；生效后标签与图关系参与 AI 路由） */}
      {confirmApply && (
        <div className="kb-dialog-mask" onClick={() => setConfirmApply(false)}>
          <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="kb-dialog-title">{t('kb.review.rlApplyConfirmTitle')}</div>
            <p className="kb-dialog-sub">{t('kb.review.rlApplyConfirmDesc')}</p>
            <div className="kb-dialog-actions">
              <button className="btn ghost" onClick={() => setConfirmApply(false)}>{t('kb.dialogCancel')}</button>
              <button className="btn save" disabled={busy || applying} onClick={() => void runApply()}>
                {t('kb.review.apply')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 版本写入等待遮罩（复用旧版确认启用弹窗：同步等待向量写入/清理/生效，完成后自动消失） */}
      {versionMask && (
        <div className="kb-version-mask">
          <div className="kb-version-card">
            <div className="kb-version-spinner" />
            <div className="kb-version-title">{t('kb.review.enablingVersion')}</div>
            <ol className="kb-version-steps mono">
              <li>{t('kb.review.vStepArchive')}</li>
              <li>{t('kb.review.vStepVector')}</li>
              <li>{t('kb.review.vStepClean')}</li>
              <li>{t('kb.review.vStepActivate')}</li>
            </ol>
            <div className="kb-version-hint">{t('kb.review.enablingHint')}</div>
          </div>
        </div>
      )}

      {/* 放弃本轮：二次确认（不可逆） */}
      {confirmDiscard && (
        <div className="kb-dialog-mask" onClick={() => setConfirmDiscard(false)}>
          <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="kb-dialog-title">{t('kb.review.discardConfirmTitle')}</div>
            <p className="kb-dialog-sub">{t('kb.review.discardConfirmDesc')}</p>
            <div className="kb-dialog-actions">
              <button className="btn ghost" onClick={() => setConfirmDiscard(false)}>{t('kb.dialogCancel')}</button>
              <button className="btn danger" disabled={busy || applying} onClick={() => void doDiscard()}>
                {t('kb.review.discardRound')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
