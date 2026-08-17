let toastTimer: ReturnType<typeof setTimeout> | null = null

/** 全局轻提示（底部胶囊）。 */
export function toastMsg(msg: string): void {
  let el = document.querySelector<HTMLDivElement>('.rail-toast')
  if (!el) {
    el = document.createElement('div')
    el.className = 'rail-toast'
    document.body.appendChild(el)
  }
  el.textContent = msg
  el.classList.add('show')
  if (toastTimer) clearTimeout(toastTimer)
  toastTimer = setTimeout(() => el.classList.remove('show'), 1800)
}
