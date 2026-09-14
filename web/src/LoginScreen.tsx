import { useState } from 'react'
import { login } from './api'
import type { Identity } from './api'

/**
 * Login, shown only when the API says authentication is required.
 *
 * A hosted instance runs with `AUTH_REQUIRED=true`; local development does not,
 * and this screen never appears there — the platform behaves exactly as it did
 * before M8 for anyone running it from the repository.
 *
 * The evaluator's account is a **viewer**: every screen, every trace, every
 * report, and nothing that writes. That is enforced by the API rather than by
 * hiding buttons, so it holds against a client that ignores what it is told.
 */
export function LoginScreen({ onSignedIn }: { onSignedIn: (who: Identity) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      onSignedIn(await login(username.trim(), password))
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login">
      <form className="login-card" onSubmit={submit}>
        <h1>Gujarat CCTV Integration Platform</h1>
        <p className="muted small">
          Statewide camera registry, ANPR, vehicle trace and live watchlist
          alerting. Sign in to continue.
        </p>

        <label>
          Username
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            spellCheck={false}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
          />
        </label>

        {error && <p className="error">{error}</p>}

        <button className="primary" type="submit" disabled={busy || !username || !password}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>

        <p className="muted small login-note">
          The demo account is read-only: it can see everything the platform holds
          and change none of it. The API enforces that, not this page.
        </p>
      </form>
    </div>
  )
}
