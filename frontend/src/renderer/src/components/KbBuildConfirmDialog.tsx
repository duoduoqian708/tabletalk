import { useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useKbGate } from '@renderer/store/kbgate'
import { useKnowledge } from '@renderer/store/knowledge'
import { useI18n } from '@renderer/store/i18n'

export type RebuildMode = 'diff' | 'full_anchor' | 'full_fresh'

/**
 * 构建入口统一受控确认弹窗（挂在 AppLayout，与 KbBuildGate 平级）：
 * - 开关由 kbgate store 驱动（buildTrigger 决定文案），ready 连接上「全部重构」也能弹
 * - includeSamples 默认不勾（spec §3.8：零实例数据出网需显式授权）
 * - rebuild 时显示重构档位三选一：
 *   diff=智能差异（只提案变化表，推荐）／full_anchor=全量重注·参考现有标签／full_fresh=全量重注·标签从零
 */
export function KbBuildConfirmDialog(): React.JSX.Element | null {
  const currentId = useConnections((s) => s.currentId)
  const buildTrigger = useKbGate((s) => s.buildTrigger)
  const closeBuildDialog = useKbGate((s) => s.closeBuildDialog)
  const buildBusy = useKnowledge((s) => s.busy)
  const buildTask = useKnowledge((s) => s.buildTask)
  const { t } = useI18n()
  const [includeSamples, setIncludeSamples] = useState(false)
  const [rebuildMode, setRebuildMode] = useState<RebuildMode>('diff')

  if (buildTrigger === null || currentId === null) return null

  const trig = buildTrigger
  const isRebuild = trig === 'rebuild'

  function startBuild(): void {
    if (!currentId) return
    const annotateMode = isRebuild && rebuildMode === 'diff' ? 'diff' : 'full'
    const tagMode = !isRebuild || rebuildMode === 'diff'
      ? 'keep'
      : rebuildMode === 'full_anchor' ? 'anchor' : 'fresh'
    void buildTask(currentId, undefined, { trigger: trig ?? 'init', includeSamples, annotateMode, tagMode })
    closeBuildDialog()
  }

  const modeOptions: { key: RebuildMode; title: string; desc: string }[] = [
    { key: 'diff', title: t('kb.modeDiffTitle'), desc: t('kb.modeDiffDesc') },
    { key: 'full_anchor', title: t('kb.modeFullAnchorTitle'), desc: t('kb.modeFullAnchorDesc') },
    { key: 'full_fresh', title: t('kb.modeFullFreshTitle'), desc: t('kb.modeFullFreshDesc') },
  ]

  return (
    <div className="kb-dialog-mask" onClick={() => closeBuildDialog()}>
      <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="kb-dialog-title">{isRebuild ? t('kb.rebuildTitle2') : t('kb.dialogTitle')}</div>
        {isRebuild && (
          <div className="kb-rebuild-modes" role="radiogroup" aria-label={t('kb.modeDiffTitle')}>
            {modeOptions.map((m) => (
              <label key={m.key} className={`kb-rebuild-mode${rebuildMode === m.key ? ' on' : ''}`}>
                <input
                  type="radio"
                  name="kb-rebuild-mode"
                  checked={rebuildMode === m.key}
                  onChange={() => setRebuildMode(m.key)}
                />
                <span className="kb-rebuild-mode-body">
                  <span className="kb-rebuild-mode-title">{m.title}</span>
                  <span className="kb-rebuild-mode-desc">{m.desc}</span>
                </span>
              </label>
            ))}
          </div>
        )}
        <label className="kb-dialog-option">
          <input
            type="checkbox"
            checked={includeSamples}
            onChange={(e) => setIncludeSamples(e.target.checked)}
          />
          <span>{t('kb.dataConsent')}</span>
        </label>
        <p className="kb-dialog-sub">{t('kb.dataConsentDesc')}</p>
        <div className="kb-dialog-actions">
          <button className="btn ghost" onClick={() => closeBuildDialog()}>{t('kb.dialogCancel')}</button>
          <button className="btn save" disabled={buildBusy} onClick={startBuild}>
            {isRebuild ? t('kb.rebuildAll') : t('kb.dialogStart')}
          </button>
        </div>
      </div>
    </div>
  )
}
