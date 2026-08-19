import { useEffect, useState } from 'react'
import { build, buildCancel, buildProgress, confirmAllEnabled, kbStatus } from '@renderer/api/knowledge'
import type { BuildProgress, KbStatus } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKbGate } from '@renderer/store/kbgate'
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
    none: { text: '未构建', cls: 'kb-badge none' },
    building: { text: `构建中 ${progress?.percent ?? 0}%`, cls: 'kb-badge building' },
    pending_review: { text: '待确认', cls: 'kb-badge pending' },
    ready: { text: '已就绪', cls: 'kb-badge ready' },
  }[st] ?? { text: st, cls: 'kb-badge' }

  return (
    <div className="kb-gate">
      <div className="kb-gate-head">
        <span className="mono" style={{ fontWeight: 600 }}>知识库 · {conn.name}</span>
        <span className={badge.cls}>{badge.text}</span>
        <button
          className="kb-gate-x"
          title="稍后（切回该数据源会再次提醒）"
          onClick={() => {
            setDismissed(currentId)
            clearForce()
          }}
        >✕</button>
      </div>

      {st === 'none' && (
        <div className="kb-gate-body">
          <div className="kb-gate-text">
            该数据源需要先构建知识库（含图谱）才能使用。构建全自动、带进度，完成后可审阅并一键确认启用。
          </div>
          <div className="kb-gate-actions">
            <button className="btn save" disabled={starting} onClick={() => void startBuild()}>
              {starting ? '启动中…' : '开始构建'}
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
            <span className="kb-bar-stage mono">{progress?.stage ?? '排队中'} · {progress?.percent ?? 0}%</span>
          </div>
          <div className="kb-gate-actions">
            <button className="btn ghost" onClick={() => void cancelBuild()}>取消构建</button>
          </div>
        </div>
      )}

      {st === 'pending_review' && (
        <div className="kb-gate-body">
          <div className="kb-gate-text">
            构建完成。请在知识库页查看内容（可修正），然后一键确认启用——确认后标签与图谱才参与 AI 路由。
          </div>
          <div className="kb-gate-actions">
            <button className="btn tl" onClick={() => setView('knowledge')}>查看并修正</button>
            <button className="btn save" onClick={() => void goConfirm()}>一键确认启用</button>
          </div>
        </div>
      )}
    </div>
  )
}
