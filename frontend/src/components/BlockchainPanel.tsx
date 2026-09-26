/**
 * Step-10 blockchain audit panel: shows the canonical audit record, the
 * ledger backend, the anchor state and — the core — the verification
 * verdict (recomputed hash vs the ledger commitment).
 *
 * Honesty rules mirrored from the backend:
 * - the ledger stores HASHES + statuses only (never images/PII);
 * - "pending" (mock ledger / offline chain) is shown as such, never faked;
 * - absence of an entry is "not logged", not "tampered".
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api'
import type { AuditLogResponse, AuditRecord, ScreeningResult } from '../types'
import { formatDateTime, shortRunId } from '../helpers'
import { ErrorBanner, Panel, Spinner } from './ui'

type VerifyState = {
  verified: boolean
  on_ledger: boolean
  record_changed: boolean
  conclusion: string
  recomputed_result_hash: string
  db_committed_hash: string | null
  db_hash_match: boolean | null
  ledger_backend: string
}

const VERDICT_META: Record<string, { label: string; className: string }> = {
  unchanged: { label: 'BLOCKCHAIN VERIFIED · UNCHANGED', className: 'status-ok' },
  changed: { label: 'RECORD CHANGED — INVESTIGATE', className: 'status-error' },
  not_logged: { label: 'NOT ON LEDGER', className: 'status-pending' },
  unavailable: { label: 'LEDGER UNAVAILABLE', className: 'status-pending' },
}

function verdictKey(v: VerifyState): keyof typeof VERDICT_META {
  if (v.verified) return 'unchanged'
  if (v.record_changed) return 'changed'
  if (!v.on_ledger) return 'not_logged'
  return 'unavailable'
}

function shortHash(h: string | null | undefined): string {
  return h ? `${h.slice(0, 16)}…` : '—'
}

export function BlockchainPanel({ result }: { result: ScreeningResult }) {
  const screeningId = result.screening_id
  const [record, setRecord] = useState<AuditRecord | null>(null)
  const [verify, setVerify] = useState<VerifyState | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    setBusy(true)
    setError(null)
    try {
      // Read-only: verify the recomputed hash against the ledger. Logging is
      // an explicit user action (button) — never a side effect of viewing.
      setVerify(await api.auditVerify(screeningId))
      setError(null)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Audit verification failed')
    } finally {
      setBusy(false)
    }
  }, [screeningId])

  useEffect(() => {
    refresh()
  }, [refresh])

  async function handleLog() {
    setBusy(true)
    setError(null)
    setNote(null)
    try {
      const log: AuditLogResponse = await api.auditLog(screeningId)
      setRecord(log.audit_record)
      setNote(
        log.ledger.ledger_status === 'anchored'
          ? `Anchored on-chain (block ${log.ledger.block_number ?? '?'}).`
          : log.ledger.ledger_status === 'pending'
            ? 'Logged in the local audit trail; ledger anchor pending (chain offline or mock backend).'
            : (log.ledger.error ?? 'Logged.'),
      )
      setVerify(await api.auditVerify(screeningId))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Audit logging failed')
    } finally {
      setBusy(false)
    }
  }

  const meta = verify ? VERDICT_META[verdictKey(verify)] : null

  return (
    <Panel
      title="Blockchain Audit (Step 10)"
      subtitle="Hash-only commitments · document images and biometrics never go on-chain"
      actions={
        <button className="btn btn-ghost" onClick={handleLog} disabled={busy}>
          Log audit record
        </button>
      }
    >
      {busy && <Spinner label="Checking ledger…" />}
      {error && <ErrorBanner message={error} />}

      {verify && meta && (
        <p className="validity-row">
          <span className={`badge ${meta.className}`}>{meta.label}</span>
          <span className="hint">backend: {verify.ledger_backend}</span>
        </p>
      )}
      {note && <p className="hint">{note}</p>}
      {verify && <p className="hint">{verify.conclusion}</p>}

      {record && (
        <div className="table-wrap">
          <table className="table">
            <tbody>
              <tr><td>Screening ID</td><td className="mono">{shortRunId(record.screening_id)}</td></tr>
              <tr><td>Recorded at (UTC)</td><td className="mono">{formatDateTime(record.timestamp)}</td></tr>
              <tr>
                <td>Result hash (sha256)</td>
                <td className="mono break" title={record.result_hash}>{shortHash(record.result_hash)}</td>
              </tr>
              <tr><td>Risk score</td><td>{record.risk_score ?? '—'}</td></tr>
              <tr><td>Validation status</td><td>{record.validation_status}</td></tr>
              <tr><td>Tampering status</td><td>{record.tampering_status}</td></tr>
              <tr><td>Face match status</td><td>{record.face_match_status}</td></tr>
              <tr><td>System</td><td className="hint">{record.system_id}</td></tr>
            </tbody>
          </table>
        </div>
      )}

      {verify && (
        <details className="raw-text-details">
          <summary>Verification details</summary>
          <ul className="mini-list">
            <li>Recomputed: <code className="mono">{shortHash(verify.recomputed_result_hash)}</code></li>
            <li>DB committed: <code className="mono">{shortHash(verify.db_committed_hash)}</code></li>
            <li>
              DB↔recomputed match: {verify.db_hash_match === null ? 'n/a' : verify.db_hash_match ? 'yes' : 'NO'}
            </li>
          </ul>
          <p className="hint">
            The ledger receives hashes and statuses only — never document images,
            raw biometric data or personal fields.
          </p>
        </details>
      )}
    </Panel>
  )
}
