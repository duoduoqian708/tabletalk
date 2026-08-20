// locales/index.ts
import { zhCN } from './zh-CN'
import type { Messages } from './zh-CN'
import { enUS } from './en-US'
export type { Messages }

export type Locale = 'zh-CN' | 'en-US'
export const locales: Record<Locale, Messages> = { 'zh-CN': zhCN, 'en-US': enUS }
export const DEFAULT_LOCALE: Locale = 'zh-CN'

type Vars = Record<string, string | number>

function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template
  return template.replace(/\{(\w+)\}/g, (_, k) => (k in vars ? String(vars[k]) : `{${k}}`))
}

// 纯函数：当前语言缺失→回退 zh-CN→回退 key（开发期可见漏翻）
export function translate(locale: Locale, key: string, vars?: Vars): string {
  const dict = locales[locale] as unknown as Record<string, unknown>
  const fallback = locales[DEFAULT_LOCALE] as unknown as Record<string, unknown>
  const lookup = (o: Record<string, unknown>): unknown =>
    key.split('.').reduce<unknown>((acc, p) => (acc && typeof acc === 'object' ? (acc as Record<string, unknown>)[p] : undefined), o)
  const val = (lookup(dict) ?? lookup(fallback) ?? key) as string
  return interpolate(val, vars)
}
