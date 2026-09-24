// 接入流程共享 helper：演示库连接创建后 → 构建门禁 → 进度 → 待确认 → 一键确认 → ready
// 用法：`import { buildAndConfirm } from './lib-onboard.mjs'`，在点完「使用演示库」后调用。
export async function buildAndConfirm(page, timeoutMs = 90000) {
  await page.waitForSelector('.kb-gate', { timeout: 20000 }).catch(() => {})
  const gate = await page.$('.kb-gate')
  if (!gate) return // 连接可能已 ready（无门禁）
  const badge = await page.textContent('.kb-gate .kb-badge').catch(() => '')
  if (badge.includes('待确认')) {
    await page.click('.kb-gate .btn.save')
    await page.waitForSelector('.kb-gate', { state: 'detached', timeout: 15000 }).catch(() => {})
    return
  }
  // 未构建 → 开始构建 → 等进度 → 待确认 → 确认启用
  await page.click('.kb-gate .btn.save')
  // 新流程：save 先弹「构建确认」对话框，需再点弹窗内「开始」（无弹窗则兼容旧直启路径）
  const dlg = await page.waitForSelector('.kb-dialog .btn.save', { timeout: 5000 }).catch(() => null)
  if (dlg) await dlg.click()
  await page.waitForSelector('.kb-badge.pending', { timeout: timeoutMs })
  await page.click('.kb-gate .btn.save')
  await page.waitForSelector('.kb-gate', { state: 'detached', timeout: 15000 }).catch(() => {})
}
