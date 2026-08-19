import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
// 主题初始化：localStorage 记忆（默认亮色）；在样式加载前设置 data-theme 防闪烁
(function () {
  try {
    var t = localStorage.getItem('tabletalk-theme') || 'light'
    document.documentElement.setAttribute('data-theme', t)
  } catch (e) { /* ignore */ }
})()
import './styles/tokens.css'
import './styles/boot.css'
import './styles/app.css'
import './styles/review.css'
import './styles/refine.css'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
