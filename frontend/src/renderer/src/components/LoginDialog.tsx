import { useState } from 'react'
import { useI18n } from '@renderer/store/i18n'

interface Props {
  open: boolean
  isInitial: boolean
  onLogin: (username: string, password: string) => Promise<{ ok: boolean; is_initial?: boolean; error?: string }>
  onChangePassword: (username: string, oldPwd: string, newPwd: string) => Promise<{ ok: boolean; error?: string }>
}

export function LoginDialog({ open, isInitial, onLogin, onChangePassword }: Props): React.JSX.Element | null {
  const { t } = useI18n()
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
      setError(res.error || t('login.fail'))
    } else if (res.is_initial) {
      setShowChange(true)
    }
  }

  const handleChange = async (): Promise<void> => {
    if (newPassword.length < 4) {
      setError(t('login.pwdTooShort'))
      return
    }
    if (newPassword !== confirm) {
      setError(t('login.pwdMismatch'))
      return
    }
    setError('')
    setLoading(true)
    const res = await onChangePassword(username, password, newPassword)
    setLoading(false)
    if (!res.ok) {
      setError(res.error || t('login.changeFail'))
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
          <span className="set-title">{t('login.title')}</span>
          <span className="set-sub mono">{t('login.subtitle')}</span>
        </div>
        <div className="set-body" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {!showChange ? (
            <>
              <div className="me-row">
                <label>{t('login.username')}</label>
                <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="admin" />
              </div>
              <div className="me-row">
                <label>{t('login.password')}</label>
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="admin123" onKeyDown={(e) => { if (e.key === 'Enter') void handleLogin() }} />
              </div>
              {isInitial && (
                <div className="hint" style={{ color: 'var(--amber)', fontSize: 12 }}>
                  {t('login.initialBanner')}
                </div>
              )}
              {error && <div className="me-test bad">{error}</div>}
              <div className="me-actions">
                <button className="btn save" disabled={loading || !username || !password} onClick={() => void handleLogin()}>
                  {loading ? t('login.loggingIn') : t('login.submit')}
                </button>
                <button className="mini-btn" onClick={() => setShowChange(true)}>{t('login.changePwd')}</button>
              </div>
              <div className="hint" style={{ fontSize: 11, color: 'var(--ink-faint)' }}>
                {t('login.forgotHint')}
              </div>
            </>
          ) : (
            <>
              <div className="hint" style={{ color: 'var(--amber)', fontSize: 12, padding: '6px 8px', background: 'var(--amber-dim)', borderRadius: 6 }}>
                {t('login.initialNotice')}
              </div>
              <div className="me-row">
                <label>{t('login.oldPassword')}</label>
                <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
              </div>
              <div className="me-row">
                <label>{t('login.newPassword')}</label>
                <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
              </div>
              <div className="me-row">
                <label>{t('login.confirmLabel')}</label>
                <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
              </div>
              {error && <div className="me-test bad">{error}</div>}
              <div className="me-actions">
                <button className="mini-btn" onClick={() => setShowChange(false)}>{t('login.skipChange')}</button>
                <button className="btn save" disabled={loading || !newPassword || !confirm} onClick={() => void handleChange()}>
                  {loading ? t('login.submitting') : t('login.confirmChange')}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
