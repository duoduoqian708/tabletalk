import { useEffect, useRef, useState } from 'react'
import { buildCancel, confirmAllEnabled, kbStatus } from '@renderer/api/knowledge'
import type { KbStatus } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKbGate } from '@renderer/store/kbgate'
import { useKnowledge } from '@renderer/store/knowledge'
import { useI18n } from '@renderer/store/i18n'
import { useUi } from '@renderer/store/ui'

/**
 * 构建门禁（全局右下浮卡，不阻塞页面操作）：
 * - 构建进度由 store 驱动（所有入口统一走 useKnowledge.buildTask → SSE 推送），本组件零轮询
 * - 当前连接 kb_status != ready → 弹出：none 引导构建 / building 进度条 / pending_review 引导确认
 * - 构建中强制显示（可最小化为胶囊，不可关闭），完成/取消后自动收起
 * - 「稍后」只关本次浮卡；切走再切回该连接重新弹出
 */
export function KbBuildGate(): React.JSX.Element | null {
  const currentId = useConnections((s) => s.currentId)
  const list = useConnections((s) => s.list)
  const setView = useUi((s) => s.setView)
  const forceConnId = useKbGate((s) => s.forceConnId)
  const clearForce = useKbGate((s) => s.clearForce)
  const { t } = useI18n()
  const [status, setStatus] = useState<KbStatus | null>(null)
  const [dismissed, setDismissed] = useState<string | null>(null)
  const [showDialog, setShowDialog] = useState(false)
  const [includeSamples, setIncludeSamples] = useState(true)
  /** 构建中最小化：缩到右下角胶囊（构建未完成不允许彻底关闭） */
  const [minimized, setMinimized] = useState(false)
  /** 停止构建二次确认 */
  const [showCancelConfirm, setShowCancelConfirm] = useState(false)

  /* 构建状态直接来自 store（SSE 实时推送，无轮询） */
  const buildBusy = useKnowledge((s) => s.busy)
  const buildProgress = useKnowledge((s) => s.buildProgress)
  const buildTask = useKnowledge((s) => s.buildTask)

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
  const needsBuild = status !== null && status.kb_status !== 'ready'
  const show = currentId !== null && conn !== null
    && (isBuilding || (needsBuild && (dismissed !== currentId || forceConnId === currentId)))

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

  async function startBuild(samples = true): Promise<void> {
    if (!currentId) return
    setShowDialog(false)
    setStatus((s) => (s ? { ...s, kb_status: 'building', building: true } : s))
    await buildTask(currentId)
    // buildTask 完成后已刷新 overview；这里再同步一次 kb_status 兜底
    if (currentId) kbStatus(currentId).then((s) => s && setStatus(s)).catch(() => undefined)
  }

  async function cancelBuild(): Promise<void> {
    if (!currentId) return
    await buildCancel(currentId)
    setStatus((s) => (s ? { ...s, kb_status: 'none', building: false } : s))
  }

  async function goConfirm(): Promise<void> {
    if (!currentId) return
    await confirmAllEnabled(currentId)
    setStatus((s) => (s ? { ...s, kb_status: 'ready', building: false } : s))
    clearForce()
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
              setDismissed(currentId)
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
            <button className="btn save" disabled={buildBusy} onClick={() => setShowDialog(true)}>
              {buildBusy ? t('kb.starting') : t('kb.build')}
            </button>
          </div>
        </div>
      )}

      {st === 'building' && (
        <div className="kb-gate-body">
          {/* 三阶段独立进度条 */}
          {(progress?.phases?.length ?? 0) > 0 ? (
            <div className="kb-phases">
              {(progress?.phases ?? []).map((ph, idx) => (
                <div key={ph.key} className="kb-phase">
                  <div className="kb-phase-head">
                    <span className="kb-phase-idx mono">{idx + 1}</span>
                    <span className="kb-phase-label mono">{ph.label}</span>
                    {ph.detail && (
                      <span className="kb-phase-detail mono">正在处理 {ph.detail}</span>
                    )}
                    <span className="kb-phase-pct mono">{ph.percent}%</span>
                  </div>
                  <div className="kb-bar">
                    <div className="kb-bar-fill" style={{ width: `${ph.percent ?? 0}%` }} />
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="kb-gate-progress">
              <div className="kb-bar">
                <div className="kb-bar-fill" style={{ width: `${progress?.percent ?? 0}%` }} />
              </div>
              <span className="kb-bar-stage mono">
                {progress?.stage ?? t('kb.queuing')}
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
          <div className="kb-gate-text">
            {t('kb.gatePendingDesc')}
          </div>
          <div className="kb-gate-actions">
            <button className="btn tl" onClick={() => setView('knowledge')}>{t('kb.viewFix')}</button>
            <button className="btn save" onClick={() => void goConfirm()}>{t('kb.confirmAll')}</button>
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

      {showDialog && (
        <div className="kb-dialog-mask" onClick={() => setShowDialog(false)}>
          <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="kb-dialog-title">{t('kb.dialogTitle')}</div>
            <label className="kb-dialog-option">
              <input
                type="checkbox"
                checked={includeSamples}
                onChange={(e) => setIncludeSamples(e.target.checked)}
              />
              <span>{t('kb.dialogSampleHint')}</span>
            </label>
            <p className="kb-dialog-sub">{t('kb.dialogSampleDesc')}</p>
            <div className="kb-dialog-actions">
              <button className="btn ghost" onClick={() => setShowDialog(false)}>{t('kb.dialogCancel')}</button>
              <button className="btn save" onClick={() => void startBuild(includeSamples)}>
                {t('kb.dialogStart')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
