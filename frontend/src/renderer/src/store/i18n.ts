// store/i18n.ts
import { create } from 'zustand'
import { translate, DEFAULT_LOCALE, type Locale } from '@renderer/locales'
import type { Messages } from '@renderer/locales'

const STORAGE_KEY = 'tabletalk-locale'

function initialLocale(): Locale {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    if (v === 'zh-CN' || v === 'en-US') return v
  } catch { /* ignore */ }
  return DEFAULT_LOCALE
}

interface I18nState {
  locale: Locale
  setLocale: (l: Locale) => void
  t: (key: keyof Messages | string, vars?: Record<string, string | number>) => string
}

export const useI18n = create<I18nState>((set, get) => ({
  locale: initialLocale(),
  setLocale: (l) => {
    try { localStorage.setItem(STORAGE_KEY, l) } catch { /* ignore */ }
    set({ locale: l })
  },
  // 绑定当前 locale → 组件消费 t 即订阅 locale 变化，切语言时自动重渲染
  t: (key, vars) => translate(get().locale, key as string, vars),
}))
