import { useCallback, useEffect, useState } from 'react'
import { api, setToken, type SessionUser } from './api'
import { Login } from './components/Login'
import { Dashboard } from './components/Dashboard'

type Session = {
  username: string
  role: SessionUser['role']
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [mustChangePassword, setMustChangePassword] = useState(false)
  const [restoring, setRestoring] = useState(true)

  // Restore an existing token: validate it server-side (/me) so a stale or
  // revoked token lands on the login screen instead of silent 401s.
  useEffect(() => {
    const token = sessionStorage.getItem('screening.token')
    if (!token) {
      setRestoring(false)
      return
    }
    api.me()
      .then((res) => setSession({ username: res.user.username, role: res.user.role }))
      .catch(() => setToken(null))
      .finally(() => setRestoring(false))
  }, [])

  const handleLogin = useCallback(
    (username: string, role: SessionUser['role'], mustChange: boolean) => {
      setSession({ username, role })
      setMustChangePassword(mustChange)
    },
    [],
  )

  const handleLogout = useCallback(() => {
    // Server-side revocation (jti denylist); a dead token is fine here.
    api.logout().catch(() => undefined)
    setToken(null)
    setSession(null)
    setMustChangePassword(false)
  }, [])

  if (restoring) return null
  if (!session) return <Login onLogin={handleLogin} />
  return (
    <Dashboard
      operator={session.username}
      role={session.role}
      mustChangePassword={mustChangePassword}
      onLogout={handleLogout}
    />
  )
}
