import { useEffect, useState } from 'react'
import { build, buildCancel, buildProgress, confirmAllEnabled, kbStatus } from '@renderer/api/knowledge'
import type { BuildProgress, KbStatus } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKbGate } from '@renderer/store/kbgate'
import { useI18n } from '@renderer/store/i18n'
import { useUi } from '@renderer/store/ui'

/**
 * 构建门禁（全局右下浮卡，不阻塞页面操作）：
 * - 当前连接 kb_status != ready → 弹出：none 引导构建 / building 进度条 / pending_review 引导确认
 * - 「稍后」只关本次浮卡；切走再切回该连接重新弹出
 * - AI/查询返回 kb_not_built → forceOpen 强制弹出
 */
export function KbBuildGate(): React.JSX.Element | null {
  const currentId = useConnections((s) => s.currentId)
  const list = useConnections((s) => s.list)
  const setView = useUi((s) => s.setView)
  const forceConnId = useKbGate((s) => s.forceConnId)
  const clearForce = useKbGate((s) => s.clearForce)
  const { t } = useI18n()
  const [status, setStatus] = useState<KbStatus | null>(null)
  const [progress, setProgress] = useState<BuildProgress | null>(null)
  const [starting, setStarting] = useState(false)
  const [dismissed, setDismissed] = useState<string | null>(null)

  const conn = list.find((c) => c.id === currentId) ?? null

  // 进入 / 切换连接 → 查知识库状态
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

  // building → 轮询进度（500ms），done 后刷新状态
  useEffect(() => {
    if (!currentId || status?.kb_status !== 'building') return
    let alive = true
    const t = setInterval(() => {
      void (async () => {
        try {
          const p = await buildProgress(currentId)
          if (!alive) return
          setProgress(p)
          if (p.done) {
            clearInterval(t)
            const s = await kbStatus(currentId)
            if (alive) setStatus(s)
          }
        } catch {
          /* 网络抖动忽略，下轮再试 */
        }
      })()
    }, 500)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [currentId, status?.kb_status])

  const needsBuild = status !== null && status.kb_status !== 'ready'
  const show = currentId !== null && conn !== null && needsBuild && (dismissed !== currentId || forceConnId === currentId)

  if (!show) return null

  const st = status?.kb_status ?? 'none'

  async function startBuild(): Promise<void> {
    if (!currentId) return
    setStarting(true)
    try {
      await build(currentId)
      setProgress(null)
      setStatus((s) => (s ? { ...s, kb_status: 'building', building: true } : s))
      // 立即拉一次进度（任务可能瞬间完成）
      try {
        const p = await buildProgress(currentId)
        setProgress(p)
        if (p.done) {
          const s = await kbStatus(currentId)
          setStatus(s)
        }
      } catch {
        /* 下轮轮询接管 */
      }
    } catch {
      /* 409 已在构建：轮询接管 */
      setStatus((s) => (s ? { ...s, kb_status: 'building', building: true } : s))
    } finally {
      setStarting(false)
    }
  }

  async function cancelBuild(): Promise<void> {
    if (!currentId) return
    await buildCancel(currentId)
    setProgress(null)
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
    <div className="kb-gate">
      <div className="kb-gate-head">
        <span className="mono" style={{ fontWeight: 600 }}>{t('kb.title')} · {conn.name}</span>
        <span className={badge.cls}>{badge.text}</span>
        <button
          className="kb-gate-x"
          title={t('kb.laterTitle')}
          onClick={() => {
            setDismissed(currentId)
            clearForce()
          }}
        >✕</button>
      </div>

      {st === 'none' && (
        <div className="kb-gate-body">
          <div className="kb-gate-text">
            {t('kb.gateNoneDesc')}
          </div>
          <div className="kb-gate-actions">
            <button className="btn save" disabled={starting} onClick={() => void startBuild()}>
              {starting ? t('kb.starting') : t('kb.build')}
            </button>
          </div>
        </div>
      )}

      {st === 'building' && (
        <div className="kb-gate-body">
          <div className="kb-gate-progress">
            <div className="kb-bar">
              <div className="kb-bar-fill" style={{ width: `${progress?.percent ?? 0}%` }} />
            </div>
            <span className="kb-bar-stage mono">{progress?.stage ?? t('kb.queuing')} · {progress?.percent ?? 0}%</span>
          </div>
          <div className="kb-gate-actions">
            <button className="btn ghost" onClick={() => void cancelBuild()}>{t('kb.cancelBuild')}</button>
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
    </div>
  )
}
