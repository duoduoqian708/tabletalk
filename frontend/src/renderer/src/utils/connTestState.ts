/** 连接测试状态持久化：按连接 id 存 localStorage（唯一键定位），卡片「测试通过」标签据此点亮。 */
export interface ConnTestState { ok: boolean; latency_ms?: number; error?: string; ts: number }

const TEST_STATE_KEY = 'tabletalk-conn-test-'

export function loadTestState(id: string): ConnTestState | null {
  try {
    const raw = localStorage.getItem(TEST_STATE_KEY + id)
    return raw ? (JSON.parse(raw) as ConnTestState) : null
  } catch { return null }
}

export function saveTestState(id: string, s: ConnTestState): void {
  try { localStorage.setItem(TEST_STATE_KEY + id, JSON.stringify(s)) } catch { /* ignore */ }
}
