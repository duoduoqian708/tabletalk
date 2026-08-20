import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { useI18n } from '@renderer/store/i18n'
// 主题初始化：localStorage 记忆（默认深色「指挥舱」）；在样式加载前设置 data-theme 防闪烁
(function () {
  try {
    var t = localStorage.getItem('tabletalk-theme') || 'dark'
    document.documentElement.setAttribute('data-theme', t)
  } catch (e) { /* ignore */ }
})()
import './styles/tokens.css'
import './styles/boot.css'
import './styles/app.css'
import './styles/review.css'
import './styles/refine.css'
import './styles/deck.css'

document.documentElement.lang = useI18n.getState().locale
useI18n.subscribe((s) => { document.documentElement.lang = s.locale })

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
