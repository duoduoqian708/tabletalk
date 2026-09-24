/* ═══════════════════════════════════════════════
   共享按钮组件（配合 ui/icons.tsx）
   统一页面动作按钮；原页面样式通过 className 透传保留，
   视觉差异是各容器刻意设计的，不在此处强行合并。
   ═══════════════════════════════════════════════ */
import type { MouseEventHandler, ReactNode } from 'react'
import { IconX } from "./icons"

type BtnProps = {
  className?: string
  title?: string
  onClick?: MouseEventHandler<HTMLButtonElement>
  disabled?: boolean
  /** 透传给 button 的额外属性（如 type="button"、stopPropagation 包装） */
  [key: string]: unknown
}

/** 通用图标按钮基座 */
function IconBtn({ icon, className, title, onClick, disabled, ...rest }: BtnProps & { icon: ReactNode }): React.JSX.Element {
  return (
    <button type="button" className={className} title={title} onClick={onClick} disabled={disabled} {...rest}>
      {icon}
    </button>
  )
}

/** 关闭按钮（✕）——modal/drawer/popup 右上角统一入口 */
export function CloseBtn({ className, title, onClick, disabled, ...rest }: BtnProps): React.JSX.Element {
  return (
    <IconBtn className={className} title={title} onClick={onClick} disabled={disabled} aria-label={title} {...rest}
      icon={<IconX size={11} />} />
  )
}

