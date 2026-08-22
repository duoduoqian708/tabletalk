import { useState } from 'react'

interface Props {
  open: boolean
  isInitial: boolean
  onLogin: (username: string, password: string) => Promise<{ ok: boolean; is_initial?: boolean; error?: string }>
  onChangePassword: (username: string, oldPwd: string, newPwd: string) => Promise<{ ok: boolean; error?: string }>
}

export function LoginDialog({ open, isInitial, onLogin, onChangePassword }: Props): React.JSX.Element | null {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [showChange, setShowChange] = useState(false)
  const [loading, setLoading] = useState(false)

  if (!open) return null

  const handleLogin = async (): Promise<void> => {
    setError('')
    setLoading(true)
    const res = await onLogin(username, password)
    setLoading(false)
    if (!res.ok) {
      setError(res.error || '登录失败')
    } else if (res.is_initial) {
      setShowChange(true)
    }
  }

  const handleChange = async (): Promise<void> => {
    if (newPassword.length < 4) {
      setError('新密码至少 4 位')
      return
    }
    if (newPassword !== confirm) {
      setError('两次输入不一致')
      return
    }
    setError('')
    setLoading(true)
    const res = await onChangePassword(username, password, newPassword)
    setLoading(false)
    if (!res.ok) {
      setError(res.error || '改密失败')
    } else {
      setShowChange(false)
      setPassword(newPassword)
      setNewPassword('')
      setConfirm('')
    }
  }

  return (
    <div className="set-mask" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div className="set-drawer" style={{ width: 360, maxHeight: '80vh' }}>
        <div className="set-head">
          <span className="set-title">登录</span>
          <span className="set-sub mono">个人版 · 内置 admin/admin123</span>
        </div>
        <div className="set-body" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {!showChange ? (
            <>
              <div className="me-row">
                <label>用户名</label>
                <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="admin" />
              </div>
              <div className="me-row">
                <label>密码</label>
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="admin123" onKeyDown={(e) => { if (e.key === 'Enter') void handleLogin() }} />
              </div>
              {isInitial && (
                <div className="hint" style={{ color: 'var(--amber)', fontSize: 12 }}>
                  检测到初始密码，建议修改（可暂不修改，忘记后删 ~/.tabletalk/users.json 重启即恢复，数据保留）
                </div>
              )}
              {error && <div className="me-test bad">{error}</div>}
              <div className="me-actions">
                <button className="btn save" disabled={loading || !username || !password} onClick={() => void handleLogin()}>
                  {loading ? '登录中…' : '登录'}
                </button>
                <button className="mini-btn" onClick={() => setShowChange(true)}>修改密码</button>
              </div>
              <div className="hint" style={{ fontSize: 11, color: 'var(--ink-faint)' }}>
                忘记密码？删除 `~/.tabletalk/users.json` 后重启即恢复为 `admin/admin123`，连接与问题库不变
              </div>
            </>
          ) : (
            <>
              <div className="hint" style={{ color: 'var(--amber)', fontSize: 12, padding: '6px 8px', background: 'var(--amber-dim)', borderRadius: 6 }}>
                您正在使用初始密码，建议修改（可暂不修改）
              </div>
              <div className="me-row">
                <label>旧密码</label>
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
              </div>
              <div className="me-row">
                <label>新密码</label>
                <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
              </div>
              <div className="me-row">
                <label>确认</label>
                <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              </div>
              {error && <div className="me-test bad">{error}</div>}
              <div className="me-actions">
                <button className="mini-btn" onClick={() => setShowChange(false)}>暂不修改</button>
                <button className="btn save" disabled={loading || !newPassword || !confirm} onClick={() => void handleChange()}>
                  {loading ? '提交中…' : '确认修改'}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
