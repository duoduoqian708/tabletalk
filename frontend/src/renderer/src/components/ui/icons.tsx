/* ═══════════════════════════════════════════════
   共享 UI 图标库（北欧极简细线，currentColor）
   统一页面图标，禁止再散落 ✎✓✕🔍＋ 等字符 icon
   用法：<IconEdit size={12} />；默认 12px，stroke 1.3
   ═══════════════════════════════════════════════ */

export type IconProps = {
  /** 尺寸（px），默认 12 */
  size?: number
  /** 线宽，默认 1.3 */
  strokeWidth?: number
  className?: string
}

const svgProps = (p: IconProps, viewBox = '0 0 12 12') => ({
  width: p.size ?? 12,
  height: p.size ?? 12,
  viewBox,
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: p.strokeWidth ?? 1.3,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
})

/** 铅笔（编辑） */
export function IconEdit(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <path d="M7.9 2.2 L9.8 4.1 L4.4 9.5 L2.1 9.9 L2.5 7.6 Z" />
      <path d="M7.1 3 L9 4.9" strokeWidth={(p.strokeWidth ?? 1.3) * 0.8} />
    </svg>
  )
}

/** 对勾（确认/完成） */
export function IconCheck(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <path d="M2.2 6.4 L4.9 9 L9.8 3.2" />
    </svg>
  )
}

/** 叉（关闭/删除/拒绝） */
export function IconX(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <path d="M2.8 2.8 L9.2 9.2 M9.2 2.8 L2.8 9.2" />
    </svg>
  )
}

/** 加号（新建/添加） */
export function IconPlus(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <path d="M6 2.2 V9.8 M2.2 6 H9.8" />
    </svg>
  )
}

/** 放大镜（搜索） */
export function IconSearch(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <circle cx="5.2" cy="5.2" r="3.1" />
      <path d="M7.5 7.5 L10 10" />
    </svg>
  )
}

/** 顺时针旋转箭头（重置/刷新） */
export function IconRefresh(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <path d="M9.8 6 A3.8 3.8 0 1 1 8.4 3.1" />
      <path d="M8.6 1.2 L8.6 3.3 L6.5 3.3" />
    </svg>
  )
}

/** 齿轮（设置） */
export function IconGear(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <circle cx="6" cy="6" r="1.7" />
      <path d="M6 1.6 V3 M6 9 V10.4 M1.6 6 H3 M9 6 H10.4 M2.9 2.9 L3.9 3.9 M8.1 8.1 L9.1 9.1 M9.1 2.9 L8.1 3.9 M3.9 8.1 L2.9 9.1" strokeWidth={(p.strokeWidth ?? 1.3) * 0.85} />
    </svg>
  )
}

/** 右箭头（发送/前进） */
export function IconSend(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <path d="M2 6 H9.6" />
      <path d="M6.8 2.8 L10 6 L6.8 9.2" />
    </svg>
  )
}

/** 左箭头（返回） */
export function IconArrowLeft(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <path d="M10 6 H2.4" />
      <path d="M5.2 2.8 L2 6 L5.2 9.2" />
    </svg>
  )
}

/** 锁（只读/锁定） */
export function IconLock(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <rect x="3.2" y="5.4" width="5.6" height="4.6" rx="1" />
      <path d="M4.4 5.4 V4.2 A1.6 1.6 0 0 1 7.6 4.2 V5.4" />
    </svg>
  )
}

/** 警告三角 */
export function IconAlert(p: IconProps = {}): React.JSX.Element {
  return (
    <svg {...svgProps(p)}>
      <path d="M6 1.8 L10.6 9.8 H1.4 Z" />
      <path d="M6 4.6 V6.6" />
      <circle cx="6" cy="8.3" r="0.5" fill="currentColor" stroke="none" />
    </svg>
  )
}

/* ── 排序/过滤（迁自 DataTable，10×10 viewBox 实心风格） ── */

/** 升序箭头（实心） */
export function IconUp(p: IconProps = {}): React.JSX.Element {
  return (
    <svg width={p.size ?? 10} height={p.size ?? 10} viewBox="0 0 10 10" aria-hidden="true">
      <path d="M5 1 L8.2 4.4 H6.4 V9 H3.6 V4.4 H1.8 Z" fill="currentColor" />
    </svg>
  )
}

/** 降序箭头（实心） */
export function IconDown(p: IconProps = {}): React.JSX.Element {
  return (
    <svg width={p.size ?? 10} height={p.size ?? 10} viewBox="0 0 10 10" aria-hidden="true">
      <path d="M5 9 L1.8 5.6 H3.6 V1 H6.4 V5.6 H8.2 Z" fill="currentColor" />
    </svg>
  )
}

/** 漏斗（过滤） */
export function IconFilter(p: IconProps = {}): React.JSX.Element {
  return (
    <svg width={p.size ?? 10} height={p.size ?? 10} viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth={p.strokeWidth ?? 1.1} strokeLinejoin="round" aria-hidden="true">
      <path d="M1 1.6 H9 L6 5.4 V8.2 L4 7.2 V5.4 Z" />
    </svg>
  )
}
