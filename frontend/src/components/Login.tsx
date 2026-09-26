/** Login screen — real JWT authentication against POST /api/auth/login.
 *
 * Security notes (mirroring the backend):
 * - the token is kept in sessionStorage (tab-scoped, cleared on close), NOT
 *   localStorage, to bound the window if the machine is shared;
 * - credentials are sent only over the API; nothing sensitive is logged;
 * - the generic "Invalid username or password" is shown for every failure
 *   (no username enumeration), exactly as the backend returns it;
 * - the bootstrap admin sees a forced password-rotation prompt.
 */
import { useState } from 'react'
import { api, ApiError, setToken, type SessionUser } from '../api'

export function Login({
  onLogin,
}: {
  onLogin: (username: string, role: SessionUser['role'], mustChange: boolean) => void
}) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!username.trim() || !password) {
      setError('Enter your operator credentials.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await api.login(username.trim(), password)
      setToken(res.access_token)
      onLogin(res.user.username, res.user.role, res.must_change_password)
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 401) {
          // Uniform message for wrong credentials (no enumeration) — mirrors
          // the backend's generic failure envelope.
          setError('Invalid username or password')
        } else if (err.status === 429) {
          setError('Too many attempts — wait a minute and try again.')
        } else if (err.status === 0) {
          setError(err.message) // network: 'Cannot reach the API…'
        } else {
          setError(err.message || 'Login failed. Is the backend running?')
        }
      } else {
        setError('Login failed')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <div className="login-card">
        <div className="login-emblem" aria-hidden>🛡</div>
        <h1>Document Screening System</h1>
        <p className="login-sub">AI-Assisted Identity &amp; Document Verification Console</p>

        <form onSubmit={handleSubmit}>
          <label className="field-label" htmlFor="username">Operator name</label>
          <input
            id="username"
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="e.g. officer.shivam"
            autoComplete="username"
            autoFocus
          />
          <label className="field-label" htmlFor="password">Password</label>
          <input
            id="password"
            className="input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••••••"
            autoComplete="current-password"
          />
          <button className="btn btn-primary btn-block" type="submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        {error && <div className="banner banner-error">{error}</div>}

        <div className="demo-note">
          <strong>Access control:</strong> sessions are role-based — SECURITY_OFFICER
          runs screenings, REVIEWER inspects flagged cases, ADMIN manages users and
          configuration. All registry data is mock/demo data; there is no connection
          to any government database.
        </div>
      </div>
    </div>
  )
}
