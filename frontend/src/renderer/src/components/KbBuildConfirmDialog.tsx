import { useState } from 'react'
import { useConnections } from '@renderer/store/connections'
import { useKbGate } from '@renderer/store/kbgate'
import { useKnowledge } from '@renderer/store/knowledge'
import { useI18n } from '@renderer/store/i18n'

/**
 * 构建入口统一受控确认弹窗（挂在 AppLayout，与 KbBuildGate 平级）：
 * - 开关由 kbgate store 驱动（buildTrigger 决定文案），ready 连接上「全部重构」也能弹
 * - includeSamples 默认不勾（spec §3.8：零实例数据出网需显式授权）
 */
export function KbBuildConfirmDialog(): React.JSX.Element | null {
  const currentId = useConnections((s) => s.currentId)
  const buildTrigger = useKbGate((s) => s.buildTrigger)
  const closeBuildDialog = useKbGate((s) => s.closeBuildDialog)
  const buildBusy = useKnowledge((s) => s.busy)
  const buildTask = useKnowledge((s) => s.buildTask)
  const { t } = useI18n()
  const [includeSamples, setIncludeSamples] = useState(false)

  if (buildTrigger === null || currentId === null) return null

  const trig = buildTrigger

  function startBuild(): void {
    if (!currentId) return
    void buildTask(currentId, undefined, { trigger: trig ?? 'init', includeSamples })
    closeBuildDialog()
  }

  return (
    <div className="kb-dialog-mask" onClick={() => closeBuildDialog()}>
      <div className="kb-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="kb-dialog-title">{trig === 'rebuild' ? t('kb.rebuildTitle2') : t('kb.dialogTitle')}</div>
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
            {trig === 'rebuild' ? t('kb.rebuildAll') : t('kb.dialogStart')}
          </button>
        </div>
      </div>
    </div>
  )
}
