import { useEffect, useRef, useState } from 'react'
import { buildCancel, kbStatus } from '@renderer/api/knowledge'
import type { KbStatus } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKbGate } from '@renderer/store/kbgate'
import { useKnowledge } from '@renderer/store/knowledge'
import { useUi } from '@renderer/store/ui'
import { useI18n } from '@renderer/store/i18n'

/**
 * 构建门禁（全局右下浮卡，不阻塞页面操作）：
 * - 构建进度由 store 驱动（所有入口统一走 useKnowledge.buildTask → SSE 推送），本组件零轮询
 * - 当前连接 kb_status != ready → 弹出：none 引导构建 / building 进度条 / pending_review 引导确认
 * - 构建中强制显示（可最小化为胶囊，不可关闭），完成/取消后自动收起
 * - ✕/稍后 → 缩为常驻警告胶囊：none 点回浮卡、pending_review 跳知识库工作台（批量确认在那里）；
 *   ready 才消失
 */
export function KbBuildGate(): React.JSX.Element | null {
  const currentId = useConnections((s) => s.currentId)
  const list = useConnections((s) => s.list)
  const forceConnId = useKbGate((s) => s.forceConnId)
  const clearForce = useKbGate((s) => s.clearForce)
  const { t } = useI18n()
  const [status, setStatus] = useState<KbStatus | null>(null)
  /** 「稍后」后的常驻胶囊态（boolean）：切连接重置，ready 后随浮卡一起消失 */
  const [dismissed, setDismissed] = useState(false)
  /** 构建中最小化：缩到右下角胶囊（构建未完成不允许彻底关闭） */
  const [minimized, setMinimized] = useState(false)
  /** 停止构建二次确认 */
  const [showCancelConfirm, setShowCancelConfirm] = useState(false)

  /* 构建状态直接来自 store（SSE 实时推送，无轮询） */
  const buildBusy = useKnowledge((s) => s.busy)
  const buildProgress = useKnowledge((s) => s.buildProgress)
  const reattachBuild = useKnowledge((s) => s.reattachBuild)

  const conn = list.find((c) => c.id === currentId) ?? null

  // 进入 / 切换连接 → 查一次知识库状态（引导判断用，非构建感知）
  useEffect(() => {
    if (!currentId) {
      setStatus(null)
      return
    }
    let alive = true
    kbStatus(currentId)
      .then((s) => alive && setStatus(s))
      .catch(() => alive && setStatus(null))
    return () => {
      alive = false
    }
  }, [currentId])

  // 刷新页面后若后端构建任务仍在跑 → 重挂 SSE 只吃剩余进度
  useEffect(() => {
    if (!currentId) return
    let alive = true
    kbStatus(currentId).then((s) => {
      if (alive && s?.building) void reattachBuild(currentId)
    }).catch(() => undefined)
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentId])

  // 切换连接 → 重置浮卡的「稍后/最小化」状态
  useEffect(() => {
    setDismissed(false)
    setMinimized(false)
  }, [currentId])

  // 构建结束（busy 变 false）→ 重新查一次状态（构建结果决定 pending_review/ready）
  const wasBusy = useRef(false)
  useEffect(() => {
    if (buildBusy) {
      wasBusy.current = true
      return
    }
    if (wasBusy.current && currentId) {
      wasBusy.current = false
      setMinimized(false)
      kbStatus(currentId).then((s) => s && setStatus(s)).catch(() => undefined)
    }
  }, [buildBusy, currentId])

  /* 构建中 = store busy + 有进度（SSE 已连接）；此时强制显示，无视 dismissed */
  const isBuilding = buildBusy && buildProgress !== null
  // 只对「未构建」与「构建中」弹出；pending_review 待审核不再打扰（入口在知识库页顶栏）
  const needsBuild = status !== null && status.kb_status === 'none'
  const show = currentId !== null && conn !== null
    && (isBuilding || needsBuild)

  if (!show) return null

  const st = isBuilding ? 'building' : (status?.kb_status ?? 'none')
  const progress = buildProgress

  /* 构建中且被最小化 → 右下角胶囊（仍显示进度，点击展开；可停止） */
  if (st === 'building' && minimized) {
    return (
      <div className="kb-gate-min" onClick={() => setMinimized(false)} title={t('kb.expandTitle')}>
        <span className="kb-min-pulse" />
        <span className="kb-min-label mono">{t('kb.building', { n: progress?.percent ?? 0 })}</span>
        <span className="kb-min-bar"><span style={{ width: `${progress?.percent ?? 0}%` }} /></span>
        <span className="kb-min-stage mono">{progress?.stage ?? t('kb.queuing')}</span>
        <button className="kb-min-stop" title={t('kb.cancelBuild')}
          onClick={(e) => { e.stopPropagation(); setShowCancelConfirm(true) }}>■</button>
      </div>
    )
  }

  /* 稍后 → 常驻警告胶囊（最终形态，无关闭按钮）：pending_review 点击跳知识库工作台，否则点回完整卡 */
  const pillMode = !isBuilding && dismissed && forceConnId !== currentId
  if (pillMode) {
    const pend = status?.kb_status === 'pending_review'
    return (
      <div className={`kb-gate-min warn${pend ? ' goto-review' : ''}`}
           onClick={() => { clearForce(); useUi.getState().setView('knowledge') }}
           title={pend ? t('kb.pillPendingReview') : t('kb.pillNotBuilt')}>
        <span className="kb-min-pulse" />
        <span className="kb-min-label mono">
          {pend ? t('kb.pillPendingReview') : t('kb.pillNotBuilt')}
        </span>
      </div>
    )
  }

  async function cancelBuild(): Promise<void> {
    if (!currentId) return
    await buildCancel(currentId)
    setStatus((s) => (s ? { ...s, kb_status: 'none', building: false } : s))
  }

  /** 去审查/确认：跳知识库工作台（批量确认与唯一入口在那里），清强制标记 */
  function goReview(): void {
    if (!currentId) return
    clearForce()
    useUi.getState().setView('knowledge')
  }

  const badge = {
    none: { text: t('kb.badgeNone'), cls: 'kb-badge none' },
    building: { text: t('kb.building', { n: progress?.percent ?? 0 }), cls: 'kb-badge building' },
    pending_review: { text: t('kb.badgePending'), cls: 'kb-badge pending' },
    ready: { text: t('kb.badgeReady'), cls: 'kb-badge ready' },
  }[st] ?? { text: st, cls: 'kb-badge' }

  return (
    <div className={`kb-gate${st === 'building' ? ' centered' : ''}`}>
      <div className="kb-gate-head">
        <span className="mono" style={{ fontWeight: 600 }}>{t('kb.title')} · {conn.name}</span>
        <span className={badge.cls}>{badge.text}</span>
        {st === 'building' ? (
          /* 构建中：只可最小化，不可关闭 */
          <button
            className="kb-gate-x"
            title={t('kb.minimizeTitle')}
            onClick={() => setMinimized(true)}
          >—</button>
        ) : (
          <button
            className="kb-gate-x"
            title={t('kb.laterTitle')}
            onClick={() => {
              setDismissed(true)
              clearForce()
            }}
          >✕</button>
        )}
      </div>

      {st === 'none' && (
        <div className="kb-gate-body">
          <div className="kb-gate-text">
            {t('kb.gateNoneDesc')}
          </div>
          <div className="kb-gate-actions">
            <button className="btn save" disabled={buildBusy}
              onClick={() => useKbGate.getState().openBuildDialog('init')}>
              {buildBusy ? t('kb.starting') : t('kb.build')}
            </button>
          </div>
        </div>
      )}

      {st === 'building' && (
        <div className="kb-gate-body">
          {/* 三阶段独立进度条（段7：阶段内子步标注 + step 计数） */}
          {(progress?.phases?.length ?? 0) > 0 ? (
            <div className="kb-phases">
              {(progress?.phases ?? []).map((ph, idx) => {
                const stepNow = (ph.step_index ?? 0) > 0 && (ph.step_total ?? 0) > 0
                  ? `${ph.step_index}/${ph.step_total}`
                  : null
                return (
                  <div key={ph.key} className="kb-phase">
                    <div className="kb-phase-head">
                      <span className="kb-phase-idx mono">{idx + 1}</span>
                      <span className="kb-phase-label mono">{ph.label}</span>
                      {ph.busy ? (
                        /* LLM 思考中：头部保留步骤名+序号（第2步可感知），附思考计时 */
                        <span className="kb-phase-busy mono">
                          {ph.step_label ? `${ph.step_label}${stepNow ? ` ${stepNow}` : ''} · ` : ''}⟳ {ph.detail ?? 'AI 思考中'}
                        </span>
                      ) : (
                        /* 完成值 + 实时 detail 并列：收尾期显示「构图收尾 · 嵌入 3/16」 */
                        (ph.step_label || ph.detail) && (
                          <span className="kb-phase-step mono">
                            {ph.step_label}{stepNow ? ` ${stepNow}` : ''}
                            {ph.detail ? ` · ${ph.detail}` : ''}
                          </span>
                        )
                      )}
                      <span className="kb-phase-pct mono">{ph.percent}%</span>
                    </div>
                    <div className="kb-bar">
                      <div className={`kb-bar-fill${ph.busy ? ' busy' : ''}`} style={{ width: `${ph.percent ?? 0}%` }} />
                    </div>
                    {ph.steps && ph.steps.length > 1 && (
                      <div className="kb-phase-chips">
                        {ph.steps.map((s) => (
                          <span
                            key={s.key}
                            className={`kb-chip${s.key === ph.step ? ' on' : ''}`}
                          >{s.label}</span>
                        ))}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="kb-gate-progress">
              <div className="kb-bar">
                <div className="kb-bar-fill" style={{ width: `${progress?.percent ?? 0}%` }} />
              </div>
              <span className="kb-bar-stage mono">
                {progress?.stage ?? t('kb.queuing')}
                {progress?.detail ? ` · ${progress.detail}` : ''}
                {' '}· {progress?.percent ?? 0}%
              </span>
            </div>
          )}
          <div className="kb-gate-actions">
            <button className="btn ghost" onClick={() => setShowCancelConfirm(true)}>{t('kb.cancelBuild')}</button>
          </div>
        </div>
      )}

      {st === 'pending_review' && (
        <div className="kb-gate-body">
          <div className="kb-gate-text">{t('kb.gatePendingDesc')}</div>
          <div className="kb-gate-actions">
            {/* 批量确认已迁至知识库工作台顶部操作条（唯一入口），这里只引导跳转 */}
            <button className="btn tl" onClick={goReview}>{t('kb.goReview')}</button>
          </div>
        </div>
      )}

      {/* 停止构建二次确认 */}
      {showCancelConfirm && (
        <div className="kb-dialog-mask" onClick={() => setShowCancelConfirm(false)}>
          <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="kb-dialog-title">{t('kb.cancelBuildTitle')}</div>
            <p className="kb-dialog-sub">{t('kb.cancelBuildDesc')}</p>
            <div className="kb-dialog-actions">
              <button className="btn ghost" onClick={() => setShowCancelConfirm(false)}>{t('kb.dialogCancel')}</button>
              <button className="btn danger" onClick={() => { setShowCancelConfirm(false); void cancelBuild() }}>
                {t('kb.cancelBuild')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
