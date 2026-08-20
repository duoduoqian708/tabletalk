import { useI18n } from '@renderer/store/i18n'

interface Props {
  v: string
  /** 额外 class，如 recent 列表里的小尺寸 */
  className?: string
}

const MAP: Record<string, [string, string]> = {
  allow: ['allow', 'verdict.allow'],
  review: ['review', 'verdict.review'],
  block: ['block', 'verdict.block'],
  executed: ['exec', 'verdict.executed'],
  manual: ['manual', 'verdict.manual'],
}

export function VerdictBadge({ v, className }: Props): React.JSX.Element {
  const { t } = useI18n()
  const [cls, key] = MAP[v] ?? ['manual', v]
  return <span className={`badge ${cls}${className ? ` ${className}` : ''}`}>{t(key)}</span>
}
