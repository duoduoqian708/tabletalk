import { useEffect, useRef, useState } from 'react'
import { buildCancel, kbStatus } from '@renderer/api/knowledge'
import { getSettings } from '@renderer/api/settings'
import type { KbStatus } from '@renderer/api/types'
import { useConnections } from '@renderer/store/connections'
import { useKbGate } from '@renderer/store/kbgate'
import { useKnowledge } from '@renderer/store/knowledge'
import { useUi } from '@renderer/store/ui'
import { useI18n } from '@renderer/store/i18n'
import { CloseBtn } from './ui/buttons'
import { IconCheck, IconAlert, IconX } from './ui/icons'

/**
 * 构建门禁（全局右下浮卡，不阻塞页面操作）：
 * - 构建进度由 store 驱动（所有入口统一走 useKnowledge.buildTask → SSE 推送），本组件零轮询
 * - 当前连接 kb_status != ready → 弹出：none 引导构建 / building 进度条 / pending_review 引导确认
 * - 构建中强制显示（可最小化为胶囊，不可关闭），完成/取消后自动收起
 * - ✕/稍后 → 缩为常驻警告胶囊：none 点回浮卡、pending_review 跳知识库工作台（批量确认在那里）；
 *   ready 才消失
 */
export function KbBuildGate({ onOpenSettings }: { onOpenSettings?: (sec: string) => void } = {}): React.JSX.Element | null {
  const currentId = useConnections((s) => s.currentId)
  const list = useConnections((s) => s.list)
  const forceConnId = useKbGate((s) => s.forceConnId)
  const clearForce = useKbGate((s) => s.clearForce)
  const view = useUi((s) => s.view)
  const { t } = useI18n()
  const [status, setStatus] = useState<KbStatus | null>(null)
  /** 「稍后」后的常驻胶囊态（boolean）：切连接重置，ready 后随浮卡一起消失 */
  const [dismissed, setDismissed] = useState(false)
  /** 构建中最小化：缩到右下角胶囊（构建未完成不允许彻底关闭） */
  const [minimized, setMinimized] = useState(false)
  /** 停止构建二次确认 */
  const [showCancelConfirm, setShowCancelConfirm] = useState(false)
  /** AI 配置检查（构建前置）：null=检查中；{chat,emb} 各表示是否可用 */
  const [aiOk, setAiOk] = useState<{ chat: boolean; emb: boolean } | null>(null)

  /* 构建状态直接来自 store（SSE 实时推送，无轮询） */
  const buildBusy = useKnowledge((s) => s.busy)
  const buildProgress = useKnowledge((s) => s.buildProgress)
  const buildError = useKnowledge((s) => s.error)
  const reattachBuild = useKnowledge((s) => s.reattachBuild)
  /** 构建失败时捕获的错误（busy→false 时从 store 取一次）；显示后用户点"重新构建"清除 */
  const [failMsg, setFailMsg] = useState<string | null>(null)

  const conn = list.find((c) => c.id === currentId) ?? null

  /** 构建完成后转入待审 → 浮卡切"去审核"CTA（替代旧版悄悄消失，完成时有明确下一步） */
  const [builtPending, setBuiltPending] = useState(false)

  // 进入/切换连接 → 查知识库状态（引导判断 + 构建重挂）；此后每 60s 轮询一次——
  // 后台自动同步（默认 30 分钟）产出提案转 pending_review 时，无需手动刷新即可看到审核入口。
  // （合并了此前两个 effect 对同一 currentId 的重复请求）
  useEffect(() => {
    if (!currentId) {
      setStatus(null)
      return
    }
    let alive = true
    const load = (): void => {
      kbStatus(currentId)
        .then((s) => {
          if (!alive) return
          setStatus(s)
          // 刷新页面后若后端构建任务仍在跑 → 重挂 SSE 只吃剩余进度（store 内幂等防重）
          if (s?.building) void reattachBuild(currentId)
        })
        .catch(() => alive && setStatus(null))
    }
    load()
    const timer = window.setInterval(load, 60_000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentId])

  // forceOpen（AI 收到 kb_not_built 时置位）：刷新状态——本地 status 过期时也要能弹出引导
  useEffect(() => {
    if (!currentId || forceConnId !== currentId) return
    let alive = true
    kbStatus(currentId).then((s) => alive && setStatus(s)).catch(() => undefined)
    return () => { alive = false }
  }, [currentId, forceConnId])

  // 构建前置检查：对话 + 嵌入模型都必须配置可用（缺失 → 引导去设置，禁止构建）
  const modelUsable = (m: { provider?: string; base_url?: string } | undefined): boolean =>
    !!m && !!String(m.provider ?? '').trim() && (String(m.provider) === 'mock' || !!String(m.base_url ?? '').trim())
  async function checkAi(): Promise<void> {
    try {
      const s = await getSettings()
      const chat = s.ai_models.find((m) => m.id === s.default_ai_model) ?? s.ai_models[0]
      const emb = s.embedding_models.find((m) => m.id === s.default_embedding_model) ?? s.embedding_models[0]
      setAiOk({ chat: modelUsable(chat), emb: modelUsable(emb) })
    } catch {
      setAiOk(null)
    }
  }
  useEffect(() => {
    setAiOk(null)
    if (!currentId) return
    void checkAi()
  }, [currentId])

  // 切换连接 → 重置浮卡的「稍后/最小化」状态
  useEffect(() => {
    setDismissed(false)
    setMinimized(false)
  }, [currentId])

  // 构建结束（busy 变 false）→ 重新查一次状态（构建结果决定 pending_review/ready）；
  // 转 pending_review → 浮卡切「去审核」CTA 态（本次构建/同步会话内显示，可关闭）
  const wasBusy = useRef(false)
  useEffect(() => {
    if (buildBusy) {
      wasBusy.current = true
      return
    }
    if (wasBusy.current && currentId) {
      wasBusy.current = false
      setMinimized(false)
      if (buildError) setFailMsg(buildError)
      else setFailMsg(null)
      kbStatus(currentId).then((s) => {
        if (s) {
          setStatus(s)
          setBuiltPending(s.kb_status === 'pending_review')
        }
      }).catch(() => undefined)
    }
  }, [buildBusy, currentId])

  // 切换连接重置 CTA 态
  useEffect(() => { setBuiltPending(false) }, [currentId])

  /* 构建中 = store busy + 有进度（SSE 已连接）且进度属于当前连接（切连接不串台）；
     此时强制显示，无视 dismissed */
  const isBuilding = buildBusy && buildProgress !== null && buildProgress.connId === currentId
  // 弹出条件：构建中 / 未构建 / 刚构建完成转待审（CTA）。
  // 2026-09 复盘：pending_here 隐藏曾造成"重构完成零引导"（弹窗/横幅已删，卡片是唯一主动引导面）
  // → 恢复在知识库页也显示；知识库页另有完成自动跳审核 + 审核 Tab 计数徽章兜底
  const needsBuild = status !== null && status.kb_status === 'none'
  // forceOpen 生效：AI 报 kb_not_built 时强制弹出（此前 show 不含 forceConnId → 空操作）
  const forceOpen = forceConnId === currentId
    && (needsBuild || status?.kb_status === 'pending_review')
  const show = currentId !== null && conn !== null
    && (isBuilding || needsBuild || forceOpen || (builtPending && !dismissed))

  if (!show) return null

  const st = isBuilding ? 'building' : (status?.kb_status ?? 'none')
  const progress = buildProgress

  /* 构建中且被最小化 → 右下角胶囊（显示阶段名，点击展开；可停止）。
     总百分比已移除：全局窗口映射值与真实进度有偏差（一上来就跳高位），只信阶段条 */
  if (st === 'building' && minimized) {
    return (
      <div className="kb-gate-min" onClick={() => setMinimized(false)} title={t('kb.expandTitle')}>
        <span className="kb-min-pulse" />
        <span className="kb-min-label mono">{t('kb.buildingLabel')}</span>
        <span className="kb-min-stage mono">{progress?.stage ?? t('kb.queuing')}</span>
        <button className="kb-min-stop" title={t('kb.cancelBuild')}
          onClick={(e) => { e.stopPropagation(); setShowCancelConfirm(true) }}>■</button>
      </div>
    )
  }

  /* 稍后 → 常驻警告胶囊（最终形态，无关闭按钮）：pending_review 点击跳知识库工作台，否则点回完整卡。
     知识库页内不显示待审胶囊（页内审核 Tab 接管） */
  const pillMode = !isBuilding && dismissed && forceConnId !== currentId
    && !(status?.kb_status === 'pending_review' && view === 'knowledge')
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

  /** 去审查/确认：跳知识库页并请求打开审核镜头（页内消费标记直达镜头），清强制标记 */
  function goReview(): void {
    if (!currentId) return
    clearForce()
    useKbGate.getState().requestReview(currentId)
    useUi.getState().setView('knowledge')
  }

  const badge = {
    none: { text: t('kb.badgeNone'), cls: 'kb-badge none' },
    building: { text: t('kb.buildingLabel'), cls: 'kb-badge building' },
    pending_review: { text: t('kb.badgePending'), cls: 'kb-badge pending' },
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
          <CloseBtn
            className="kb-gate-x"
            title={t('kb.laterTitle')}
            onClick={() => {
              setDismissed(true)
              clearForce()
            }}
          />
        )}
      </div>

      {st === 'none' && (
        <div className="kb-gate-body">
          {failMsg && (
            <div className="kb-gate-text kb-gate-error">
              <div className="kb-error-title"><IconX size={11} /> {t('kb.buildFailed')}</div>
              <div className="kb-error-msg">{failMsg}</div>
              <div className="kb-gate-actions">
                <button className="btn danger" onClick={() => setFailMsg(null)}>
                  {t('kb.dismiss')}
                </button>
              </div>
            </div>
          )}
          {!failMsg && (
            <div className="kb-gate-text">
              {t('kb.gateNoneDesc')}
            </div>
          )}
          <div className="kb-gate-actions">
            {aiOk === null || (aiOk.chat && aiOk.emb) ? (
              <button className="btn save" disabled={buildBusy || aiOk === null}
                onClick={() => useKbGate.getState().openBuildDialog('init')}>
                {buildBusy ? t('kb.starting') : t('kb.build')}
              </button>
            ) : (
              <div className="kb-ai-miss">
                <div className="kb-ai-miss-title mono"><IconAlert size={11} /> {t('kb.aiRequired')}</div>
                <div className="kb-ai-miss-list">
                  {!aiOk.chat && <div>· {t('kb.aiMissingChat')}</div>}
                  {!aiOk.emb && <div>· {t('kb.aiMissingEmb')}</div>}
                </div>
                <div className="kb-ai-miss-actions">
                  <button className="btn save" onClick={() => onOpenSettings?.('llm')}>{t('kb.aiGoConfig')}</button>
                  <button className="btn ghost" onClick={() => void checkAi()}>{t('kb.aiRecheck')}</button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {st === 'building' && (
        <div className="kb-gate-body">
          {/* 三阶段独立进度条（段7：阶段内子步标注 + step 计数）；总百分比由右上角徽章表达 */}
          {(progress?.phases?.length ?? 0) > 0 ? (
            <div className="kb-phases">
              {(progress?.phases ?? []).map((ph, idx) => {
                // 单步阶段无 chips → 头部保留步骤名；多步阶段步骤态由 chips（done/on）表达，头部不再重复
                const headStep = (!ph.steps || ph.steps.length <= 1) ? ph.step_label : null
                return (
                  <div key={ph.key} className="kb-phase">
                    <div className="kb-phase-head">
                      <span className="kb-phase-idx mono">{idx + 1}</span>
                      <span className="kb-phase-label mono">{ph.label}</span>
                      {ph.busy ? (
                        /* LLM 思考中：仅思考计时（步骤态看 chips 脉冲）；条自带 busy 流光，不放圆圈 icon */
                        <span className="kb-phase-busy mono">
                          {headStep ? `${headStep} · ` : ''}{ph.detail ?? 'AI 思考中'}
                        </span>
                      ) : (
                        /* 实时 detail（已含计数，如「已完成 12/45」），不再重复 step_index/total */
                        (headStep || ph.detail) && (
                          <span className="kb-phase-step mono">
                            {headStep}{headStep && ph.detail ? ' · ' : ''}{ph.detail ?? ''}
                          </span>
                        )
                      )}
                      <span className="kb-phase-pct mono">{ph.percent}%</span>
                    </div>
                    <div className="kb-bar">
                      <div className={`kb-bar-fill${ph.busy ? ' busy' : ''}`} style={{ width: `${ph.percent ?? 0}%` }} />
                    </div>
                    {/* 流式生成尾巴：单行 LLM 思考/正文尾文，证明在输出而非卡死 */}
                    {ph.live && <div className="kb-phase-live mono" title={ph.live}>{ph.live}</div>}
                    {ph.steps && ph.steps.length > 1 && (() => {
                      // chips 三态：已完成（序号在当前步之前 / 阶段满 100）常亮 ✓；当前步脉冲；未开始灰
                      const curIdx = ph.steps.findIndex((s) => s.key === ph.step)
                      const phaseDone = (ph.percent ?? 0) >= 100
                      return (
                        <div className="kb-phase-chips">
                          {ph.steps.map((s, i) => {
                            const done = phaseDone || (curIdx >= 0 && i < curIdx)
                            const on = !phaseDone && i === curIdx
                            return (
                              <span key={s.key} className={`kb-chip${done ? ' done' : ''}${on ? ' on' : ''}`}>
                                {done && <><IconCheck size={9} /> </>}{s.label}
                              </span>
                            )
                          })}
                        </div>
                      )
                    })()}
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
            {/* 审核在知识库页的审核镜头内完成（唯一收尾点：底部裁决栏「应用」），这里只引导跳转 */}
            <button className="btn save" onClick={goReview}>{t('kb.goReview')}</button>
            <button className="btn ghost" onClick={() => { setBuiltPending(false); setDismissed(true) }}>
              {t('kb.review.later')}
            </button>
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
